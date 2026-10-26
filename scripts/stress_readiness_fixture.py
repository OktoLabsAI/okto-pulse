"""Concurrent isolated readiness load, writes, and publication-lag probe.

This uses the Spec's population fixture. The candidate read can exercise the
real REST route in-process (including authorization and JSON serialization) or
the lower-level service; the legacy read remains an internal source snapshot.
Writer latency is split into lock wait and transaction time.
Publication targets share one bounded polling query; the engine uses the
Community SQLite PRAGMAs. Delivery evidence writes append directly to SQLite
without the API command path. The in-process REST mode excludes network and
browser costs, so this isolated harness alone cannot prove the UI SLO.
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import logging
from multiprocessing import get_context
import os
from pathlib import Path
import platform
import psutil
import sqlite3
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter, process_time

from fastapi import FastAPI
import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from benchmark_readiness_fixture import (
    seed, percentile, register_fixture_verifier, enrich_mixed_fixture, audit_fixture,
)
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_persisted_readiness import CommunityPersistedReadinessStore
from okto_pulse.community.adapters.sqlalchemy_database import install_community_sqlite_pragmas
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import (
    CommunityDeliveryReadinessReader, CommunityReadinessClock,
)
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.community.api import code_traceability as readiness_api
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.delivery_readiness import ReadinessActor
from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.core.ports.test_evidence import reset_test_evidence_write_verifier_for_tests
from okto_pulse.core.services.delivery_readiness import DeliveryReadinessService
from okto_pulse.core.services.persisted_delivery_readiness import build_readiness_generation

ACTOR = ReadinessActor('benchmark-agent', 'agent', 'local', 'benchmark-board')


async def audit_source_projection_parity(path: Path, args: argparse.Namespace) -> dict:
    """Outside the timed run, compare every stored manifest to its current source."""
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}', pool_size=20, max_overflow=0)
    install_community_sqlite_pragmas(engine)
    store = CommunityPersistedReadinessStore(engine)
    worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
        maintenance_enabled=False)

    class FixedClock:
        def __init__(self, value):
            self.value = value

        def now(self):
            return self.value

    drained = 0
    deadline = monotonic() + max(60, args.specs * 10)
    try:
        while True:
            async with engine.connect() as connection:
                rows = (await connection.exec_driver_sql(
                    "SELECT s.spec_id,s.source_revision,g.captured_revision,j.state,j.error_code "
                    "FROM readiness_scope s LEFT JOIN readiness_generation g "
                    "ON g.id=s.active_generation_id JOIN readiness_job j ON j.scope_id=s.id "
                    "WHERE s.board_id='benchmark-board'")).all()
            if len(rows) != args.specs:
                raise AssertionError(f'parity_scope_count:{len(rows)}/{args.specs}')
            terminal = [(row[0], row[4]) for row in rows if row[3] == 'terminal']
            if terminal:
                raise AssertionError(f'parity_terminal:{terminal}')
            if all(row[1] == row[2] for row in rows):
                break
            if monotonic() >= deadline:
                raise TimeoutError('parity_drain_deadline')
            drained += await worker.process_once()
            await asyncio.sleep(.1)

        for number in range(args.specs):
            spec_id = f'benchmark-spec-{number:03d}'
            projected = await store.read(board_id='benchmark-board', spec_id=spec_id, edition=1)
            if projected is None or projected.freshness != 'current' or projected.evaluated_at is None:
                raise AssertionError(f'parity_generation_unavailable:{spec_id}')
            if not await store.audit_active_generation(spec_id=spec_id):
                raise AssertionError(f'parity_manifest_corrupt:{spec_id}')
            reader = CommunityDeliveryReadinessReader(engine, clock=FixedClock(projected.evaluated_at))
            snapshot, revision = await reader.load_for_projection(
                board_id='benchmark-board', spec_id=spec_id,
                blocking_execution=TrackedBlockingExecution())
            expected = await asyncio.to_thread(build_readiness_generation, snapshot,
                captured_revision=revision)
            async with engine.connect() as connection:
                manifest = (await connection.exec_driver_sql(
                    'SELECT manifest_sha256 FROM readiness_generation WHERE id=?',
                    (projected.generation_id,))).scalar_one()
            if (revision != projected.captured_revision
                    or expected.source_snapshot_id != projected.source_snapshot_id
                    or expected.manifest_sha256 != manifest):
                raise AssertionError(f'parity_source_mismatch:{spec_id}')
        return {'status': 'pass', 'scopes': args.specs, 'drained_publications': drained}
    finally:
        await engine.dispose()


def rest_client(service: DeliveryReadinessService) -> httpx.AsyncClient:
    """Use the productive FastAPI route without external network RTT."""
    app = FastAPI()
    app.include_router(readiness_api.router)

    async def principal() -> Principal:
        return Principal(subject=ACTOR.actor_id, realm_id=ACTOR.realm_id,
            actor_kind='agent', claims={'permissions': registered_permission_flags()})

    async def unit_of_work():
        yield SimpleNamespace(services=SimpleNamespace(delivery_readiness=service),
            commit=AsyncMock())

    app.dependency_overrides[readiness_api.require_principal] = principal
    app.dependency_overrides[readiness_api.get_unit_of_work] = unit_of_work
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
        base_url='http://readiness-benchmark')


async def run(path: Path, args: argparse.Namespace) -> dict:
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}',
        pool_size=20, max_overflow=0)
    if args.mode == 'persisted':
        install_community_sqlite_pragmas(engine)
    else:
        # The legacy comparison deliberately removes readiness triggers, so
        # current-schema admission must refuse it. Match the runtime's SQLite
        # connection settings without pretending that schema is current.
        @event.listens_for(engine.sync_engine, 'connect')
        def _legacy_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute('PRAGMA journal_mode=WAL')
                cursor.execute('PRAGMA busy_timeout=30000')
                cursor.execute('PRAGMA synchronous=FULL')
                cursor.execute('PRAGMA foreign_keys=ON')
            finally:
                cursor.close()
    executor = (ProcessPoolExecutor(max_workers=args.worker_concurrency,
        mp_context=get_context('spawn'), initializer=register_fixture_verifier,
        initargs=(path,))
        if args.full_projection_process else None)
    workers = [ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
        full_projection_executor=executor, database_path=path,
        maintenance_enabled=index == 0,
        max_inflight=args.pipeline_depth if index == 0 else 1)
        for index in range(args.worker_concurrency)]
    worker = workers[0]
    reader = CommunityDeliveryReadinessReader(engine, clock=CommunityReadinessClock())
    service = DeliveryReadinessService(reader, None)
    client = rest_client(service) if args.read_surface == 'rest' else None
    read_ms, write_ms, lag_ms, burst_write_ms, burst_lag_ms = [], [], [], [], []
    lock_wait_ms, transaction_ms, burst_lock_wait_ms, burst_transaction_ms = [], [], [], []
    source_update_ms, source_commit_ms = [], []
    errors: list[str] = []
    warmup_errors: list[str] = []
    freshness: dict[str, int] = {}
    measuring = args.warmup_seconds == 0
    read_phases_ms: dict[str, list[float]] = {
        'checkout': [], 'auth_before': [], 'projection': [], 'auth_after': [],
        'store_db': [], 'store_decode': [],
        'identity': [], 'presets': [], 'policy': [],
    }
    publish_phases_ms: dict[str, list[float]] = {
        'snapshot': [], 'compute': [], 'stage': [], 'publish': [],
        'snapshot_checkout': [], 'snapshot_begin': [], 'snapshot_scope': [],
        'snapshot_source': [], 'snapshot_rollback': [],
        'snapshot_rows': [], 'snapshot_materialize': [],
        'stage_metadata': [], 'stage_pack': [], 'stage_heartbeat': [],
        'stage_insert': [], 'stage_verify': [],
        'stage_verify_read': [], 'stage_verify_decode': [],
        'stage_verify_validate': [], 'stage_verify_digest': [],
    }

    class ReadPhaseHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if not measuring:
                return
            if record.msg == 'readiness.persisted_read_phases':
                read_phases_ms['checkout'].append(record.readiness_checkout_ms)
                read_phases_ms['auth_before'].append(record.readiness_auth_before_ms)
                read_phases_ms['projection'].append(record.readiness_projection_ms)
                read_phases_ms['auth_after'].append(record.readiness_auth_after_ms)
            elif record.msg == 'readiness.persisted_store_phases':
                read_phases_ms['store_db'].append(record.readiness_store_db_ms)
                read_phases_ms['store_decode'].append(record.readiness_store_decode_ms)
            elif record.msg == 'readiness.access_phases':
                read_phases_ms['identity'].append(record.readiness_identity_ms)
                read_phases_ms['presets'].append(record.readiness_presets_ms)
                read_phases_ms['policy'].append(record.readiness_policy_ms)
            elif record.msg == 'readiness.projection_snapshot_phases':
                publish_phases_ms['snapshot_checkout'].append(record.readiness_checkout_ms)
                publish_phases_ms['snapshot_begin'].append(record.readiness_begin_ms)
                publish_phases_ms['snapshot_scope'].append(record.readiness_scope_ms)
                publish_phases_ms['snapshot_source'].append(record.readiness_source_ms)
                publish_phases_ms['snapshot_rollback'].append(record.readiness_rollback_ms)
                publish_phases_ms['snapshot_rows'].append(record.readiness_read_rows_ms)
                publish_phases_ms['snapshot_materialize'].append(record.readiness_materialize_ms)
                for table, elapsed in record.readiness_query_ms.items():
                    publish_phases_ms.setdefault(f'snapshot_query_{table}', []).append(elapsed)
            elif record.msg == 'readiness.stage_phases':
                for key in ('metadata', 'pack', 'heartbeat', 'insert', 'verify'):
                    publish_phases_ms[f'stage_{key}'].append(
                        getattr(record, f'readiness_stage_{key}_ms'))
                for key in ('read', 'decode', 'validate', 'digest'):
                    value = getattr(record, f'readiness_stage_verify_{key}_ms', None)
                    if value is not None:
                        publish_phases_ms[f'stage_verify_{key}'].append(value)
            elif isinstance(record.msg, str) and record.msg.startswith('readiness.published '):
                for name, value in zip(('snapshot', 'compute', 'stage', 'publish'), record.args[4:8]):
                    publish_phases_ms[name].append(float(value))
            elif isinstance(record.msg, str) and record.msg.startswith('readiness.projection_failed '):
                detail = str(record.exc_info[1]).splitlines()[0][:160] if record.exc_info else ''
                (errors if measuring else warmup_errors).append(
                    f'worker:{record.args[2]}:{detail}')

    phase_logger = logging.getLogger(
        'okto_pulse.community.adapters.sqlalchemy_delivery_readiness')
    previous_log_level = phase_logger.level
    previous_propagate = phase_logger.propagate
    phase_handler = ReadPhaseHandler()
    phase_logger.addHandler(phase_handler)
    phase_logger.setLevel(logging.DEBUG)
    phase_logger.propagate = False
    publish_logger = logging.getLogger('okto_pulse.readiness_projection')
    previous_publish_level = publish_logger.level
    publish_logger.addHandler(phase_handler)
    publish_logger.setLevel(logging.DEBUG)
    stop_load = asyncio.Event()
    stop_worker = asyncio.Event()
    writer_guard = asyncio.Lock()
    pending_observations: dict[int, tuple[str, int, float, list[float]]] = {}
    next_observation_id = 0
    worker_publications = 0
    burst_elapsed_ms = None
    rss_bytes, pool_checkouts, blocking_tasks, wal_samples = [], [], [], []
    resource_sample_ms: list[float] = []
    event_loop_lag_ms: list[float] = []
    cpu_start = process_time()

    async def write_source(connection, *, index: int, spec_id: str, phase: str) -> None:
        if args.write_kind == 'spec_description':
            await connection.exec_driver_sql(
                'UPDATE specs SET description=? WHERE id=?',
                (f'{phase}-{index}', spec_id))
            return
        record_id = f'{spec_id}-write-{phase}-{index}'
        # A valid material checkpoint changes proof qualification for the
        # target Card; a free-form note would only churn the snapshot hash.
        payload = json.dumps({'progress': {
            'contract_version': 'delivery-progress/v2', 'material_change': 'unknown',
            'source_state': {'workspace_state': 'unknown', 'recoverability': 'unknown'},
            'target_ids': [], 'remaining': f'Benchmark {phase} work {index}',
        }, 'justification': f'Benchmark {phase} report {index}'})
        await connection.exec_driver_sql(
            'INSERT INTO card_delivery_evidence_records '
            '(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,'
            'idempotency_key,payload_sha256,payload,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (record_id, 'benchmark-board', f'{spec_id}-task-000', spec_id, 1,
                'progress', 'benchmark-agent', 'agent', record_id,
                sha256(payload.encode()).hexdigest(), payload,
                datetime.now(timezone.utc).isoformat()))

    try:
        initial_fixture_audit = None
        if args.mode == 'persisted':
            published = 0
            while published < args.specs:
                published += sum(await asyncio.gather(
                    *(item.process_once() for item in workers)))
                if (await worker.store.queue_status())['counts'].get('terminal', 0):
                    raise RuntimeError(f'readiness_fixture_initial_terminal:{warmup_errors[-3:]}')
                await asyncio.sleep(.06)
            initial_fixture_audit = audit_fixture(path, args)
        # Exclude connection/thread cold start from steady-state write latency.
        async with engine.begin() as connection:
            await write_source(connection, index=-1,
                spec_id='benchmark-spec-000', phase='warmup')
        if args.mode == 'persisted':
            await asyncio.sleep(.06)
            await worker.process_once()

        async def projection_loop(item):
            nonlocal worker_publications
            while not stop_worker.is_set():
                processed = 0
                try:
                    processed = await item.process_once()
                    worker_publications += processed
                except Exception as exc:
                    detail = str(exc).splitlines()[0][:160]
                    (errors if measuring else warmup_errors).append(
                        f'worker:{type(exc).__name__}:{detail}')
                await asyncio.sleep(0 if processed else .1)

        async def reader_loop(client_number: int):
            index = client_number
            period = args.clients / args.reads_per_second
            while not stop_load.is_set():
                spec_id = f'benchmark-spec-{index % args.specs:03d}'
                started = perf_counter()
                try:
                    if args.mode == 'legacy':
                        await reader.load(board_id='benchmark-board', spec_id=spec_id)
                    else:
                        if args.read_surface == 'rest':
                            response = await client.get(
                                f'/boards/benchmark-board/specs/{spec_id}/delivery-readiness-projection')
                            response.raise_for_status()
                            result = response.json()
                            observed_freshness = result['projection']['freshness']
                        elif args.read_surface == 'service':
                            result = await service.read_persisted(board_id='benchmark-board',
                                spec_id=spec_id, actor=ACTOR)
                            json.dumps(result, ensure_ascii=False)
                            observed_freshness = result['projection']['freshness']
                        else:
                            result = await worker.store.read(board_id='benchmark-board',
                                spec_id=spec_id, edition=1)
                            if result is None:
                                raise RuntimeError('projection_missing')
                            observed_freshness = result.freshness
                        if measuring:
                            freshness[observed_freshness] = freshness.get(observed_freshness, 0) + 1
                    if measuring:
                        read_ms.append((perf_counter() - started) * 1000)
                except httpx.HTTPStatusError as exc:
                    try:
                        detail = exc.response.json().get('detail')
                        code = detail.get('code') if isinstance(detail, dict) else None
                    except (ValueError, AttributeError):
                        code = None
                    (errors if measuring else warmup_errors).append(
                        f'read:http_{exc.response.status_code}:{code or "unknown"}')
                except Exception as exc:
                    (errors if measuring else warmup_errors).append(
                        f'read:{getattr(exc, "code", type(exc).__name__)}')
                index += args.clients
                await asyncio.sleep(max(0, period - (perf_counter() - started)))

        async def record_write(index: int, *, burst: bool = False):
                nonlocal next_observation_id
                measured = measuring
                spec_id = f'benchmark-spec-{index % args.specs:03d}'
                began = perf_counter()
                try:
                    async def commit_write():
                        async with engine.connect() as connection:
                            lock_requested = perf_counter()
                            await connection.exec_driver_sql('BEGIN IMMEDIATE')
                            lock_acquired = perf_counter()
                            try:
                                await write_source(connection, index=index, spec_id=spec_id,
                                    phase='burst' if burst else 'load')
                                update_done = perf_counter()
                                await connection.commit()
                                committed_at = perf_counter()
                                # The real source command commits with its intent trigger.
                                # Observing the revision belongs outside the writer lock.
                                revision = (await connection.exec_driver_sql(
                                    'SELECT source_revision FROM readiness_scope WHERE spec_id=?',
                                    (spec_id,))).scalar_one() if args.mode == 'persisted' else None
                                return (revision, committed_at,
                                    (lock_acquired - lock_requested) * 1000,
                                    (committed_at - lock_acquired) * 1000,
                                    (update_done - lock_acquired) * 1000,
                                    (committed_at - update_done) * 1000)
                            except BaseException:
                                await connection.rollback()
                                raise
                    if args.serialize_writes:
                        async with writer_guard:
                            revision, committed, waited_ms, held_ms, updated_ms, committed_ms = await commit_write()
                    else:
                        revision, committed, waited_ms, held_ms, updated_ms, committed_ms = await commit_write()
                    if measured:
                        write_ms.append((committed - began) * 1000)
                        lock_wait_ms.append(waited_ms)
                        transaction_ms.append(held_ms)
                        source_update_ms.append(updated_ms)
                        source_commit_ms.append(committed_ms)
                    if burst and measured:
                        burst_write_ms.append((committed - began) * 1000)
                        burst_lock_wait_ms.append(waited_ms)
                        burst_transaction_ms.append(held_ms)
                    if args.mode == 'legacy' or not measured:
                        return
                    next_observation_id += 1
                    pending_observations[next_observation_id] = (
                        spec_id, revision, committed,
                        burst_lag_ms if burst else lag_ms)
                except Exception as exc:
                    (errors if measured else warmup_errors).append(f'write:{type(exc).__name__}')

        async def observation_loop():
            while not stop_load.is_set() or pending_observations:
                if pending_observations:
                    async with engine.connect() as connection:
                        rows = (await connection.exec_driver_sql(
                            'SELECT s.spec_id,g.captured_revision FROM readiness_scope s '
                            'LEFT JOIN readiness_generation g ON g.id=s.active_generation_id '
                            'WHERE s.board_id=?', ('benchmark-board',))).all()
                    revisions = {spec_id: revision for spec_id, revision in rows}
                    observed_at = perf_counter()
                    for observation_id, (spec_id, target, committed_at, destination) in tuple(
                            pending_observations.items()):
                        revision = revisions.get(spec_id)
                        if revision is not None and revision >= target:
                            destination.append((observed_at - committed_at) * 1000)
                            pending_observations.pop(observation_id, None)
                        elif observed_at - committed_at >= 10:
                            errors.append('publish_deadline')
                            pending_observations.pop(observation_id, None)
                await asyncio.sleep(.1)

        async def writer_loop(started_at: float):
            if args.writes_per_second == 0:
                await stop_load.wait()
                return
            index = 0
            while not stop_load.is_set():
                await record_write(index)
                index += 1
                await asyncio.sleep(max(0, started_at + index / args.writes_per_second - monotonic()))

        async def burst_loop(started_at: float):
            nonlocal burst_elapsed_ms
            if args.burst_size <= 0:
                return
            await asyncio.sleep(max(0, started_at + args.burst_at_seconds - monotonic()))
            launched_at = perf_counter()
            pending = []
            for index in range(args.burst_size):
                await asyncio.sleep(max(0, started_at + args.burst_at_seconds
                    + index / args.burst_size - monotonic()))
                target = 10_000 + (index * args.specs if args.burst_scope == 'single' else index)
                pending.append(asyncio.create_task(record_write(target, burst=True)))
            await asyncio.gather(*pending)
            burst_elapsed_ms = (perf_counter() - launched_at) * 1000

        async def resource_loop():
            if args.resource_sample_seconds == 0:
                await stop_load.wait()
                return
            process = psutil.Process()
            wal = path.with_name(path.name + '-wal')

            def sample_filesystem_and_processes():
                child_rss = 0
                for child in process.children(recursive=True):
                    try:
                        child_rss += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                return (process.memory_info().rss + child_rss,
                    wal.stat().st_size if wal.exists() else 0)

            while not stop_load.is_set():
                await asyncio.sleep(args.resource_sample_seconds)
                if measuring:
                    sample_started = perf_counter()
                    rss, wal_size = await asyncio.to_thread(sample_filesystem_and_processes)
                    resource_sample_ms.append((perf_counter() - sample_started) * 1000)
                    rss_bytes.append(rss)
                    pool_checkouts.append(engine.sync_engine.pool.checkedout())
                    blocking_tasks.append(sum(item.blocking_execution.pending_count
                        for item in workers))
                    wal_samples.append(wal_size)

        async def event_loop_monitor():
            while not stop_load.is_set():
                deadline = monotonic() + .5
                await asyncio.sleep(max(0, deadline - monotonic()))
                if measuring:
                    event_loop_lag_ms.append(max(0, (monotonic() - deadline) * 1000))

        started_at = monotonic()
        projection_tasks = ([asyncio.create_task(projection_loop(item)) for item in workers]
            if args.mode == 'persisted' else [])
        observation_task = asyncio.create_task(observation_loop()) if args.mode == 'persisted' else None
        tasks = [asyncio.create_task(writer_loop(started_at)),
            *(asyncio.create_task(reader_loop(index)) for index in range(args.clients)),
            asyncio.create_task(resource_loop()), asyncio.create_task(event_loop_monitor())]
        if args.warmup_seconds:
            await asyncio.sleep(args.warmup_seconds)
            measuring = True
        if args.burst_size:
            tasks.append(asyncio.create_task(burst_loop(monotonic())))
        await asyncio.sleep(args.duration_seconds)
        stop_load.set()
        await asyncio.gather(*tasks)
        if observation_task is not None:
            await observation_task
        stop_worker.set()
        if projection_tasks:
            await asyncio.gather(*projection_tasks)
        lagging = []
        if args.mode == 'persisted':
            async with engine.connect() as connection:
                lagging = (await connection.exec_driver_sql(
                    'SELECT s.spec_id,s.source_revision,g.captured_revision,j.state,j.error_code '
                    'FROM readiness_scope s LEFT JOIN readiness_generation g '
                    'ON g.id=s.active_generation_id JOIN readiness_job j ON j.scope_id=s.id '
                    'WHERE g.captured_revision IS NULL OR g.captured_revision<s.source_revision'
                )).all()
        return {
            'mode': args.mode, 'duration_s': args.duration_seconds,
            'fixture_audit': initial_fixture_audit,
            'read_surface': args.read_surface,
            'writes_per_second': args.writes_per_second,
            'write_kind': args.write_kind,
            'serialize_writes': args.serialize_writes,
            'warmup_s': args.warmup_seconds, 'warmup_errors': warmup_errors[:25],
            'fixture': {'specs': args.specs, 'cards_per_spec': args.cards,
                'obligations_per_spec': args.obligations, 'scenarios_per_spec': args.scenarios,
                'facts_per_spec': args.facts, 'history_per_spec': args.history,
                'overload_first': args.overload_first,
                'overload_shape': {'cards': args.overload_cards,
                    'obligations': args.overload_obligations,
                    'scenarios': args.overload_scenarios,
                    'facts': args.overload_facts} if args.overload_first else None},
            'reads': len(read_ms), 'writes': len(write_ms),
            'error_count': len(errors), 'errors': errors[:25],
            'warmup_error_count': len(warmup_errors),
            'worker_publications': worker_publications,
            'worker_concurrency': args.worker_concurrency,
            'full_projection_process': args.full_projection_process,
            'lagging_scopes': [tuple(row) for row in lagging],
            'read_p50_ms': round(median(read_ms), 3) if read_ms else None,
            'read_p95_ms': round(percentile(read_ms, .95), 3) if read_ms else None,
            'read_p99_ms': round(percentile(read_ms, .99), 3) if read_ms else None,
            'read_max_ms': round(max(read_ms), 3) if read_ms else None,
            'read_phases_p95_ms': {
                name: round(percentile(samples, .95), 3) if samples else None
                for name, samples in read_phases_ms.items()},
            'publish_phases_p95_ms': {
                name: round(percentile(samples, .95), 3) if samples else None
                for name, samples in publish_phases_ms.items()},
            'write_p50_ms': round(median(write_ms), 3) if write_ms else None,
            'write_p95_ms': round(percentile(write_ms, .95), 3) if write_ms else None,
            'write_p99_ms': round(percentile(write_ms, .99), 3) if write_ms else None,
            'write_max_ms': round(max(write_ms), 3) if write_ms else None,
            'lock_wait_p95_ms': round(percentile(lock_wait_ms, .95), 3) if lock_wait_ms else None,
            'transaction_p95_ms': round(percentile(transaction_ms, .95), 3) if transaction_ms else None,
            'transaction_max_ms': round(max(transaction_ms), 3) if transaction_ms else None,
            'source_update_p95_ms': round(percentile(source_update_ms, .95), 3) if source_update_ms else None,
            'source_commit_p95_ms': round(percentile(source_commit_ms, .95), 3) if source_commit_ms else None,
            'burst': {'requested': args.burst_size, 'scope': args.burst_scope,
                'writes': len(burst_write_ms),
                'elapsed_ms': round(burst_elapsed_ms, 3) if burst_elapsed_ms is not None else None,
                'write_p95_ms': round(percentile(burst_write_ms, .95), 3) if burst_write_ms else None,
                'lock_wait_p95_ms': round(percentile(burst_lock_wait_ms, .95), 3) if burst_lock_wait_ms else None,
                'transaction_p95_ms': round(percentile(burst_transaction_ms, .95), 3) if burst_transaction_ms else None,
                'publish_p95_ms': round(percentile(burst_lag_ms, .95), 3) if burst_lag_ms else None,
                'publish_max_ms': round(max(burst_lag_ms), 3) if burst_lag_ms else None},
            'publish_p95_ms': round(percentile(lag_ms, .95), 3) if lag_ms else None,
            'publish_p99_ms': round(percentile(lag_ms, .99), 3) if lag_ms else None,
            'publish_max_ms': round(max(lag_ms), 3) if lag_ms else None,
            'publish_observations': len(lag_ms), 'freshness': freshness,
            'cpu_s': round(process_time() - cpu_start, 3),
            'resources': {'rss_max_bytes': max(rss_bytes, default=0),
                'pool_checked_out_max': max(pool_checkouts, default=0),
                'blocking_tasks_max': max(blocking_tasks, default=0),
                'wal_max_bytes': max(wal_samples, default=0),
                'resource_sample_p95_ms': round(percentile(resource_sample_ms, .95), 3)
                    if resource_sample_ms else None,
                'event_loop_lag_p95_ms': round(percentile(event_loop_lag_ms, .95), 3)
                    if event_loop_lag_ms else None},
            'pool': engine.sync_engine.pool.status(),
            'wal_bytes': path.with_name(path.name + '-wal').stat().st_size
                if path.with_name(path.name + '-wal').exists() else 0,
            'samples_ms': {'read': [round(v, 3) for v in read_ms],
                'read_phases': {
                    name: [round(v, 3) for v in samples]
                    for name, samples in read_phases_ms.items()},
                'publish_phases': {
                    name: [round(v, 3) for v in samples]
                    for name, samples in publish_phases_ms.items()},
                'write': [round(v, 3) for v in write_ms],
                'lock_wait': [round(v, 3) for v in lock_wait_ms],
                'transaction': [round(v, 3) for v in transaction_ms],
                'source_update': [round(v, 3) for v in source_update_ms],
                'source_commit': [round(v, 3) for v in source_commit_ms],
                'publish': [round(v, 3) for v in lag_ms],
                'burst_write': [round(v, 3) for v in burst_write_ms],
                'burst_lock_wait': [round(v, 3) for v in burst_lock_wait_ms],
                'burst_transaction': [round(v, 3) for v in burst_transaction_ms],
                'burst_publish': [round(v, 3) for v in burst_lag_ms]},
            'resource_samples': {'rss_bytes': rss_bytes, 'pool_checkouts': pool_checkouts,
                'blocking_tasks': blocking_tasks, 'wal_bytes': wal_samples,
                'resource_sample_ms': [round(v, 3) for v in resource_sample_ms],
                'event_loop_lag_ms': [round(v, 3) for v in event_loop_lag_ms]},
        }
    finally:
        phase_logger.removeHandler(phase_handler)
        phase_logger.setLevel(previous_log_level)
        phase_logger.propagate = previous_propagate
        publish_logger.removeHandler(phase_handler)
        publish_logger.setLevel(previous_publish_level)
        if client is not None:
            await client.aclose()
        await engine.dispose()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['legacy', 'persisted'], required=True)
    parser.add_argument('--full-projection-process', action='store_true')
    parser.add_argument('--worker-concurrency', type=int, default=1)
    parser.add_argument('--pipeline-depth', type=int, choices=(1, 2), default=1)
    parser.add_argument('--read-surface', choices=['internal', 'service', 'rest'])
    parser.add_argument('--duration-seconds', type=int, default=60)
    parser.add_argument('--warmup-seconds', type=int, default=0)
    parser.add_argument('--reads-per-second', type=int, default=10)
    parser.add_argument('--writes-per-second', type=float, default=1)
    parser.add_argument('--write-kind', choices=('spec_description', 'delivery_evidence_append'),
        default='delivery_evidence_append')
    parser.add_argument('--clients', type=int, default=5)
    parser.add_argument('--resource-sample-seconds', type=float, default=.5,
        help='Resource sampler interval; 0 disables resource sampling for diagnosis')
    parser.add_argument('--specs', type=int, default=10)
    parser.add_argument('--cards', type=int, default=100)
    parser.add_argument('--obligations', type=int, default=100)
    parser.add_argument('--scenarios', type=int, default=200)
    parser.add_argument('--facts', type=int, default=2000)
    parser.add_argument('--history', type=int, default=50)
    parser.add_argument('--fixture-profile', choices=('simple', 'mixed'), default='mixed')
    parser.add_argument('--overload-first', action='store_true')
    parser.add_argument('--overload-cards', type=int, default=500)
    parser.add_argument('--overload-obligations', type=int, default=500)
    parser.add_argument('--overload-scenarios', type=int, default=200)
    parser.add_argument('--overload-facts', type=int, default=5000)
    parser.add_argument('--burst-size', type=int, default=0)
    parser.add_argument('--burst-scope', choices=('single', 'round_robin'), default='single')
    parser.add_argument('--serialize-writes', action='store_true')
    parser.add_argument('--burst-at-seconds', type=int, default=5)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.read_surface is None:
        args.read_surface = 'internal' if args.mode == 'legacy' else 'service'
    if args.mode == 'legacy' and args.read_surface != 'internal':
        parser.error('legacy fixture only supports the internal read; it is not a v1 service baseline')
    if args.burst_size and (args.burst_at_seconds < 0 or args.burst_at_seconds >= args.duration_seconds):
        parser.error('burst-at-seconds must fall inside the run')
    if args.writes_per_second < 0:
        parser.error('writes-per-second must be nonnegative')
    if args.resource_sample_seconds < 0:
        parser.error('resource-sample-seconds must be nonnegative')
    if args.worker_concurrency < 1 or args.worker_concurrency > 4:
        parser.error('worker-concurrency must be between 1 and 4')
    if args.pipeline_depth > 1 and args.worker_concurrency != 1:
        parser.error('pipeline-depth=2 requires one claim worker')
    with TemporaryDirectory(prefix='readiness-stress-') as folder:
        path = Path(folder) / 'pulse.db'
        seed(path, args)
        if args.mode == 'legacy':
            with closing(sqlite3.connect(path)) as connection:
                names = [name for (name,) in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='trigger' "
                    "AND name LIKE 'trg_readiness_%'")]
                for name in names:
                    connection.execute(f'DROP TRIGGER "{name}"')
                connection.commit()
        register_fixture_verifier(path)
        try:
            asyncio.run(enrich_mixed_fixture(path, args))
            result = asyncio.run(run(path, args))
            if args.mode == 'persisted':
                try:
                    result['post_load_parity'] = asyncio.run(audit_source_projection_parity(path, args))
                except Exception as exc:
                    result['post_load_parity'] = {'status': 'failed',
                        'code': type(exc).__name__, 'detail': str(exc)[:200]}
        finally:
            reset_test_evidence_write_verifier_for_tests()
        result['fixture_seed'] = f'readiness-deterministic-v3-{args.fixture_profile}'
        if result['fixture_audit'] is None:
            result['fixture_audit'] = audit_fixture(path, args)
        result['seed_sha256'] = sha256(Path(seed.__code__.co_filename).read_bytes()).hexdigest()
        result['harness_sha256'] = sha256(Path(__file__).read_bytes()).hexdigest()
        result['command'] = ['stress_readiness_fixture.py', *sys.argv[1:]]
        result['host'] = {'platform': platform.platform(), 'python': platform.python_version(),
            'logical_cpus': os.cpu_count()}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: v for k, v in result.items() if k not in {'samples_ms', 'resource_samples'}}, sort_keys=True), flush=True)
        else:
            print(json.dumps({k: v for k, v in result.items() if k not in {'samples_ms', 'resource_samples'}}, sort_keys=True), flush=True)
        if (result['error_count'] or result['warmup_error_count'] or not result['reads']
                or args.mode == 'persisted' and result['post_load_parity']['status'] != 'pass'):
            raise SystemExit(1)
