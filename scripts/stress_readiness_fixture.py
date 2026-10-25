"""Concurrent isolated readiness load, writes, and publication-lag probe.

This uses the Spec's population fixture. The candidate read includes service
authorization and JSON serialization; the legacy read remains an internal
source snapshot. Writer latency is split into lock wait and transaction time.
Publication targets share one bounded polling query; the engine uses the
Community SQLite PRAGMAs. This isolated harness does not claim REST/UI SLOs.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import platform
import psutil
import sqlite3
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter, process_time

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from benchmark_readiness_fixture import seed, percentile, WORKSPACE
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_database import install_community_sqlite_pragmas
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import (
    CommunityDeliveryReadinessReader, CommunityReadinessClock,
)
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.core.ports.delivery_readiness import ReadinessActor
from okto_pulse.core.services.delivery_readiness import DeliveryReadinessService

ACTOR = ReadinessActor('benchmark-agent', 'agent', 'local', 'benchmark-board')


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
    worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
    reader = CommunityDeliveryReadinessReader(engine, clock=CommunityReadinessClock())
    service = DeliveryReadinessService(reader, None)
    read_ms, write_ms, lag_ms, burst_write_ms, burst_lag_ms = [], [], [], [], []
    lock_wait_ms, transaction_ms, burst_lock_wait_ms, burst_transaction_ms = [], [], [], []
    source_update_ms, source_commit_ms = [], []
    errors: list[str] = []
    warmup_errors: list[str] = []
    freshness: dict[str, int] = {}
    measuring = args.warmup_seconds == 0
    read_phases_ms: dict[str, list[float]] = {
        'auth_before': [], 'projection': [], 'auth_after': [],
    }
    publish_phases_ms: dict[str, list[float]] = {
        'snapshot': [], 'compute': [], 'stage': [], 'publish': [],
    }

    class ReadPhaseHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if not measuring:
                return
            if record.msg == 'readiness.persisted_read_phases':
                read_phases_ms['auth_before'].append(record.readiness_auth_before_ms)
                read_phases_ms['projection'].append(record.readiness_projection_ms)
                read_phases_ms['auth_after'].append(record.readiness_auth_after_ms)
            elif isinstance(record.msg, str) and record.msg.startswith('readiness.published '):
                for name, value in zip(publish_phases_ms, record.args[4:8]):
                    publish_phases_ms[name].append(float(value))

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
    publish_logger.setLevel(logging.INFO)
    stop_load = asyncio.Event()
    stop_worker = asyncio.Event()
    writer_guard = asyncio.Lock()
    pending_observations: dict[int, tuple[str, int, float, list[float]]] = {}
    next_observation_id = 0
    worker_publications = 0
    burst_elapsed_ms = None
    rss_bytes, pool_checkouts, blocking_tasks, wal_samples = [], [], [], []
    cpu_start = process_time()
    try:
        if args.mode == 'persisted':
            published = 0
            while published < args.specs:
                published += await worker.process_once()
                await asyncio.sleep(.06)
        # Exclude connection/thread cold start from steady-state write latency.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE specs SET description='warmup' WHERE id='benchmark-spec-000'")
        if args.mode == 'persisted':
            await asyncio.sleep(.06)
            await worker.process_once()

        async def projection_loop():
            nonlocal worker_publications
            while not stop_worker.is_set():
                processed = 0
                try:
                    processed = await worker.process_once()
                    worker_publications += processed
                except Exception as exc:
                    (errors if measuring else warmup_errors).append(f'worker:{type(exc).__name__}')
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
                        if args.read_surface == 'service':
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
                                await connection.exec_driver_sql(
                                    'UPDATE specs SET description=? WHERE id=?',
                                    (f'{"burst" if burst else "load"}-{index}', spec_id))
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
            index = 0
            while not stop_load.is_set():
                await record_write(index)
                index += 1
                await asyncio.sleep(max(0, started_at + index - monotonic()))

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
            process = psutil.Process()
            wal = path.with_name(path.name + '-wal')
            while not stop_load.is_set():
                if measuring:
                    rss_bytes.append(process.memory_info().rss)
                    pool_checkouts.append(engine.sync_engine.pool.checkedout())
                    blocking_tasks.append(worker.blocking_execution.pending_count)
                    wal_samples.append(wal.stat().st_size if wal.exists() else 0)
                await asyncio.sleep(.5)

        started_at = monotonic()
        projection_task = asyncio.create_task(projection_loop()) if args.mode == 'persisted' else None
        observation_task = asyncio.create_task(observation_loop()) if args.mode == 'persisted' else None
        tasks = [asyncio.create_task(writer_loop(started_at)),
            *(asyncio.create_task(reader_loop(index)) for index in range(args.clients)),
            asyncio.create_task(resource_loop())]
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
        if projection_task is not None:
            await projection_task
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
            'read_surface': args.read_surface,
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
            'reads': len(read_ms), 'writes': len(write_ms), 'errors': errors[:25],
            'worker_publications': worker_publications,
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
                'wal_max_bytes': max(wal_samples, default=0)},
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
                'blocking_tasks': blocking_tasks, 'wal_bytes': wal_samples},
        }
    finally:
        phase_logger.removeHandler(phase_handler)
        phase_logger.setLevel(previous_log_level)
        phase_logger.propagate = previous_propagate
        publish_logger.removeHandler(phase_handler)
        publish_logger.setLevel(previous_publish_level)
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['legacy', 'persisted'], required=True)
    parser.add_argument('--read-surface', choices=['internal', 'service'])
    parser.add_argument('--duration-seconds', type=int, default=60)
    parser.add_argument('--warmup-seconds', type=int, default=0)
    parser.add_argument('--reads-per-second', type=int, default=10)
    parser.add_argument('--clients', type=int, default=5)
    parser.add_argument('--specs', type=int, default=10)
    parser.add_argument('--cards', type=int, default=100)
    parser.add_argument('--obligations', type=int, default=100)
    parser.add_argument('--scenarios', type=int, default=200)
    parser.add_argument('--facts', type=int, default=2000)
    parser.add_argument('--history', type=int, default=50)
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
        result = asyncio.run(run(path, args))
        result['fixture_seed'] = 'readiness-deterministic-v2'
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
