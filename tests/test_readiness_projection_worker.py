"""A committed Spec intent can publish a durable generation without KG."""

import asyncio
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
from multiprocessing import get_context
import orjson
import sqlite3
import threading
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, create_current_schema_objects, seed_current_schema_fences,
)
from okto_pulse.community.adapters.readiness_schema_upgrade import upgrade_readiness_schema
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters import readiness_projection_worker as worker_module
from okto_pulse.community.adapters.sqlalchemy_persisted_readiness import (
    CommunityPersistedReadinessStore, ReadinessLeaseLost,
)
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError
from okto_pulse.core.services.persisted_delivery_readiness import (
    ProjectedUnit, build_readiness_generation, readiness_manifest_digest,
)
from test_readiness_schema_upgrade import _v401_predecessor


def test_projection_child_reuses_canonical_database_path(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, '_process_runner', None)
    monkeypatch.setattr(worker_module, '_process_engine', None)
    monkeypatch.setattr(worker_module, '_process_database_path', None)
    database_path = tmp_path / 'pulse.sqlite3'
    runner, engine = worker_module._projection_child_runtime(database_path)
    try:
        same_runner, same_engine = worker_module._projection_child_runtime(
            str(database_path.resolve()))
        assert same_runner is runner
        assert same_engine is engine
        with pytest.raises(ValueError, match='readiness_process_database_path_changed'):
            worker_module._projection_child_runtime(tmp_path / 'another.sqlite3')
    finally:
        runner.run(engine.dispose())
        runner.close()


async def _seed_one_spec(engine):
    async with engine.begin() as connection:
        await connection.run_sync(create_current_schema_objects)
        await connection.run_sync(seed_current_schema_fences)
        await connection.exec_driver_sql("INSERT INTO boards (id,name,owner_id,realm_id) "
            "VALUES ('board','Test','owner','local')")
        await connection.exec_driver_sql("INSERT INTO specs "
            "(id,board_id,title,status,version,created_by,architecture_adoption,execution_contract) "
            "VALUES ('spec','board','Spec','in_progress',1,'owner','{}',?)",
            (json.dumps({"contract_version": "spec-execution-contract/v1", "board_id": "board",
                "spec_id": "spec", "adopted_in_edition": 1, "actor_id": "owner",
                "origin": "new_spec"}),))


@pytest.mark.asyncio
async def test_projection_capture_reads_delta_with_source_revision(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        payload = json.dumps({"progress": {"contract_version": "delivery-progress/v2",
            "material_change": "unknown", "source_state": {
                "workspace_state": "unknown", "recoverability": "unknown"},
            "target_ids": [], "remaining": "Checkpoint"}, "justification": "Checkpoint"})
        async with engine.begin() as connection:
            await connection.exec_driver_sql("INSERT INTO cards "
                "(id,board_id,spec_id,title,status,position,created_by) "
                "VALUES ('card','board','spec','Card','in_progress',0,'owner')")
            await connection.exec_driver_sql("INSERT INTO cards "
                "(id,board_id,spec_id,title,status,position,created_by) "
                "VALUES ('other-card','board','spec','Other','in_progress',1,'owner')")
            await connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                "idempotency_key,payload_sha256,payload,created_at) "
                "VALUES ('other-progress','board','other-card','spec',1,'progress','agent','agent',"
                "'other-progress',?,?,CURRENT_TIMESTAMP)",
                (sha256(payload.encode()).hexdigest(), payload))
            await connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                "idempotency_key,payload_sha256,payload,created_at) "
                "VALUES ('progress-1','board','card','spec',1,'progress','agent','agent',"
                "'progress-1',?,?,CURRENT_TIMESTAMP)",
                (sha256(payload.encode()).hexdigest(), payload))
        _snapshot, revision, delta = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec',
            blocking_execution=worker.blocking_execution, include_source_delta=True)
        assert delta is not None
        assert (delta.source_revision, delta.kind, delta.card_id) == (
            revision, 'card_progress_insert', 'card')
        async with engine.begin() as connection:
            await connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                "idempotency_key,payload_sha256,payload,created_at) "
                "VALUES ('progress-2','board','card','spec',1,'progress','agent','agent',"
                "'progress-2',?,?,CURRENT_TIMESTAMP)",
                (sha256(payload.encode()).hexdigest(), payload))
        phases = {}
        cached, progress_revision, progress_delta = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution,
            include_source_delta=True, phase_metrics=phases)
        assert progress_delta is not None and progress_revision == revision + 1
        assert phases['readiness_materialize_facts_plan_cache_hit'] == 1.0
        assert phases['readiness_record_cache_hit'] == 1.0
        assert phases['readiness_record_cache_reused_rows'] >= 1
        cold_reader = type(worker.reader)(engine, clock=worker.reader.clock)
        cold, _, _ = await cold_reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution,
            include_source_delta=True)
        assert cached.snapshot_id == cold.snapshot_id
        assert cached.facts == cold.facts
        async with engine.begin() as connection:
            for index in (3, 4):
                await connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                    "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                    "idempotency_key,payload_sha256,payload,created_at) "
                    "VALUES (?,'board','card','spec',1,'progress','agent','agent',"
                    "?,?,?,CURRENT_TIMESTAMP)",
                    (f'progress-{index}', f'progress-{index}',
                        sha256(payload.encode()).hexdigest(), payload))
        phases = {}
        spanned, spanned_revision, _ = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution,
            include_source_delta=True, phase_metrics=phases)
        assert spanned_revision == progress_revision + 2
        assert phases['readiness_materialize_facts_plan_cache_hit'] == 1.0
        assert phases['readiness_record_cache_hit'] == 1.0
        assert phases['readiness_plan_cache_chain_length'] == 2.0
        cold_spanned, _, _ = await cold_reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution,
            include_source_delta=True)
        assert spanned.snapshot_id == cold_spanned.snapshot_id
        assert spanned.facts == cold_spanned.facts
        cache_key = ('board', 'spec', 1)
        cached_revision, cached_rows = worker.reader._projection_record_cache[cache_key]
        worker.reader._projection_record_cache[cache_key] = (cached_revision,
            tuple(replace(row, id='missing-cached-id') if row.id == 'progress-1'
                else row for row in cached_rows))
        async with engine.begin() as connection:
            await connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                "idempotency_key,payload_sha256,payload,created_at) "
                "VALUES ('progress-5','board','card','spec',1,'progress','agent','agent',"
                "'progress-5',?,?,CURRENT_TIMESTAMP)",
                (sha256(payload.encode()).hexdigest(), payload))
        phases = {}
        recovered, recovered_revision, _ = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution,
            include_source_delta=True, phase_metrics=phases)
        assert recovered_revision == spanned_revision + 1
        assert phases['readiness_materialize_facts_plan_cache_hit'] == 1.0
        assert phases['readiness_record_cache_hit'] == 0.0
        cold_recovered, _, _ = await cold_reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution,
            include_source_delta=True)
        assert recovered.snapshot_id == cold_recovered.snapshot_id
        assert recovered.facts == cold_recovered.facts
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET title='Changed' WHERE id='spec'")
            await connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                "idempotency_key,payload_sha256,payload,created_at) "
                "VALUES ('progress-6','board','card','spec',1,'progress','agent','agent',"
                "'progress-6',?,?,CURRENT_TIMESTAMP)",
                (sha256(payload.encode()).hexdigest(), payload))
        phases = {}
        _snapshot, next_revision, next_delta = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec',
            blocking_execution=worker.blocking_execution, include_source_delta=True,
            phase_metrics=phases)
        assert next_revision == recovered_revision + 2
        assert phases['readiness_materialize_facts_plan_cache_hit'] == 0.0
        assert phases['readiness_record_cache_hit'] == 0.0
        assert phases['readiness_plan_cache_chain_length'] == 2.0
        assert next_delta is not None
        assert (next_delta.source_revision, next_delta.kind, next_delta.card_id) == (
            next_revision, 'card_progress_insert', 'card')
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_skipped_progress_revisions_use_durable_chain_only_when_complete(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        async with engine.begin() as connection:
            await connection.exec_driver_sql("INSERT INTO cards "
                "(id,board_id,spec_id,title,status,position,created_by) "
                "VALUES ('card','board','spec','Card','in_progress',0,'owner')")
        await asyncio.sleep(.06)
        assert await worker.process_once() == 1
        baseline = await worker.store.active_predecessor_revision(
            scope_id='spec:1', board_id='board', spec_id='spec', edition=1,
            before_revision=1_000_000)
        assert baseline is not None
        assert await worker.store.gc_once() is True
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM readiness_source_delta_event "
                "WHERE scope_id='spec:1'")).scalar_one() == 0
        payload = json.dumps({"progress": {"contract_version": "delivery-progress/v2",
            "material_change": "unknown", "source_state": {
                "workspace_state": "unknown", "recoverability": "unknown"},
            "target_ids": [], "remaining": "Checkpoint"}, "justification": "Checkpoint"})
        async with engine.begin() as connection:
            for index in (1, 2):
                record_id = f'progress-{index}'
                await connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                    "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                    "idempotency_key,payload_sha256,payload,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (record_id, 'board', 'card', 'spec', 1, 'progress', 'agent',
                     'agent', record_id, sha256(payload.encode()).hexdigest(), payload))
        snapshot, revision, delta = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec',
            blocking_execution=worker.blocking_execution, include_source_delta=True)
        assert revision == baseline + 2
        chain = await worker.store.load_progress_delta_chain(
            scope_id='spec:1', after_revision=baseline, through_revision=revision)
        assert chain is not None
        assert await worker.store.gc_once() is False
        assert tuple((item.revision, item.kind, item.card_id) for item in chain) == (
            (baseline + 1, 'card_progress_insert', 'card'),
            (baseline + 2, 'card_progress_insert', 'card'))
        claim = SimpleNamespace(scope_id='spec:1', board_id='board',
            spec_id='spec', edition=1)
        candidate = await worker_module._candidate_predecessor(
            worker.store, claim, revision, delta)
        assert candidate is not None
        assert candidate[0].captured_revision == baseline
        assert candidate[1] == chain
        assert snapshot.facts.scope.spec_id == 'spec'
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "DELETE FROM readiness_source_delta_event "
                "WHERE scope_id='spec:1' AND source_revision=?",
                (baseline + 1,))
        assert await worker.store.load_progress_delta_chain(
            scope_id='spec:1', after_revision=baseline, through_revision=revision) is None
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO readiness_source_delta_event "
                "(scope_id,source_revision,kind,card_id) "
                "VALUES ('spec:1',?,'card_progress_insert','card')",
                (baseline + 1,))

        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET title='Changed' WHERE id='spec'")
            record_id = 'progress-3'
            await connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                "idempotency_key,payload_sha256,payload,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (record_id, 'board', 'card', 'spec', 1, 'progress', 'agent',
                 'agent', record_id, sha256(payload.encode()).hexdigest(), payload))
        _, mixed_revision, mixed_delta = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec',
            blocking_execution=worker.blocking_execution, include_source_delta=True)
        assert mixed_delta.kind == 'card_progress_insert'
        assert await worker.store.load_progress_delta_chain(
            scope_id='spec:1', after_revision=baseline,
            through_revision=mixed_revision) is None
        assert await worker_module._candidate_predecessor(
            worker.store, claim, mixed_revision, mixed_delta) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_predecessor_loader_verifies_active_manifest_and_row_keys(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        assert await worker.process_once() == 1
        visible = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert visible is not None and visible.captured_revision is not None
        phases = {}
        previous = await worker.store.load_verified_predecessor(
            scope_id='spec:1', board_id='board', spec_id='spec', edition=1,
            expected_revision=visible.captured_revision, phase_metrics=phases)
        assert previous is not None
        assert phases['readiness_predecessor_cache_hit'] == 1.0
        assert previous.manifest_sha256
        assert len(previous.summaries) == len(previous.diagnostics) == 1
        cold_phases = {}
        cold = await CommunityPersistedReadinessStore(engine).load_verified_predecessor(
            scope_id='spec:1', board_id='board', spec_id='spec', edition=1,
            expected_revision=visible.captured_revision, phase_metrics=cold_phases)
        assert cold is not None and cold.manifest_sha256 == previous.manifest_sha256
        assert cold_phases['readiness_predecessor_cache_hit'] == 0.0
        assert await worker.store.load_verified_predecessor(
            scope_id='spec:1', board_id='board', spec_id='spec', edition=1,
            expected_revision=visible.captured_revision + 1) is None
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE readiness_summary SET "
                "payload=json_set(payload,'$.card_id','wrong') "
                "WHERE generation_id=? AND card_key=''", (visible.generation_id,))
        assert await worker.store.load_verified_predecessor(
            scope_id='spec:1', board_id='board', spec_id='spec', edition=1,
            expected_revision=visible.captured_revision) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_verified_predecessor_cache_retains_only_current_scope_generation(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        assert await worker.process_once() == 1
        original = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET title='Changed' WHERE id='spec'")
        await asyncio.sleep(.06)
        assert await worker.process_once() == 1
        current = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert current.generation_id != original.generation_id
        assert tuple(worker.store._verified_generation_cache) == (current.generation_id,)
        phases = {}
        predecessor = await worker.store.load_verified_predecessor(
            scope_id='spec:1', board_id='board', spec_id='spec', edition=1,
            expected_revision=current.captured_revision, phase_metrics=phases)
        assert predecessor is not None
        assert phases['readiness_predecessor_cache_hit'] == 1.0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('submit_raises', [False, True])
async def test_broken_projection_process_falls_back_and_publishes(tmp_path, submit_raises):
    database = tmp_path / 'pulse.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')

    class BrokenExecutor:
        def submit(self, *_args, **_kwargs):
            if submit_raises:
                raise BrokenProcessPool('pool already broken')
            result = Future()
            result.set_exception(BrokenProcessPool('child stopped'))
            return result

    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
            full_projection_executor=BrokenExecutor(), database_path=database)
        assert await worker.process_once() == 1
        assert worker.full_projection_executor is None
        visible = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert visible is not None and visible.freshness == 'current'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pipeline_keeps_one_calculation_after_process_pool_breaks(tmp_path, monkeypatch):
    database = tmp_path / 'pulse.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')

    class BrokenExecutor:
        def submit(self, *_args, **_kwargs):
            raise BrokenProcessPool('pool unavailable')

    active = peak = 0
    guard = threading.Lock()
    original = worker_module.build_readiness_generation

    def observed_build(*args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.05)
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            for spec_id in ('y-spec', 'z-spec'):
                await connection.exec_driver_sql(
                    "INSERT INTO specs (id,board_id,title,status,version,created_by,"
                    "architecture_adoption,execution_contract) "
                    "VALUES (?,'board','Additional','in_progress',1,'owner','{}',?)",
                    (spec_id, json.dumps({"contract_version": "spec-execution-contract/v1",
                        "board_id": "board", "spec_id": spec_id, "adopted_in_edition": 1,
                        "actor_id": "owner", "origin": "new_spec"})),
                )
        await asyncio.sleep(.15)
        monkeypatch.setattr(worker_module, 'build_readiness_generation', observed_build)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
            full_projection_executor=BrokenExecutor(), database_path=database,
            max_inflight=2)
        assert await worker.process_once() == 2
        assert peak == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pipeline_cancellation_waits_for_both_inflight_calculations(tmp_path):
    database = tmp_path / 'pulse.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')

    class PausedExecutor:
        def __init__(self):
            self.futures = []

        def submit(self, *_args, **_kwargs):
            future = Future()
            self.futures.append(future)
            return future

    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            for spec_id in ('y-spec', 'z-spec'):
                await connection.exec_driver_sql(
                    "INSERT INTO specs (id,board_id,title,status,version,created_by,"
                    "architecture_adoption,execution_contract) "
                    "VALUES (?,'board','Additional','in_progress',1,'owner','{}',?)",
                    (spec_id, json.dumps({"contract_version": "spec-execution-contract/v1",
                        "board_id": "board", "spec_id": spec_id, "adopted_in_edition": 1,
                        "actor_id": "owner", "origin": "new_spec"})),
                )
        await asyncio.sleep(.15)
        executor = PausedExecutor()
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
            full_projection_executor=executor, database_path=database, max_inflight=2)
        processing = asyncio.create_task(worker.process_once())
        async with asyncio.timeout(5):
            while len(executor.futures) < 2:
                await asyncio.sleep(.01)
        processing.cancel()
        await asyncio.sleep(.05)
        assert not processing.done()
        for future in executor.futures:
            future.set_exception(RuntimeError('calculation stopped'))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(processing, timeout=5)
    finally:
        await engine.dispose()


def _seed_migrated_v401_many_specs(database):
    _v401_predecessor(database)
    spec_ids = ['spec', *[f'spec-{number:03d}' for number in range(100)]]
    contracts = [(spec_id, json.dumps({
        'contract_version': 'spec-execution-contract/v1',
        'board_id': 'board', 'spec_id': spec_id,
        'adopted_in_edition': 1, 'actor_id': 'owner', 'origin': 'new_spec',
    })) for spec_id in spec_ids]
    with closing(sqlite3.connect(database)) as predecessor:
        predecessor.execute("INSERT INTO boards (id,name,owner_id,realm_id) "
                            "VALUES ('board','Test','owner','local')")
        predecessor.executemany(
            "INSERT INTO specs (id,board_id,title,status,version,created_by,"
            "architecture_adoption,execution_contract) "
            "VALUES (?,'board','Spec','in_progress',1,'owner','{}',?)", contracts)
        predecessor.commit()
    backup = upgrade_readiness_schema(database)
    assert backup.is_file()
    return backup


@pytest.mark.asyncio
async def test_worker_publishes_one_complete_generation_after_source_commit(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.process_once() == 1
        first = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert first is not None and first.freshness == "current"
        assert first.source_snapshot_id and len(first.source_snapshot_id) == 64
        assert first.summaries[0]["implementation"]["percent"] is None
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET description='changed' WHERE id='spec'")
        delayed = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert delayed is not None and delayed.freshness == "updating"
        assert delayed.generation_id == first.generation_id
        assert delayed.source_snapshot_id == first.source_snapshot_id
        assert delayed.summaries == first.summaries
        await asyncio.sleep(0.06)  # The bounded debounce keeps burst writes coalesced.
        assert await worker.process_once() == 1
        current = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert current is not None and current.freshness == "current"
        assert current.generation_id != first.generation_id
        async with engine.connect() as connection:
            state = (await connection.exec_driver_sql("SELECT s.source_revision,s.active_generation_id,"
                "j.state,j.requested_revision FROM readiness_scope s JOIN readiness_job j "
                "ON j.scope_id=s.id WHERE s.spec_id='spec'")).one()
            assert state.source_revision == state.requested_revision == 2
            assert state.active_generation_id
            assert state.state == "idle"
            gen = (await connection.exec_driver_sql("SELECT state,summary_count,unit_count "
                "FROM readiness_generation WHERE id=?", (state.active_generation_id,))).one()
            assert gen == ("complete", 1, 0)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE readiness_generation "
                "SET created_at='2000-01-01 00:00:00' WHERE id=?", (first.generation_id,))
        assert await worker.store.gc_once()
        with pytest.raises(ReadinessReadError, match="readiness_cursor_stale"):
            await worker.store.read(board_id="board", spec_id="spec", edition=1,
                generation_id_override=first.generation_id)
        assert (await worker.store.read(board_id="board", spec_id="spec", edition=1)).generation_id == current.generation_id
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE readiness_job SET state='terminal',error_code='test' "
                "WHERE scope_id='spec:1'")
        requeued = await worker.store.requeue_terminal(scope_id="spec:1", board_id="board",
            actor_id="owner", actor_kind="human", expected_revision=2,
            idempotency_key="repair-one", reason="Retry after storage repair")
        assert requeued["scope"] == "spec:1" and requeued["requested_revision"] == 2
        assert requeued["queued"] and not requeued["replayed"] and requeued["audit_id"]
        replayed = await worker.store.requeue_terminal(scope_id="spec:1", board_id="board",
            actor_id="owner", actor_kind="human", expected_revision=2,
            idempotency_key="repair-one", reason="Retry after storage repair")
        assert replayed["replayed"] is True
        assert replayed["audit_id"] == requeued["audit_id"]
        with pytest.raises(ReadinessReadError, match="readiness_requeue_conflict"):
            await worker.store.requeue_terminal(scope_id="spec:1", board_id="board",
                actor_id="owner", actor_kind="human", expected_revision=1,
                idempotency_key="repair-two", reason="Wrong revision")
        with pytest.raises(ReadinessReadError, match="readiness_requeue_conflict"):
            await worker.store.requeue_terminal(scope_id="spec:1", board_id="board",
                actor_id="owner", actor_kind="human", expected_revision=2,
                idempotency_key="repair-one", reason="Conflicting replay")
        async with engine.connect() as connection:
            audit = (await connection.exec_driver_sql("SELECT operation,actor_id,previous_error_code,reason "
                "FROM readiness_operation_audit WHERE scope_id='spec:1'")).one()
            assert audit == ("requeue_terminal", "owner", "test", "Retry after storage repair")
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE readiness_summary SET payload='{}' "
                "WHERE generation_id=?", (current.generation_id,))
        assert not await worker.store.audit_active_generation(spec_id="spec")
        quarantined = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert quarantined is not None and quarantined.freshness == "pending_initial"
        assert quarantined.generation_id is None
        async with engine.connect() as connection:
            state = (await connection.exec_driver_sql("SELECT state,error_code FROM readiness_job "
                "WHERE scope_id='spec:1'")).one()
            assert state == ("pending", "readiness_active_manifest_mismatch")
        assert await worker.process_once() == 1
        repaired = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert repaired is not None and repaired.freshness == "current"
        assert repaired.generation_id != current.generation_id
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET description='new proof revision' "
                "WHERE id='spec'")
        await asyncio.sleep(0.06)
        stale = await worker.store.claim(owner="worker-one")
        assert stale is not None
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE readiness_job SET lease_until='2000-01-01' "
                "WHERE scope_id='spec:1'")
        fresh = await worker.store.claim(owner="worker-two")
        assert fresh is not None and fresh.epoch > stale.epoch
        with pytest.raises(ReadinessLeaseLost):
            await worker.store.heartbeat(stale)
        await worker.store.fail(stale, error_code="old_attempt")
        async with engine.connect() as connection:
            owner, state, error = (await connection.exec_driver_sql(
                "SELECT owner,state,error_code FROM readiness_job WHERE scope_id='spec:1'"
            )).one()
        assert (owner, state, error) == ("worker-two", "running", None)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tampered_diagnostic_quarantines_active_generation(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.process_once() == 1
        current = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert current is not None and current.freshness == "current"
        async with engine.begin() as connection:
            count = (await connection.exec_driver_sql(
                "SELECT count(*) FROM readiness_diagnostic WHERE generation_id=?",
                (current.generation_id,),
            )).scalar_one()
            assert count == 1
            await connection.exec_driver_sql(
                "UPDATE readiness_diagnostic SET payload='{}' WHERE generation_id=?",
                (current.generation_id,),
            )
        assert not await worker.store.audit_active_generation(spec_id="spec")
        quarantined = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert quarantined is not None and quarantined.freshness == "pending_initial"
        assert quarantined.generation_id is None
        async with engine.connect() as connection:
            state = (await connection.exec_driver_sql(
                "SELECT state,error_code FROM readiness_job WHERE scope_id='spec:1'",
            )).one()
            assert state == ("pending", "readiness_active_manifest_mismatch")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_projection_cpu_runs_after_snapshot_checkout_is_released(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        entered, resume = asyncio.Event(), asyncio.Event()

        class PausedExecution:
            calls = 0

            async def run(self, operation):
                self.calls += 1
                if self.calls == 1:
                    assert engine.sync_engine.pool.checkedout() == 0
                    entered.set()
                    await resume.wait()
                return await asyncio.to_thread(operation)

        worker = ReadinessProjectionProcessor(engine, PausedExecution())
        processing = asyncio.create_task(worker.process_once())
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert engine.sync_engine.pool.checkedout() == 0
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET description='committed during CPU' "
                "WHERE id='spec'")
        resume.set()
        assert await asyncio.wait_for(processing, timeout=10) == 1
        stale = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert stale is not None and stale.freshness == 'updating'
        assert stale.captured_revision == 1 and stale.source_revision == 2
        assert await worker.process_once() == 1
        current = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert current is not None and current.freshness == 'current'
        assert current.captured_revision == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_staging_manifest_never_becomes_active(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        claim = await worker.store.claim(owner='manifest-test')
        assert claim is not None
        snapshot, revision = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution)
        generation = build_readiness_generation(snapshot, captured_revision=revision)
        with pytest.raises(ValueError, match='readiness_stage_manifest_mismatch'):
            await worker.store.stage(claim, replace(generation, manifest_sha256='0' * 64))
        visible = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert visible is not None and visible.freshness == 'pending_initial'
        assert visible.generation_id is None
        async with engine.connect() as connection:
            state = (await connection.exec_driver_sql(
                "SELECT state,active_generation_id FROM readiness_scope s "
                "JOIN readiness_job j ON j.scope_id=s.id WHERE s.spec_id='spec'"
            )).one()
            assert state == ('running', None)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stage_verifier_checks_canonical_payload_and_typed_card_key(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        claim = await worker.store.claim(owner='canonical-stage-test')
        assert claim is not None
        snapshot, revision = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution)
        generation = build_readiness_generation(snapshot, captured_revision=revision)
        generation_id = await worker.store.stage(claim, generation)
        async with engine.begin() as connection:
            payload = (await connection.exec_driver_sql(
                "SELECT payload FROM readiness_summary WHERE generation_id=?",
                (generation_id,))).scalar_one()
            assert payload == orjson.dumps(orjson.loads(payload),
                option=orjson.OPT_SORT_KEYS).decode()
            await connection.exec_driver_sql(
                "UPDATE readiness_summary SET card_key='wrong' WHERE generation_id=?",
                (generation_id,))
        with pytest.raises(ValueError, match='readiness_stage_manifest_mismatch'):
            await worker.store.verify_stage(claim, generation_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_incremental_stage_copies_only_unchanged_units_and_verifies_payload(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        first_claim = await worker.store.claim(owner='copy-test')
        assert first_claim is not None
        snapshot, revision = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution)
        base = build_readiness_generation(snapshot, captured_revision=revision)
        unchanged = ProjectedUnit('card-a', 'obligation', 'ac:a', 'a' * 64, 1,
            ('card-a',), 'satisfied', 'satisfied', (), ())
        changed = ProjectedUnit('card-b', 'obligation', 'ac:b', 'b' * 64, 1,
            ('card-b',), 'missing', 'missing', (), ())

        def with_units(source, *, captured_revision, snapshot_id, units):
            digest = readiness_manifest_digest(board_id=source.board_id,
                spec_id=source.spec_id, edition=source.spec_edition,
                captured_revision=captured_revision, source_snapshot_id=snapshot_id,
                evaluator_version=source.evaluator_version,
                summaries=[row.payload() for row in source.summaries],
                units=[row.row() for row in units],
                diagnostics=[row.row() for row in source.diagnostics])
            return replace(source, captured_revision=captured_revision,
                source_snapshot_id=snapshot_id, units=units, manifest_sha256=digest)

        first = with_units(base, captured_revision=revision,
            snapshot_id=base.source_snapshot_id, units=(unchanged, changed))
        first_id = await worker.store.stage(first_claim, first)
        assert await worker.store.publish(first_claim, first_id)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET title='New source' WHERE id='spec'")
        await asyncio.sleep(.06)
        second_claim = await worker.store.claim(owner='copy-test')
        assert second_claim is not None
        second = with_units(base, captured_revision=revision + 1,
            snapshot_id='next-source-snapshot',
            units=(unchanged, replace(changed, implementation_state='satisfied')))
        phases = {}
        second_id = await worker.store.stage(second_claim, second,
            predecessor=first, phase_metrics=phases)
        assert phases['readiness_stage_copied_unit_count'] == 1
        assert phases['readiness_stage_rebuilt_unit_count'] == 1
        async with engine.connect() as connection:
            original = (await connection.exec_driver_sql(
                "SELECT payload FROM readiness_unit WHERE generation_id=? AND card_key='card-a'",
                (first_id,))).scalar_one()
            copied = (await connection.exec_driver_sql(
                "SELECT payload FROM readiness_unit WHERE generation_id=? AND card_key='card-a'",
                (second_id,))).scalar_one()
            assert copied == original
        assert await worker.store.publish(second_claim, second_id)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET title='Third source' WHERE id='spec'")
            await connection.exec_driver_sql(
                "UPDATE readiness_unit SET payload=json_set(payload,'$.implementation_state','failed') "
                "WHERE generation_id=? AND card_key='card-a'", (second_id,))
        await asyncio.sleep(.06)
        third_claim = await worker.store.claim(owner='copy-test')
        assert third_claim is not None
        third = with_units(base, captured_revision=revision + 2,
            snapshot_id='third-source-snapshot', units=second.units)
        with pytest.raises(ValueError, match='readiness_stage_manifest_mismatch'):
            await worker.store.stage(third_claim, third, predecessor=second)
        async with engine.connect() as connection:
            active_id = (await connection.exec_driver_sql(
                "SELECT active_generation_id FROM readiness_scope WHERE spec_id='spec'"
            )).scalar_one()
            assert active_id == second_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_process_verifier_rejects_tampered_stage_before_publication(tmp_path, caplog):
    database = tmp_path / 'pulse.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f'PRAGMA main.application_id={APPLICATION_ID}')
            await connection.exec_driver_sql(f'PRAGMA main.user_version={SCHEMA_VERSION}')
        with ProcessPoolExecutor(max_workers=1, mp_context=get_context('spawn')) as executor:
            worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
                full_projection_executor=executor, database_path=database)
            await asyncio.sleep(.06)
            claim = await worker.store.claim(owner=worker.owner)
            assert claim is not None
            snapshot, revision = await worker.reader.load_for_projection(
                board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution)
            generation = build_readiness_generation(snapshot, captured_revision=revision)
            with pytest.raises(ValueError, match='readiness_stage_manifest_mismatch'):
                await worker._stage_generation(claim, replace(generation,
                    manifest_sha256='0' * 64))
            visible = await worker.store.read(board_id='board', spec_id='spec', edition=1)
            assert visible is not None and visible.generation_id is None
            with caplog.at_level(logging.DEBUG, logger='okto_pulse.readiness_projection'):
                generation_id = await worker._stage_generation(claim, generation)
            phases = [record for record in caplog.records
                if record.msg == 'readiness.stage_phases']
            assert phases and phases[-1].readiness_stage_batch_count > 0
            assert phases[-1].readiness_stage_verify_ms >= 0
            assert await worker.store.publish(claim, generation_id)
            visible = await worker.store.read(board_id='board', spec_id='spec', edition=1)
            assert visible is not None and visible.freshness == 'current'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_process_projects_and_stages_before_parent_publish(tmp_path, caplog):
    database = tmp_path / 'pulse.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f'PRAGMA main.application_id={APPLICATION_ID}')
            await connection.exec_driver_sql(f'PRAGMA main.user_version={SCHEMA_VERSION}')
        with ProcessPoolExecutor(max_workers=1, mp_context=get_context('spawn')) as executor:
            worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
                full_projection_executor=executor, database_path=database)
            with caplog.at_level(logging.DEBUG, logger='okto_pulse.readiness_projection'):
                assert await worker.process_once() == 1
            visible = await worker.store.read(board_id='board', spec_id='spec', edition=1)
            assert visible is not None and visible.freshness == 'current'
            assert any(record.msg == 'readiness.stage_phases' for record in caplog.records)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_process_stage_cancellation_waits_for_child_result(tmp_path):
    database = tmp_path / 'pulse.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')

    class PausedExecutor:
        def __init__(self):
            self.future = None

        def submit(self, *_args, **_kwargs):
            self.future = Future()
            return self.future

    try:
        await _seed_one_spec(engine)
        executor = PausedExecutor()
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
            full_projection_executor=executor, database_path=database)
        await asyncio.sleep(.06)
        claim = await worker.store.claim(owner=worker.owner)
        assert claim is not None
        staging = asyncio.create_task(worker._stage_generation(claim, object()))
        async with asyncio.timeout(5):
            while executor.future is None:
                await asyncio.sleep(.01)
        staging.cancel()
        await asyncio.sleep(.05)
        assert not staging.done()
        executor.future.set_result('stage-completed')
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(staging, timeout=5)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_incompatible_evaluator_generation_cannot_publish(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        claim = await worker.store.claim(owner='evaluator-test')
        assert claim is not None
        snapshot, revision = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution)
        generation = build_readiness_generation(snapshot, captured_revision=revision)
        future_version = 'delivery-readiness-evaluator/future'
        future_digest = readiness_manifest_digest(
            board_id=generation.board_id, spec_id=generation.spec_id,
            edition=generation.spec_edition, captured_revision=generation.captured_revision,
            source_snapshot_id=generation.source_snapshot_id,
            evaluator_version=future_version,
            summaries=[summary.payload() for summary in generation.summaries],
            units=[asdict(unit) for unit in generation.units],
            diagnostics=[row.row() for row in generation.diagnostics],
        )
        incompatible = replace(generation, evaluator_version=future_version,
            manifest_sha256=future_digest)
        staged = await worker.store.stage(claim, incompatible)
        assert not await worker.store.publish(claim, staged)
        visible = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert visible is not None and visible.generation_id is None
        assert visible.freshness == 'pending_initial'
        good_staged = await worker.store.stage(claim, generation)
        assert await worker.store.publish(claim, good_staged)
        current = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert current is not None and current.generation_id == good_staged
        assert current.freshness == 'current'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_older_snapshot_publishes_as_updating_without_losing_newer_debt(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        claim = await worker.store.claim(owner='slow-worker')
        assert claim is not None
        snapshot, revision = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution)
        generation = build_readiness_generation(snapshot, captured_revision=revision)
        staged = await worker.store.stage(claim, generation)
        before = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert before is not None and before.generation_id is None
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET description='newer source' WHERE id='spec'")
        assert await worker.store.publish(claim, staged)
        intermediate = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert intermediate is not None and intermediate.generation_id == staged
        assert intermediate.freshness == 'updating'
        async with engine.connect() as connection:
            state = (await connection.exec_driver_sql(
                "SELECT s.source_revision,j.requested_revision,j.state "
                "FROM readiness_scope s JOIN readiness_job j ON j.scope_id=s.id "
                "WHERE s.id='spec:1'" )).one()
        assert state[0] == state[1] == revision + 1
        assert state[2] == 'pending'
        await asyncio.sleep(.06)
        assert await worker.process_once() == 1
        current = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert current is not None and current.freshness == 'current'
        assert current.generation_id != staged
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_round_robin_survives_restart_and_failing_scope(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            for spec_id in ('a-spec', 'z-spec'):
                await connection.exec_driver_sql(
                    "INSERT INTO specs (id,board_id,title,status,version,created_by,"
                    "architecture_adoption,execution_contract) "
                    "VALUES (?,'board','Spec','in_progress',1,'owner','{}',?)",
                    (spec_id, json.dumps({"contract_version": "spec-execution-contract/v1",
                        "board_id": "board", "spec_id": spec_id, "adopted_in_edition": 1,
                        "actor_id": "owner", "origin": "new_spec"})),
                )
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        claimed = []
        for _ in range(3):
            claim = await worker.store.claim(owner='round-robin-test')
            assert claim is not None
            claimed.append(claim.spec_id)
            await worker.store.fail(claim, error_code='synthetic_failure')
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "UPDATE readiness_job SET due_at='2000-01-01 00:00:00' WHERE scope_id=?",
                    (claim.scope_id,),
                )
        assert claimed == ['a-spec', 'spec', 'z-spec']
        async with engine.connect() as connection:
            cursor = (await connection.exec_driver_sql(
                "SELECT value FROM app_settings WHERE key='readiness_claim_cursor'"
            )).scalar_one()
        assert cursor == 'z-spec:1'
        await engine.dispose()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
        restarted = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        next_claim = await restarted.store.claim(owner='after-restart')
        assert next_claim is not None and next_claim.spec_id == 'a-spec'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_crash_replay_keeps_source_and_hides_unpublished_staging(tmp_path):
    database = tmp_path / 'pulse.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        abandoned = await worker.store.claim(owner='crashed-worker')
        assert abandoned is not None
        snapshot, revision = await worker.reader.load_for_projection(
            board_id='board', spec_id='spec', blocking_execution=worker.blocking_execution)
        staged_id = await worker.store.stage(abandoned,
            build_readiness_generation(snapshot, captured_revision=revision))
        before = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert before is not None and before.generation_id is None
        async with engine.connect() as connection:
            source_before = (await connection.exec_driver_sql(
                "SELECT version,description FROM specs WHERE id='spec'")).one()
        await engine.dispose()

        # A dead process cannot renew its lease. Reopening the same durable DB
        # must steal the claim with a newer epoch and ignore its staged rows.
        engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE readiness_job SET lease_until='2000-01-01' WHERE scope_id='spec:1'")
        restarted = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await restarted.process_once() == 1
        current = await restarted.store.read(board_id='board', spec_id='spec', edition=1)
        assert current is not None and current.freshness == 'current'
        assert current.generation_id != staged_id
        assert not await restarted.store.publish(abandoned, staged_id)
        async with engine.connect() as connection:
            source_after = (await connection.exec_driver_sql(
                "SELECT version,description FROM specs WHERE id='spec'")).one()
            states = (await connection.exec_driver_sql(
                "SELECT id,state FROM readiness_generation WHERE scope_id='spec:1'")).all()
        assert source_after == source_before
        # The bounded GC may expire the abandoned staging during recovery.
        assert (staged_id, 'staging') in states or (staged_id, 'expired') in states
        assert (current.generation_id, 'complete') in states

        # A response lost after publication is an idle job on restart.
        await engine.dispose()
        engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
        again = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await again.process_once() == 0
        replay = await again.store.read(board_id='board', spec_id='spec', edition=1)
        assert replay is not None and replay.generation_id == current.generation_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_eight_failures_terminal_then_fenced_admin_requeue(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        await asyncio.sleep(.06)
        for number in range(1, 9):
            claim = await worker.store.claim(owner=f'failure-{number}')
            assert claim is not None and claim.requested_revision == 1
            before_failure = datetime.now(timezone.utc)
            await worker.store.fail(claim, error_code='storage_unavailable')
            after_failure = datetime.now(timezone.utc)
            async with engine.connect() as connection:
                row = (await connection.exec_driver_sql(
                    "SELECT state,attempt,requested_revision,error_code,due_at "
                    "FROM readiness_job WHERE scope_id='spec:1'")).one()
            assert row.state == ('terminal' if number == 8 else 'pending')
            assert row.attempt == number and row.requested_revision == 1
            assert row.error_code == 'storage_unavailable'
            due = datetime.fromisoformat(row.due_at)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            backoff = min(30, 2 ** (number - 1))
            assert due >= before_failure + timedelta(seconds=backoff)
            assert due <= after_failure + timedelta(seconds=backoff * 1.2)
            assert await worker.store.claim(owner='early') is None
            if number < 8:
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(
                        "UPDATE readiness_job SET due_at='2000-01-01' "
                        "WHERE scope_id='spec:1'")
        with pytest.raises(ReadinessReadError, match='readiness_requeue_conflict'):
            await worker.store.requeue_terminal(scope_id='spec:1', board_id='board',
                actor_id='owner', actor_kind='human', expected_revision=0,
                idempotency_key='wrong', reason='Storage restored')
        outcome = await worker.store.requeue_terminal(scope_id='spec:1', board_id='board',
            actor_id='owner', actor_kind='human', expected_revision=1,
            idempotency_key='repair', reason='Storage restored')
        assert outcome['queued'] and not outcome['replayed'] and outcome['audit_id']
        replay = await worker.store.requeue_terminal(scope_id='spec:1', board_id='board',
            actor_id='owner', actor_kind='human', expected_revision=1,
            idempotency_key='repair', reason='Storage restored')
        assert replay['replayed'] and replay['audit_id'] == outcome['audit_id']
        async with engine.connect() as connection:
            row = (await connection.exec_driver_sql(
                "SELECT state,attempt,requested_revision,error_code FROM readiness_job "
                "WHERE scope_id='spec:1'")).one()
            audit = (await connection.exec_driver_sql(
                "SELECT count(*),max(previous_error_code) FROM readiness_operation_audit "
                "WHERE scope_id='spec:1'")).one()
        assert row == ('pending', 0, 1, None)
        assert audit == (1, 'storage_unavailable')
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stage_storage_failure_keeps_previous_generation_until_repair(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.process_once() == 1
        previous = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert previous is not None and previous.freshness == 'current'
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET description='new evidence' WHERE id='spec'")
        await asyncio.sleep(.06)
        original = worker.store.stage

        async def disk_full(*_args, **_kwargs):
            raise OSError('database or disk is full')

        monkeypatch.setattr(worker.store, 'stage', disk_full)
        assert await worker.process_once() == 0
        failed = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert failed is not None and failed.generation_id == previous.generation_id
        assert failed.freshness == 'updating' and failed.error_code == 'OSError'
        monkeypatch.setattr(worker.store, 'stage', original)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE readiness_job SET due_at='2000-01-01' WHERE scope_id='spec:1'")
        assert await worker.process_once() == 1
        repaired = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert repaired is not None and repaired.freshness == 'current'
        assert repaired.generation_id != previous.generation_id
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT version,description FROM specs WHERE id='spec'")).one() == (1, 'new evidence')
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sustained_queue_lag_is_warned_without_exposing_payload(tmp_path, monkeypatch, caplog):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        async def reconciled():
            return 0
        async def queue():
            return {'counts': {'pending': 1}, 'oldest_s': 66.0}
        async def no_claim(*, owner):
            return None
        monkeypatch.setattr(worker.store, 'reconcile_once', reconciled)
        monkeypatch.setattr(worker.store, 'queue_status', queue)
        monkeypatch.setattr(worker.store, 'claim', no_claim)
        monkeypatch.setattr(worker.store, 'gc_once', reconciled)
        with caplog.at_level('WARNING', logger='okto_pulse.readiness_projection'):
            assert await worker.process_once() == 0
        assert 'readiness.queue_lag oldest_s=66.000' in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_audits_each_active_generation_in_a_page(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO specs (id,board_id,title,status,version,created_by,"
                "architecture_adoption,execution_contract) "
                "VALUES ('z-spec','board','Other','in_progress',1,'owner','{}',?)",
                (json.dumps({"contract_version": "spec-execution-contract/v1",
                    "board_id": "board", "spec_id": "z-spec", "adopted_in_edition": 1,
                    "actor_id": "owner", "origin": "new_spec"}),),
            )
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.process_once() == 1
        assert await worker.process_once() == 1
        first = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        last = await worker.store.read(board_id="board", spec_id="z-spec", edition=1)
        assert first is not None and first.freshness == "current"
        assert last is not None and last.freshness == "current"

        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE readiness_summary SET payload='{}' WHERE generation_id=?",
                (first.generation_id,),
            )
        # The cursor can be at the end of the page after startup. The next
        # sweep must eventually inspect both active manifests, not only the
        # lexicographically last Spec.
        assert await worker.store.reconcile_once() == 0
        assert await worker.store.reconcile_once() == 2
        repaired = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert repaired is not None and repaired.generation_id is None
        assert repaired.freshness == "pending_initial"
        async with engine.connect() as connection:
            job = (await connection.exec_driver_sql(
                "SELECT state,error_code FROM readiness_job WHERE scope_id='spec:1'"
            )).one()
        assert job == ("pending", "readiness_active_manifest_mismatch")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_cursor_resumes_bounded_pages_after_restart(tmp_path):
    database = tmp_path / 'pulse.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
    try:
        await _seed_one_spec(engine)
        rows = []
        for number in range(103):
            spec_id = f'spec-{number:03d}'
            rows.append((spec_id, json.dumps({
                'contract_version': 'spec-execution-contract/v1',
                'board_id': 'board', 'spec_id': spec_id,
                'adopted_in_edition': 1, 'actor_id': 'owner', 'origin': 'new_spec',
            })))
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO specs (id,board_id,title,status,version,created_by,"
                "architecture_adoption,execution_contract) "
                "VALUES (?,'board','Spec','in_progress',1,'owner','{}',?)", rows)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.store.reconcile_once(limit=100) == 100
        async with engine.connect() as connection:
            cursor = (await connection.exec_driver_sql(
                "SELECT value FROM app_settings WHERE key='readiness_reconcile_cursor'"
            )).scalar_one()
        assert cursor == 'spec-098'

        # A new source write to a scope already visited by backfill must keep
        # its own revision and pending work across the worker restart.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE specs SET title='Reported during backfill' WHERE id='spec-050'")
        async with engine.connect() as connection:
            revision = (await connection.exec_driver_sql(
                "SELECT source_revision FROM readiness_scope WHERE id='spec-050:1'"
            )).scalar_one()
            requested = (await connection.exec_driver_sql(
                "SELECT requested_revision FROM readiness_job WHERE scope_id='spec-050:1'"
            )).scalar_one()
        assert revision >= 1 and requested == revision

        await engine.dispose()
        engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
        resumed = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await resumed.store.reconcile_once(limit=100) == 4
        async with engine.connect() as connection:
            cursor = (await connection.exec_driver_sql(
                "SELECT value FROM app_settings WHERE key='readiness_reconcile_cursor'"
            )).scalar_one()
            jobs = (await connection.exec_driver_sql(
                "SELECT count(*) FROM readiness_job")).scalar_one()
        assert cursor == 'spec-102' and jobs == 104
        assert await resumed.store.reconcile_once(limit=100) == 0
        assert await resumed.store.reconcile_once(limit=100) == 100
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT requested_revision FROM readiness_job WHERE scope_id='spec-050:1'"
            )).scalar_one() == revision
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_after_v401_upgrade_completes_all_scopes_and_three_restarts(tmp_path):
    database = tmp_path / 'pulse.sqlite3'
    _seed_migrated_v401_many_specs(database)
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
    try:
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.store.reconcile_once(limit=100) == 100
        pending = await worker.store.read(board_id='board', spec_id='spec-050', edition=1)
        assert pending is not None and pending.freshness == 'pending_initial'
        assert pending.generation_id is None and pending.summaries == ()

        # A restart resumes the persisted cursor rather than starting another
        # full scan. The final scope is discovered in a second bounded page.
        await engine.dispose()
        engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.store.reconcile_once(limit=100) == 1
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT count(*) FROM readiness_job")).scalar_one() == 101
            assert (await connection.exec_driver_sql(
                "SELECT count(DISTINCT scope_id) FROM readiness_job")).scalar_one() == 101

        processed = 0
        initial_attempts = 0
        while processed < 25 and initial_attempts < 100:
            processed += await worker.process_once()
            initial_attempts += 1
            if processed < 25:
                await asyncio.sleep(0.06)
        assert processed == 25
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE specs SET title='Report during backfill' WHERE id='spec-050'")
        await engine.dispose()
        engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        attempts = 0
        while attempts < 250:
            processed += await worker.process_once()
            attempts += 1
            if attempts == 35:
                await engine.dispose()
                engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
                worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
            if attempts % 10 == 0 or processed >= 101:
                async with engine.connect() as connection:
                    complete = (await connection.exec_driver_sql(
                        "SELECT count(*) FROM readiness_scope s JOIN readiness_job j "
                        "ON j.scope_id=s.id WHERE s.active_generation_id IS NOT NULL "
                        "AND j.state='idle' AND j.requested_revision=s.source_revision"
                    )).scalar_one()
                if complete == 101:
                    break
            await asyncio.sleep(0.06)
        assert processed >= 101
        async with engine.connect() as connection:
            rows = (await connection.exec_driver_sql(
                "SELECT s.spec_id,s.source_revision,s.active_generation_id,"
                "j.requested_revision,j.state FROM readiness_scope s "
                "JOIN readiness_job j ON j.scope_id=s.id ORDER BY s.spec_id"
            )).all()
        assert len(rows) == 101
        assert all(row.active_generation_id and row.state == 'idle' and
                   row.requested_revision == row.source_revision for row in rows)
        assert next(row for row in rows if row.spec_id == 'spec-050').source_revision >= 2
        for row in rows:
            visible = await worker.store.read(board_id='board', spec_id=row.spec_id, edition=1)
            assert visible is not None and visible.freshness == 'current'
            assert visible.generation_id == row.active_generation_id
            assert visible.summaries[0]['implementation']['percent'] is None
            assert visible.summaries[0]['verification']['percent'] is None
    finally:
        await engine.dispose()


def test_real_community_lifespan_serves_pending_then_current_on_upgraded_copy(
    tmp_path, monkeypatch,
):
    data_home = tmp_path / 'pulse-home'
    database = data_home / 'data' / 'pulse.db'
    database.parent.mkdir(parents=True)
    _seed_migrated_v401_many_specs(database)
    monkeypatch.setenv('DATA_DIR', str(data_home))
    monkeypatch.setenv('KG_EMBEDDING_MODE', 'stub')
    monkeypatch.setenv('KG_DAILY_TICK_DISABLED', '1')

    from okto_pulse.community.main import create_community_app
    from okto_pulse.community.api.auth_deps import require_principal
    from okto_pulse.core.ports.authentication import Principal
    from okto_pulse.core.ports.permission_policy import registered_permission_flags

    start_worker = threading.Event()
    real_process_once = ReadinessProjectionProcessor.process_once

    async def held_process_once(self):
        await asyncio.to_thread(start_worker.wait)
        return await real_process_once(self)

    monkeypatch.setattr(ReadinessProjectionProcessor, 'process_once', held_process_once)
    app = create_community_app()
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject='owner', realm_id='local', actor_kind='human',
        claims={'permissions': registered_permission_flags()},
    )
    endpoint = '/api/v1/boards/board/specs/spec-000/delivery-readiness-summary'
    with TestClient(app) as client:
        try:
            assert client.get('/').status_code == 200
            pending = client.get(endpoint)
            assert pending.status_code == 200, pending.text
            assert pending.json()['projection']['freshness'] == 'pending_initial'
            start_worker.set()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = client.get(endpoint)
                assert current.status_code == 200, current.text
                if current.json()['projection']['freshness'] == 'current':
                    assert current.json()['delivery_readiness']['implementation']['percent'] is None
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('migrated server did not publish a current generation')
        finally:
            start_worker.set()


@pytest.mark.asyncio
async def test_unrelated_board_commit_does_not_retry_projection_snapshot(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("INSERT INTO boards (id,name,owner_id,realm_id) "
                "VALUES ('other','Other','owner','local')")
        entered, resume = asyncio.Event(), asyncio.Event()

        class PausedExecution:
            calls = 0

            async def run(self, operation):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    await resume.wait()
                return await asyncio.to_thread(operation)

        worker = ReadinessProjectionProcessor(engine, PausedExecution())
        processing = asyncio.create_task(worker.process_once())
        await asyncio.wait_for(entered.wait(), timeout=10)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE boards SET name='Changed' WHERE id='other'")
        resume.set()
        assert await asyncio.wait_for(processing, timeout=10) == 1
        result = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert result is not None and result.freshness == 'current'
        assert result.captured_revision == result.source_revision == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_many_source_revisions_coalesce_into_one_generation(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            for index in range(20):
                await connection.exec_driver_sql(
                    "UPDATE specs SET description=? WHERE id='spec'", (f'revision-{index}',),
                )
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.process_once() == 1
        result = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        assert result is not None and result.freshness == 'current'
        assert result.captured_revision == result.source_revision == 21
        async with engine.connect() as connection:
            generations = (await connection.exec_driver_sql(
                "SELECT count(*) FROM readiness_generation WHERE scope_id='spec:1'"
            )).scalar_one()
        assert generations == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_calculation_worker_pipelines_only_when_multiple_scopes_are_pending(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        for spec_id in ('y-spec', 'z-spec'):
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO specs (id,board_id,title,status,version,created_by,"
                    "architecture_adoption,execution_contract) "
                    "VALUES (?,'board','Additional','in_progress',1,'owner','{}',?)",
                    (spec_id, json.dumps({"contract_version": "spec-execution-contract/v1",
                        "board_id": "board", "spec_id": spec_id, "adopted_in_edition": 1,
                        "actor_id": "owner", "origin": "new_spec"})),
                )
        await asyncio.sleep(.15)
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(), max_inflight=2)
        assert await worker.process_once() == 2
        assert await worker.process_once() == 1
        for spec_id in ('spec', 'y-spec', 'z-spec'):
            result = await worker.store.read(board_id='board', spec_id=spec_id, edition=1)
            assert result is not None and result.freshness == 'current'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pipeline_returns_to_maintenance_under_continuous_backlog():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    try:
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution(),
            max_inflight=2)

        class ContinuousBacklog:
            claims = 0

            async def claim(self, *, owner):
                self.claims += 1
                return self.claims

            async def eligible_pending_count(self):
                return 2

        async def complete(claim):
            if claim == 1:
                await asyncio.sleep(.2)
            return 1

        async def no_maintenance():
            return None

        worker.store = ContinuousBacklog()
        worker._run_claim = complete
        worker._maintenance_once = no_maintenance
        assert await worker.process_once() == worker_module.PIPELINE_BATCH_CLAIMS
        assert worker.store.claims == worker_module.PIPELINE_BATCH_CLAIMS
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_over_cap_scope_is_unknown_while_healthy_scope_progresses(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        await _seed_one_spec(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE specs SET test_scenarios=? WHERE id='spec'",
                (json.dumps([f'scenario-{index}' for index in range(5001)]),),
            )
            await connection.exec_driver_sql(
                "INSERT INTO specs (id,board_id,title,status,version,created_by,"
                "architecture_adoption,execution_contract) "
                "VALUES ('z-spec','board','Healthy','in_progress',1,'owner','{}',?)",
                (json.dumps({"contract_version": "spec-execution-contract/v1",
                    "board_id": "board", "spec_id": "z-spec", "adopted_in_edition": 1,
                    "actor_id": "owner", "origin": "new_spec"}),),
            )
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.process_once() == 1
        assert await worker.process_once() == 1
        limited = await worker.store.read(board_id='board', spec_id='spec', edition=1)
        healthy = await worker.store.read(board_id='board', spec_id='z-spec', edition=1)
        assert limited is not None and limited.freshness == 'current'
        assert limited.summaries[0]['implementation']['status'] == 'unknown'
        assert limited.summaries[0]['implementation']['percent'] is None
        assert limited.summaries[0]['verification']['status'] == 'unknown'
        assert limited.units == ()
        assert healthy is not None and healthy.freshness == 'current'
    finally:
        await engine.dispose()
