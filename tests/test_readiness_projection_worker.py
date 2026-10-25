"""A committed Spec intent can publish a durable generation without KG."""

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    create_current_schema_objects, seed_current_schema_fences,
)
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_persisted_readiness import ReadinessLeaseLost
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError
from okto_pulse.core.services.persisted_delivery_readiness import (
    build_readiness_generation, readiness_manifest_digest,
)


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
    finally:
        await engine.dispose()


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
