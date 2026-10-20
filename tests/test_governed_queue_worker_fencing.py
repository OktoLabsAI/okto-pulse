"""Card 4 -- durable worker fencing and compare-and-delete acknowledgements."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone,
    Board,
    ConsolidationQueue,
    DomainEventRow,
)
from okto_pulse.core.ports.consolidation import (
    ConsolidationClaimScope,
    register_consolidation_persistence_port,
    reset_consolidation_persistence_port_for_tests,
)


BOARD_ID = "card4-worker-fence-board"
ARTIFACT_ID = "card4-deleted-spec"


@pytest.mark.asyncio
async def test_worker_fence_blocks_investigation_on_an_unrelated_board(queue_store):
    """Reproduce E2E contention with the real fence, without a slow graph run."""
    factory, adapter = queue_store
    async with factory() as seed:
        seed.add(Board(realm_id="local", id="unrelated-board", name="Other", owner_id="tester"))
        seed.add(_queue_row(row_id="writer-probe", work_kind="consolidate",
                            generation=0, delete_event_id=None, claim_token="token"))
        await seed.commit()
    async with factory() as worker, factory() as author:
        await author.execute(text("PRAGMA busy_timeout=50"))
        assert await adapter.queue_claim_is_current_and_unfenced(
            worker, entry_id="writer-probe", claim_token="token", board_id=BOARD_ID,
            artifact_type="spec", artifact_id=ARTIFACT_ID, work_kind="consolidate",
            source="test", generation=0, delete_event_id=None,
        )
        # Identical write-fence SQL used by Code Investigation admission.
        admission = text("UPDATE boards SET id = id WHERE id = :board_id")
        with pytest.raises(OperationalError) as failure:
            await author.execute(admission, {"board_id": "unrelated-board"})
        assert failure.value.orig.sqlite_errorcode & 0xFF == 5
        await author.rollback()
        await worker.rollback()
        assert (await author.execute(admission, {"board_id": "unrelated-board"})).rowcount == 1
        await author.rollback()


@pytest.mark.asyncio
async def test_prepared_projection_releases_writer_and_survives_claim_replacement(queue_store):
    factory, adapter = queue_store
    operation_id = "80f7de89-6d3b-48c4-b00c-7bf6c764e47a"
    async with factory() as seed:
        seed.add(Board(realm_id="local", id="projection-other-board",
                       name="Other", owner_id="tester"))
        seed.add(_queue_row(row_id="projection-queue", work_kind="consolidate",
                            generation=0, delete_event_id=None,
                            claim_token="projection-claim"))
        await seed.commit()
    async with factory() as worker, factory() as author:
        assert await adapter.prepare_projection_operation(
            worker, operation_id=operation_id, entry_id="projection-queue",
            claim_token="projection-claim", board_id=BOARD_ID,
            artifact_type="spec", artifact_id=ARTIFACT_ID, source="test",
            graph_after="baseline-token", output={"session_id": "session-1"},
        )
        await worker.commit()
        pending = await adapter.list_unsettled_projection_operations(
            worker, board_id=BOARD_ID
        )
        assert len(pending) == 1
        assert pending[0]["operation_id"] == operation_id
        assert await adapter.has_unsettled_projection_operation(
            worker, board_id=BOARD_ID, artifact_type="spec", artifact_id=ARTIFACT_ID
        )
        await worker.rollback()
        await author.execute(text("PRAGMA busy_timeout=50"))
        result = await author.execute(text(
            "UPDATE boards SET id = id WHERE id = 'projection-other-board'"
        ))
        assert result.rowcount == 1
        await author.commit()
        row = await worker.get(ConsolidationQueue, "projection-queue")
        row.status = "pending"
        row.claim_token = None
        await worker.commit()
        await adapter.finalize_projection_operation(
            worker, operation_id=operation_id, board_id=BOARD_ID,
            graph_commit="durable-graph-commit",
        )
        await worker.commit()
    async with factory() as reopened:
        prepared = await reopened.get(DomainEventRow, operation_id)
        assert prepared is not None
        assert prepared.payload_json["output"] == {"session_id": "session-1"}
        assert (await reopened.get(ConsolidationQueue, "projection-queue")).status == "pending"
        events = (await reopened.execute(select(DomainEventRow).where(
            DomainEventRow.board_id == BOARD_ID,
            DomainEventRow.event_type == "kg.projection.operation.finalized",
        ))).scalars().all()
        assert len(events) == 1
        assert events[0].payload_json["graph_commit"] == "durable-graph-commit"
        assert not await adapter.list_unsettled_projection_operations(
            reopened, board_id=BOARD_ID
        )
        assert not await adapter.has_unsettled_projection_operation(
            reopened, board_id=BOARD_ID, artifact_type="spec", artifact_id=ARTIFACT_ID
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["future_protocol", "identity_mismatch"])
async def test_unknown_or_corrupt_projection_journal_blocks_recovery(
    queue_store, corruption,
):
    factory, adapter = queue_store
    operation_id = "80f7de89-6d3b-48c4-b00c-7bf6c764e47b"
    async with factory() as db:
        db.add(_queue_row(
            row_id="unknown-protocol-queue", work_kind="consolidate",
            generation=0, delete_event_id=None, claim_token="claim",
        ))
        await db.commit()
        assert await adapter.prepare_projection_operation(
            db, operation_id=operation_id, entry_id="unknown-protocol-queue",
            claim_token="claim", board_id=BOARD_ID,
            artifact_type="spec", artifact_id=ARTIFACT_ID, source="test",
            graph_after="baseline-token", output={"session_id": "session-1"},
        )
        await db.commit()
        event = await db.get(DomainEventRow, operation_id)
        assert event.payload_json["projector_version"]
        payload = dict(event.payload_json)
        if corruption == "future_protocol":
            payload["protocol"] = 2
            expected = "projection_operation_protocol_incompatible"
        else:
            payload["operation_id"] = "different-operation"
            expected = "projection_operation_journal_corrupt"
        event.payload_json = payload
        await db.commit()

    async with factory() as reopened:
        # Quarantine only this board. Other prepared operations may still be
        # listed and recovered, even when this row sorts first in the page.
        assert await adapter.list_unsettled_projection_operations(
            reopened, board_id=BOARD_ID,
        ) == ()
        with pytest.raises(RuntimeError, match=expected):
            await adapter.finalize_projection_operation(
                reopened, operation_id=operation_id, board_id=BOARD_ID,
                graph_commit="native-commit-token",
            )
        assert await adapter.has_unsettled_projection_operation(
            reopened, board_id=BOARD_ID,
        )


@pytest.mark.asyncio
async def test_corrupt_projection_journal_does_not_starve_other_board(queue_store):
    factory, adapter = queue_store
    bad_id = "10f7de89-6d3b-48c4-b00c-7bf6c764e47b"
    good_id = "90f7de89-6d3b-48c4-b00c-7bf6c764e47b"
    async with factory() as db:
        db.add(Board(realm_id="local", id="healthy-board", name="Healthy", owner_id="tester"))
        db.add(_queue_row(row_id="bad-journal-queue", work_kind="consolidate",
                          generation=0, delete_event_id=None, claim_token="bad-claim"))
        await db.commit()
        assert await adapter.prepare_projection_operation(
            db, operation_id=bad_id, entry_id="bad-journal-queue",
            claim_token="bad-claim", board_id=BOARD_ID,
            artifact_type="spec", artifact_id=ARTIFACT_ID, source="test",
            graph_after="baseline-token", output={"session_id": "bad"},
        )
        await db.commit()
        bad = await db.get(DomainEventRow, bad_id)
        bad.payload_json = {**bad.payload_json, "protocol": 2}
        await db.commit()
        db.add(ConsolidationQueue(
            id="good-journal-queue", board_id="healthy-board",
            artifact_type="spec", artifact_id="good-spec", source="test",
            status="claimed", claim_token="good-claim",
        ))
        await db.commit()
        assert await adapter.prepare_projection_operation(
            db, operation_id=good_id, entry_id="good-journal-queue",
            claim_token="good-claim", board_id="healthy-board",
            artifact_type="spec", artifact_id="good-spec", source="test",
            graph_after="baseline-token", output={"session_id": "good"},
        )
        await db.commit()
    async with factory() as db:
        listed = await adapter.list_unsettled_projection_operations(db, limit=1)
        assert [item["operation_id"] for item in listed] == [good_id]
        assert await adapter.has_unsettled_projection_operation(db, board_id=BOARD_ID)


@pytest.mark.asyncio
async def test_projection_preflight_does_not_reserve_sqlite_writer(queue_store):
    factory, adapter = queue_store
    async with factory() as seed:
        seed.add(Board(realm_id="local", id="preflight-other-board",
                       name="Other", owner_id="tester"))
        seed.add(_queue_row(row_id="preflight-queue", work_kind="consolidate",
                            generation=0, delete_event_id=None,
                            claim_token="preflight-claim"))
        await seed.commit()
    async with factory() as worker, factory() as author:
        assert await adapter.queue_claim_is_current_and_unfenced(
            worker, entry_id="preflight-queue", claim_token="preflight-claim",
            board_id=BOARD_ID, artifact_type="spec", artifact_id=ARTIFACT_ID,
            work_kind="consolidate", source="test", generation=0,
            delete_event_id=None, reserve_writer=False,
        )
        await author.execute(text("PRAGMA busy_timeout=50"))
        result = await author.execute(text(
            "UPDATE boards SET id = id WHERE id = 'preflight-other-board'"
        ))
        assert result.rowcount == 1
        await author.commit()
        await worker.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_receipt, progress_kind, newer_pending", [
    (None, "complete", False),
    ("native-commit-token", "complete", False),
    ("native-commit-token", "complete", True),
    ("native-commit-token", "removal_progress", False),
])
async def test_prepared_projection_recovers_after_restart(
    queue_store, monkeypatch, graph_receipt, progress_kind, newer_pending,
):
    from types import SimpleNamespace
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    operation_id = "45bd7e65-e439-4a53-abbe-d4e5364cc5ae"
    async with factory() as db:
        db.add(_queue_row(
            row_id="recovery-queue", work_kind="consolidate", generation=0,
            delete_event_id=None, claim_token="recovery-claim",
        ))
        await db.commit()
        assert await adapter.prepare_projection_operation(
            db, operation_id=operation_id, entry_id="recovery-queue",
            claim_token="recovery-claim", board_id=BOARD_ID,
            artifact_type="spec", artifact_id=ARTIFACT_ID, source="test",
            graph_after="baseline-token",
            output={"progress_kind": progress_kind, "nodes_superseded": 0,
                    "session_id": "recovery-session", "agent_id": "recovery-agent"},
        )
        await db.commit()
        if newer_pending:
            row = await db.get(ConsolidationQueue, "recovery-queue")
            row.status = "pending"
            row.claim_token = None
            await db.commit()

    class Lease:
        def ensure_owned(self, **_kwargs):
            return None

    @contextmanager
    def guarded(*_args, **_kwargs):
        yield Lease()

    @asynccontextmanager
    async def advisory(*_args, **_kwargs):
        yield

    class Graph:
        async def projection_commit_receipt(self, board_id, op_id, *, after):
            assert (board_id, op_id, after) == (
                BOARD_ID, operation_id, "baseline-token"
            )
            return graph_receipt

    async def durable(**_kwargs):
        return None

    staged = []
    finalized_sessions = []
    maintained_sessions = []

    async def stage(_db, *, operation_id, output):
        staged.append((operation_id, output))

    async def finalize(session_id, *, agent_id):
        async with factory() as check:
            markers = (await check.execute(select(DomainEventRow).where(
                DomainEventRow.event_type == "kg.projection.operation.finalized",
            ))).scalars().all()
            assert len(markers) == 1
        finalized_sessions.append((session_id, agent_id))

    async def maintain(_db, *, entry, session_id):
        assert finalized_sessions == [("recovery-session", "recovery-agent")]
        assert entry.artifact_id == ARTIFACT_ID
        maintained_sessions.append(session_id)

    monkeypatch.setattr(consolidation, "guarded_board_write", guarded)
    monkeypatch.setattr(consolidation, "advisory_lock", advisory)
    monkeypatch.setattr(consolidation, "_ensure_board_graph_durable", durable)
    monkeypatch.setattr(consolidation, "get_kg_registry", lambda: SimpleNamespace(
        graph_transaction=Graph()
    ), raising=False)
    monkeypatch.setattr(consolidation, "stage_recovered_projection_output", stage)
    monkeypatch.setattr(consolidation, "finalize_deferred_consolidation", finalize)
    monkeypatch.setattr(consolidation, "_run_post_commit_maintenance", maintain)
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(
            factory, durable_projection_protocol=True
        )
        assert await processor.recover_projection_operations() == 1
        assert await processor.recover_projection_operations() == 0
    finally:
        reset_consolidation_persistence_port_for_tests()

    async with factory() as db:
        queue = await db.get(ConsolidationQueue, "recovery-queue")
        markers = (await db.execute(select(DomainEventRow).where(
            DomainEventRow.board_id == BOARD_ID,
            DomainEventRow.event_type.like("kg.projection.operation.%"),
        ))).scalars().all()
        assert len(markers) == 2
        if graph_receipt is None:
            assert queue is not None and queue.status == "pending"
            assert queue.claim_token is None
            assert staged == []
            assert finalized_sessions == []
            assert maintained_sessions == []
            assert any(m.event_type.endswith("absent") for m in markers)
        else:
            if progress_kind == "complete":
                if newer_pending:
                    assert queue is not None and queue.status == "pending"
                    assert queue.claim_token is None
                else:
                    assert queue is None
                assert staged == [(operation_id, {
                    "progress_kind": "complete", "nodes_superseded": 0,
                    "session_id": "recovery-session", "agent_id": "recovery-agent",
                })]
            else:
                assert queue is not None and queue.status == "pending"
                assert queue.claim_token is None
                assert staged == []
            assert any(m.event_type.endswith("finalized") for m in markers)
            assert finalized_sessions == [("recovery-session", "recovery-agent")]
            assert maintained_sessions == (
                ["recovery-session"] if progress_kind == "complete" else []
            )


@pytest.mark.asyncio
async def test_recovery_durability_failure_preserves_prepared_claim(
    queue_store, monkeypatch,
):
    from types import SimpleNamespace
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    operation_id = "55bd7e65-e439-4a53-abbe-d4e5364cc5ae"
    async with factory() as db:
        db.add(_queue_row(
            row_id="durability-queue", work_kind="consolidate", generation=0,
            delete_event_id=None, claim_token="durability-claim",
        ))
        await db.commit()
        assert await adapter.prepare_projection_operation(
            db, operation_id=operation_id, entry_id="durability-queue",
            claim_token="durability-claim", board_id=BOARD_ID,
            artifact_type="spec", artifact_id=ARTIFACT_ID, source="test",
            graph_after="baseline-token", output={"session_id": "session-1"},
        )
        await db.commit()

    @contextmanager
    def guarded(*_args, **_kwargs):
        yield SimpleNamespace(ensure_owned=lambda **_kwargs: None)

    @asynccontextmanager
    async def advisory(*_args, **_kwargs):
        yield

    class Graph:
        async def projection_commit_receipt(self, *_args, **_kwargs):
            return "native-commit-token"

    async def failed_durability(**_kwargs):
        raise RuntimeError("injected_fsync_failure")

    monkeypatch.setattr(consolidation, "guarded_board_write", guarded)
    monkeypatch.setattr(consolidation, "advisory_lock", advisory)
    monkeypatch.setattr(consolidation, "_ensure_board_graph_durable", failed_durability)
    monkeypatch.setattr(consolidation, "get_kg_registry", lambda: SimpleNamespace(
        graph_transaction=Graph()
    ), raising=False)
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(
            factory, durable_projection_protocol=True
        )
        with pytest.raises(RuntimeError, match="injected_fsync_failure"):
            await processor.recover_projection_operations()
    finally:
        reset_consolidation_persistence_port_for_tests()

    async with factory() as db:
        queue = await db.get(ConsolidationQueue, "durability-queue")
        assert queue is not None and queue.status == "claimed"
        assert [item["operation_id"] for item in
                await adapter.list_unsettled_projection_operations(db)] == [operation_id]
        markers = (await db.execute(select(DomainEventRow).where(
            DomainEventRow.event_type.like("kg.projection.operation.%"),
        ))).scalars().all()
        assert [item.event_type for item in markers] == [
            "kg.projection.operation.prepared"
        ]


@pytest.mark.asyncio
async def test_worker_graph_pause_does_not_hold_sqlite_writer(queue_store, monkeypatch):
    from types import SimpleNamespace
    from okto_pulse.core.application.processors import consolidation
    from okto_pulse.core.kg.interfaces import registry as graph_registry

    factory, adapter = queue_store
    async with factory() as seed:
        seed.add(Board(realm_id="local", id="other-during-graph",
                       name="Other", owner_id="tester"))
        seed.add(_queue_row(
            row_id="paused-graph-queue", work_kind="consolidate", generation=0,
            delete_event_id=None, claim_token="paused-claim",
        ))
        await seed.commit()

    prepared = asyncio.Event()
    proceed = asyncio.Event()

    async def graph_commit(_request, **kwargs):
        await kwargs["projection_precommit"]({
            "graph_after": "baseline-token",
            "output": {"session_id": "paused-session"},
        })
        prepared.set()
        await proceed.wait()
        return SimpleNamespace(session_id="paused-session")

    async def durable(**_kwargs):
        return None

    class Graph:
        async def projection_commit_receipt(self, *_args, **_kwargs):
            return "native-commit-token"

    class Lease:
        pass

    monkeypatch.setattr(consolidation, "commit_consolidation", graph_commit)
    monkeypatch.setattr(consolidation, "_ensure_board_graph_durable", durable)
    monkeypatch.setattr(graph_registry, "get_kg_registry", lambda: SimpleNamespace(
        graph_transaction=Graph()
    ))
    register_consolidation_persistence_port(adapter)
    try:
        async with factory() as worker, factory() as author:
            entry = SimpleNamespace(
                id="paused-graph-queue", board_id=BOARD_ID,
                artifact_type="spec", artifact_id=ARTIFACT_ID,
                source="test", work_kind="consolidate", generation=0,
                delete_event_id=None, claim_token="paused-claim",
            )
            prepared_ids = []
            receipts = []
            task = asyncio.create_task(
                consolidation._commit_consolidation_with_board_graph_lifecycle(
                    entry=entry, session_id="paused-session", summary_text="test",
                    db=worker, enter_graph_write=lambda _ref: Lease(),
                    journaled_prepared=prepared_ids,
                    journaled_receipts=receipts,
                )
            )
            ready = asyncio.create_task(prepared.wait())
            done, _ = await asyncio.wait(
                {ready, task}, timeout=5,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                await task
            assert ready in done
            await author.execute(text("PRAGMA busy_timeout=50"))
            result = await author.execute(text(
                "UPDATE boards SET id = id WHERE id = 'other-during-graph'"
            ))
            assert result.rowcount == 1
            await author.commit()
            proceed.set()
            await asyncio.wait_for(task, timeout=5)
            assert len(prepared_ids) == 1
            assert receipts == [(prepared_ids[0], "native-commit-token")]
    finally:
        proceed.set()
        reset_consolidation_persistence_port_for_tests()


@pytest.mark.asyncio
async def test_journaled_worker_stages_output_after_receipt_and_acks(
    queue_store, monkeypatch,
):
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    operation_id = "f0b2c538-461a-45f0-9fd2-745c52c877e5"
    async with factory() as db:
        row = _queue_row(
            row_id="journaled-worker-queue", work_kind="consolidate",
            generation=0, delete_event_id=None, claim_token=None,
        )
        row.status = "pending"
        db.add(row)
        await db.commit()

    async def process(db, entry, **kwargs):
        assert kwargs["journaled_prepared"] == []
        assert await adapter.prepare_projection_operation(
            db, operation_id=operation_id, entry_id=entry.id,
            claim_token=entry.claim_token, board_id=entry.board_id,
            artifact_type=entry.artifact_type, artifact_id=entry.artifact_id,
            source=entry.source, graph_after="baseline-token",
            output={"progress_kind": "complete", "nodes_superseded": 0},
        )
        await adapter.commit(db)
        kwargs["journaled_prepared"].append(operation_id)
        kwargs["journaled_receipts"].append(
            (operation_id, "native-commit-token")
        )
        return True

    staged = []

    async def stage(_db, *, operation_id, output):
        staged.append((operation_id, output))

    async def no_reservation(_db, *, board_id):
        assert board_id == BOARD_ID
        return None

    monkeypatch.setattr(consolidation, "_process_queue_entry_serialized", process)
    monkeypatch.setattr(consolidation, "stage_recovered_projection_output", stage)
    monkeypatch.setattr(adapter, "board_administrative_rebuild_source", no_reservation)
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(
            factory, batch_size=1, durable_projection_protocol=True
        )
        assert await processor.process_batch() == 1
    finally:
        reset_consolidation_persistence_port_for_tests()

    assert staged == [(operation_id, {
        "progress_kind": "complete", "nodes_superseded": 0
    })]
    async with factory() as db:
        assert await db.get(ConsolidationQueue, "journaled-worker-queue") is None
        markers = (await db.execute(select(DomainEventRow).where(
            DomainEventRow.board_id == BOARD_ID,
            DomainEventRow.event_type == "kg.projection.operation.finalized",
        ))).scalars().all()
        assert len(markers) == 1
        assert markers[0].payload_json["operation_id"] == operation_id


@pytest_asyncio.fixture
async def queue_store(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'card4-worker-fence.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_wal(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Board(realm_id="local", id=BOARD_ID, name="Card 4", owner_id="tester"))
        await session.commit()
    try:
        yield factory, CommunitySqlAlchemyConsolidationPersistence()
    finally:
        await engine.dispose()


def _queue_row(
    *,
    row_id: str,
    work_kind: str,
    generation: int,
    delete_event_id: str | None,
    claim_token: str | None,
    status: str = "claimed",
    artifact_id: str = ARTIFACT_ID,
) -> ConsolidationQueue:
    return ConsolidationQueue(
        id=row_id,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=artifact_id,
        work_kind=work_kind,
        generation=generation,
        payload=(
            {
                "schema_version": 1,
                "delete_event_id": delete_event_id,
                "source_refs": [f"spec:{artifact_id}"],
            }
            if work_kind == "stale_reconcile"
            else None
        ),
        delete_event_id=delete_event_id,
        priority="high",
        source="governed_delete" if work_kind == "stale_reconcile" else "test",
        status=status,
        worker_id="worker-card4" if status == "claimed" else None,
        claimed_by_session_id="worker-card4" if status == "claimed" else None,
        claim_token=claim_token,
        claimed_at=datetime.now(timezone.utc) if status == "claimed" else None,
        claim_timeout_at=datetime.now(timezone.utc) + timedelta(minutes=5) if status == "claimed" else None,
    )


@pytest.mark.asyncio
async def test_queue_claim_fence_is_exact_and_generation_fail_closed(queue_store):
    """Consolidate needs no tombstone; reconcile needs the exact tombstone."""

    factory, adapter = queue_store
    async with factory() as session:
        session.add_all(
            [
                _queue_row(
                    row_id="legacy-consolidate",
                    work_kind="consolidate",
                    generation=0,
                    delete_event_id=None,
                    claim_token="legacy-token",
                ),
                _queue_row(
                    row_id="reconcile-g1",
                    work_kind="stale_reconcile",
                    generation=1,
                    delete_event_id="delete-event-1",
                    claim_token="reconcile-token",
                ),
            ]
        )
        session.add(
            ArtifactDeletionTombstone(
                id="tombstone-g1",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=ARTIFACT_ID,
                generation=1,
                delete_event_id="delete-event-1",
            )
        )
        await session.commit()

        legacy = dict(
            entry_id="legacy-consolidate",
            claim_token="legacy-token",
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id=ARTIFACT_ID,
            work_kind="consolidate",
            source="test",
            generation=0,
            delete_event_id=None,
        )
        # Any tombstone permanently fences legacy canonical publication.
        assert not await adapter.queue_claim_is_current_and_unfenced(session, **legacy)

        reconcile = dict(
            entry_id="reconcile-g1",
            claim_token="reconcile-token",
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id=ARTIFACT_ID,
            work_kind="stale_reconcile",
            source="governed_delete",
            generation=1,
            delete_event_id="delete-event-1",
        )
        assert await adapter.queue_claim_is_current_and_unfenced(session, **reconcile)
        for divergent in (
            {"claim_token": "stolen-token"},
            {"artifact_id": "another-spec"},
            {"generation": 2},
            {"delete_event_id": "delete-event-2"},
            {"work_kind": "stale_sweep"},
        ):
            candidate = {**reconcile, **divergent}
            assert not await adapter.queue_claim_is_current_and_unfenced(
                session, **candidate
            )

        # A later deletion generation makes the old G1 job stale immediately.
        tombstone = await session.get(ArtifactDeletionTombstone, "tombstone-g1")
        assert tombstone is not None
        tombstone.generation = 2
        tombstone.delete_event_id = "delete-event-2"
        await session.commit()
        assert not await adapter.queue_claim_is_current_and_unfenced(
            session, **reconcile
        )


@pytest.mark.asyncio
async def test_legacy_claim_is_current_only_until_tombstone_commits(queue_store):
    """The last re-check observes a tombstone committed after the claim."""

    factory, adapter = queue_store
    identity = dict(
        entry_id="legacy-race",
        claim_token="claim-before-delete",
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id="race-spec",
        work_kind="consolidate",
        source="test",
        generation=0,
        delete_event_id=None,
    )
    async with factory() as session:
        session.add(
            _queue_row(
                row_id="legacy-race",
                work_kind="consolidate",
                generation=0,
                delete_event_id=None,
                claim_token="claim-before-delete",
                artifact_id="race-spec",
            )
        )
        await session.commit()
        assert await adapter.queue_claim_is_current_and_unfenced(session, **identity)

    async with factory() as deletion_session:
        deletion_session.add(
            ArtifactDeletionTombstone(
                id="race-tombstone",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="race-spec",
                generation=1,
                delete_event_id="race-delete-event",
            )
        )
        await deletion_session.commit()

    async with factory() as worker_session:
        assert not await adapter.queue_claim_is_current_and_unfenced(
            worker_session, **identity
        )


@pytest.mark.asyncio
async def test_wal_delete_wins_between_extraction_and_recheck(queue_store):
    """A delete holding the writer slot linearizes before publication."""

    factory, adapter = queue_store
    identity = dict(
        entry_id="wal-delete-wins",
        claim_token="wal-worker-token",
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id="wal-deleted-spec",
        work_kind="consolidate",
        source="test",
        generation=0,
        delete_event_id=None,
    )
    async with factory() as seed:
        seed.add(
            _queue_row(
                row_id="wal-delete-wins",
                work_kind="consolidate",
                generation=0,
                delete_event_id=None,
                claim_token="wal-worker-token",
                artifact_id="wal-deleted-spec",
            )
        )
        await seed.commit()

    worker_session = factory()
    try:
        async with factory() as deletion_session:
            deletion_session.add(
                ArtifactDeletionTombstone(
                    id="wal-delete-wins-tombstone",
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id="wal-deleted-spec",
                    generation=1,
                    delete_event_id="wal-delete-event",
                )
            )
            # Flush acquires SQLite's sole WAL writer slot but deliberately
            # leaves the governed deletion uncommitted for the race window.
            await deletion_session.flush()
            fence_task = asyncio.create_task(
                adapter.queue_claim_is_current_and_unfenced(
                    worker_session,
                    **identity,
                )
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(fence_task), timeout=0.2)

            await deletion_session.commit()
            assert not await asyncio.wait_for(fence_task, timeout=3)
    finally:
        await worker_session.rollback()
        await worker_session.close()


@pytest.mark.asyncio
async def test_wal_worker_fence_holds_delete_until_ack_commit(queue_store):
    """The opposite order is serialized through the worker's CAS ACK."""

    factory, adapter = queue_store
    identity = dict(
        entry_id="wal-worker-wins",
        claim_token="wal-winning-token",
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id="wal-live-spec",
        work_kind="consolidate",
        source="test",
        generation=0,
        delete_event_id=None,
    )
    async with factory() as seed:
        seed.add(
            _queue_row(
                row_id="wal-worker-wins",
                work_kind="consolidate",
                generation=0,
                delete_event_id=None,
                claim_token="wal-winning-token",
                artifact_id="wal-live-spec",
            )
        )
        await seed.commit()

    worker_session = factory()
    delete_started = asyncio.Event()

    async def _commit_delete_after_fence():
        async with factory() as deletion_session:
            deletion_session.add(
                ArtifactDeletionTombstone(
                    id="wal-worker-wins-tombstone",
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id="wal-live-spec",
                    generation=1,
                    delete_event_id="wal-later-delete-event",
                )
            )
            delete_started.set()
            await deletion_session.commit()

    try:
        assert await adapter.queue_claim_is_current_and_unfenced(
            worker_session,
            **identity,
        )
        delete_task = asyncio.create_task(_commit_delete_after_fence())
        await delete_started.wait()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(delete_task), timeout=0.2)

        assert await adapter.ack_claimed_queue_entry(
            worker_session,
            entry_id=identity["entry_id"],
            claim_token=identity["claim_token"],
            board_id=BOARD_ID,
            source="test",
            work_kind="consolidate",
            generation=0,
            delete_event_id=None,
        )
        await worker_session.commit()
        await asyncio.wait_for(delete_task, timeout=3)
    finally:
        await worker_session.rollback()
        await worker_session.close()

    async with factory() as verification:
        assert (
            await verification.get(
                ConsolidationQueue,
                identity["entry_id"],
            )
            is None
        )
        assert (
            await verification.get(
                ArtifactDeletionTombstone,
                "wal-worker-wins-tombstone",
            )
            is not None
        )


@pytest.mark.asyncio
async def test_ready_listing_includes_card8_stale_sweep(queue_store):
    """Card 8 makes the board-scoped coordinator worker-claimable."""

    factory, adapter = queue_store
    async with factory() as session:
        session.add_all(
            [
                _queue_row(
                    row_id="ready-consolidate",
                    work_kind="consolidate",
                    generation=0,
                    delete_event_id=None,
                    claim_token=None,
                    status="pending",
                ),
                ConsolidationQueue(
                    id="card8-sweep",
                    board_id=BOARD_ID,
                    artifact_type="board",
                    artifact_id=BOARD_ID,
                    work_kind="stale_sweep",
                    generation=0,
                    payload={"cursor": "", "budget": 50, "attempt": 0},
                    delete_event_id=None,
                    priority="low",
                    source="kg_tick",
                    status="pending",
                ),
            ]
        )
        await session.commit()

        ready = await adapter.list_ready_pending(
            session,
            now=datetime.now(timezone.utc),
        )
    assert [entry.id for entry in ready] == ["ready-consolidate", "card8-sweep"]


@pytest.mark.asyncio
async def test_recovery_processor_claims_only_exact_rebuild_membership(
    queue_store,
    monkeypatch,
) -> None:
    """Direct recovery never claims another board, source or work kind."""

    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    target_source = "rebuild:manifest-exact"
    other_board = "card4-worker-fence-other-board"

    def pending_row(
        row_id: str,
        *,
        board_id: str = BOARD_ID,
        source: str = target_source,
        work_kind: str = "consolidate",
    ) -> ConsolidationQueue:
        return ConsolidationQueue(
            id=row_id,
            board_id=board_id,
            artifact_type="spec" if work_kind == "consolidate" else "board",
            artifact_id=(row_id if work_kind == "consolidate" else board_id),
            work_kind=work_kind,
            generation=0,
            priority="high",
            source=source,
            status="pending",
            triggered_at=datetime.now(timezone.utc),
        )

    async with factory() as session:
        session.add(Board(realm_id="local", id=other_board, name="Other", owner_id="tester"))
        session.add_all(
            [
                pending_row("exact-rebuild-row"),
                pending_row("other-board-row", board_id=other_board),
                pending_row("other-source-row", source="rebuild:old-manifest"),
                pending_row(
                    "live-source-row",
                    source="event:spec.updated",
                ),
                pending_row(
                    "stale-sweep-row",
                    source="kg_tick:stale_sweep",
                    work_kind="stale_sweep",
                ),
            ]
        )
        await session.commit()

    processed_ids: list[str] = []

    async def process_exact(_db, entry, **_kwargs):  # noqa: ANN001, ANN003
        processed_ids.append(entry.id)
        return True

    async def reservation_source(_db, *, board_id: str):  # noqa: ANN001
        return target_source if board_id == BOARD_ID else None

    monkeypatch.setattr(
        consolidation,
        "_process_queue_entry_serialized",
        process_exact,
    )
    monkeypatch.setattr(
        adapter,
        "board_administrative_rebuild_source",
        reservation_source,
    )
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(factory, batch_size=5)
        scope = ConsolidationClaimScope(
            board_id=BOARD_ID,
            source=target_source,
        )
        assert await processor.process_batch(claim_scope=scope) == 1
        assert await processor.process_batch(claim_scope=scope) == 0
    finally:
        reset_consolidation_persistence_port_for_tests()

    assert processed_ids == ["exact-rebuild-row"]
    async with factory() as session:
        rows = (
            await session.execute(
                select(ConsolidationQueue).order_by(ConsolidationQueue.id)
            )
        ).scalars()
        remaining = {row.id: row.status for row in rows}
    assert remaining == {
        "live-source-row": "pending",
        "other-board-row": "pending",
        "other-source-row": "pending",
        "stale-sweep-row": "pending",
    }


@pytest.mark.asyncio
async def test_recovery_processor_exactly_repends_claim_left_by_killed_process(
    queue_store,
    monkeypatch,
) -> None:
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    target_source = "rebuild:manifest-killed"
    unrelated_board = f"{BOARD_ID}-unrelated"
    async with factory() as session:
        session.add(Board(realm_id="local", id=unrelated_board, name="Unrelated", owner_id="tester"))
        session.add_all(
            [
                ConsolidationQueue(
                    id="killed-exact-claim",
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id="killed-exact-claim",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source=target_source,
                    status="claimed",
                    triggered_at=datetime.now(timezone.utc),
                    claimed_at=datetime.now(timezone.utc),
                    claim_timeout_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                    worker_id="dead-worker",
                    claimed_by_session_id="dead-worker",
                    claim_token="dead-claim-token",
                ),
                ConsolidationQueue(
                    id="unrelated-claimed",
                    board_id=unrelated_board,
                    artifact_type="spec",
                    artifact_id="unrelated-claimed",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source="event:spec.updated",
                    status="claimed",
                    triggered_at=datetime.now(timezone.utc),
                    claimed_at=datetime.now(timezone.utc),
                    claim_timeout_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                    worker_id="live-worker",
                    claimed_by_session_id="live-worker",
                    claim_token="live-claim-token",
                ),
            ]
        )
        await session.commit()

    async def reservation_source(_db, *, board_id: str):  # noqa: ANN001
        return target_source if board_id == BOARD_ID else None

    logical_graph_ids = {"spec:killed-exact-claim"}

    async def idempotent_graph_replay(_db, entry, **_kwargs):  # noqa: ANN001, ANN003
        logical_graph_ids.add(f"spec:{entry.artifact_id}")
        return True

    monkeypatch.setattr(
        adapter,
        "board_administrative_rebuild_source",
        reservation_source,
    )
    monkeypatch.setattr(
        consolidation,
        "_process_queue_entry_serialized",
        idempotent_graph_replay,
    )
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(factory, batch_size=1)
        scope = ConsolidationClaimScope(
            board_id=BOARD_ID,
            source=target_source,
            reservation_lineage_id="a" * 64,
        )
        assert (
            await processor.recover_exact_claims(
                claim_scope=scope,
                recovery_authority_probe=lambda: True,
            )
            == 1
        )
        assert await processor.process_batch(claim_scope=scope) == 1
    finally:
        reset_consolidation_persistence_port_for_tests()

    assert logical_graph_ids == {"spec:killed-exact-claim"}
    async with factory() as session:
        assert await session.get(ConsolidationQueue, "killed-exact-claim") is None
        unrelated = await session.get(ConsolidationQueue, "unrelated-claimed")
        assert unrelated is not None
        assert unrelated.status == "claimed"
        assert unrelated.claim_token == "live-claim-token"


@pytest.mark.asyncio
async def test_live_claim_is_neutrally_repended_when_rebuild_reserves_board(
    queue_store,
    monkeypatch,
) -> None:
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    payload = {"revision": 11}
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="live-before-reservation",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="live-before-reservation",
                work_kind="consolidate",
                generation=0,
                priority="high",
                source="event:spec.updated",
                status="pending",
                attempts=2,
                payload=payload,
                triggered_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    reservation_checks = 0

    async def reservation(_db, *, board_id: str):  # noqa: ANN001
        nonlocal reservation_checks
        assert board_id == BOARD_ID
        reservation_checks += 1
        return None if reservation_checks == 1 else "rebuild:manifest-started"

    async def must_not_process(*_args, **_kwargs):  # noqa: ANN201
        pytest.fail("a live claim fenced by rebuild must not reach graph work")

    monkeypatch.setattr(adapter, "board_administrative_rebuild_source", reservation)
    monkeypatch.setattr(
        consolidation,
        "_process_queue_entry_serialized",
        must_not_process,
    )
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(factory, batch_size=1)
        assert await processor.process_batch() == 0
    finally:
        reset_consolidation_persistence_port_for_tests()

    assert reservation_checks >= 2
    async with factory() as session:
        row = await session.get(ConsolidationQueue, "live-before-reservation")
        assert row is not None
        assert row.status == "pending"
        assert row.source == "event:spec.updated"
        assert row.payload == payload
        assert row.attempts == 2
        assert row.claim_token is None
        assert row.claimed_at is None
        assert row.claim_timeout_at is None
        assert row.worker_id is None
        assert row.claimed_by_session_id is None


@pytest.mark.parametrize("reserved_source", (None, "", "rebuild:other"))
@pytest.mark.asyncio
async def test_recovery_processor_requires_its_exact_live_reservation(
    queue_store,
    monkeypatch,
    reserved_source: str | None,
) -> None:
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    target_source = "rebuild:manifest-exact"
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="reservation-required-row",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="reservation-required-spec",
                work_kind="consolidate",
                generation=0,
                priority="high",
                source=target_source,
                status="pending",
                triggered_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    async def reservation(_db, *, board_id: str):  # noqa: ANN001, ANN201
        assert board_id == BOARD_ID
        return reserved_source

    monkeypatch.setattr(
        adapter,
        "board_administrative_rebuild_source",
        reservation,
    )
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(factory, batch_size=1)
        with pytest.raises(
            RuntimeError,
            match="consolidation_exact_claim_reservation_mismatch",
        ):
            await processor.process_batch(
                claim_scope=ConsolidationClaimScope(
                    board_id=BOARD_ID,
                    source=target_source,
                )
            )
    finally:
        reset_consolidation_persistence_port_for_tests()

    async with factory() as session:
        row = await session.get(ConsolidationQueue, "reservation-required-row")
        assert row is not None
        assert row.status == "pending"
        assert row.claim_token is None


@pytest.mark.asyncio
async def test_administrative_rebuild_source_ignores_expired_manifest(
    monkeypatch,
) -> None:
    from okto_pulse.core.kg.single_writer_lock import (
        KGAdministrativeOperationReservation,
        LockManifest,
    )

    adapter = CommunitySqlAlchemyConsolidationPersistence()
    now_epoch = datetime.now(timezone.utc).timestamp()
    manifest = LockManifest(
        owner_token="reservation-token",
        owner_id="operator",
        operation="kg02_rebuild_reservation:manifest-expiry",
        acquired_at_epoch=now_epoch - 10,
        expires_at_epoch=now_epoch - 1,
        admin_lane=True,
    )

    def inspect_expired(_self, *, board_id: str):  # noqa: ANN001, ANN201
        assert board_id == BOARD_ID
        return manifest

    monkeypatch.setattr(
        KGAdministrativeOperationReservation,
        "inspect",
        inspect_expired,
    )

    assert (
        await adapter.board_administrative_rebuild_source(
            None,
            board_id=BOARD_ID,
        )
        is None
    )

    active = LockManifest(
        owner_token=manifest.owner_token,
        owner_id=manifest.owner_id,
        operation=manifest.operation,
        acquired_at_epoch=manifest.acquired_at_epoch,
        expires_at_epoch=now_epoch + 60,
        admin_lane=True,
    )

    def inspect_active(_self, *, board_id: str):  # noqa: ANN001, ANN201
        assert board_id == BOARD_ID
        return active

    monkeypatch.setattr(
        KGAdministrativeOperationReservation,
        "inspect",
        inspect_active,
    )
    assert (
        await adapter.board_administrative_rebuild_source(
            None,
            board_id=BOARD_ID,
        )
        == "rebuild:manifest-expiry"
    )


@pytest.mark.asyncio
async def test_exact_claim_cas_rejects_retag_between_listing_and_claim(
    queue_store,
    monkeypatch,
) -> None:
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    target_source = "rebuild:manifest-exact"
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="retag-race-row",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="retag-race-spec",
                work_kind="consolidate",
                generation=0,
                priority="high",
                source=target_source,
                status="pending",
                triggered_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    original_listing = adapter.list_ready_pending_exact

    async def list_then_retag(context, **kwargs):  # noqa: ANN001, ANN003
        listed = await original_listing(context, **kwargs)
        row = await context.get(ConsolidationQueue, "retag-race-row")
        assert row is not None
        row.source = "event:spec.updated"
        await context.flush()
        return listed

    async def reservation(_db, *, board_id: str):  # noqa: ANN001
        assert board_id == BOARD_ID
        return target_source

    monkeypatch.setattr(adapter, "list_ready_pending_exact", list_then_retag)
    monkeypatch.setattr(
        adapter,
        "board_administrative_rebuild_source",
        reservation,
    )
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(factory, batch_size=1)
        assert (
            await processor.process_batch(
                claim_scope=ConsolidationClaimScope(
                    board_id=BOARD_ID,
                    source=target_source,
                )
            )
            == 0
        )
    finally:
        reset_consolidation_persistence_port_for_tests()

    async with factory() as session:
        row = await session.get(ConsolidationQueue, "retag-race-row")
        assert row is not None
        assert row.source == "event:spec.updated"
        assert row.status == "pending"
        assert row.claim_token is None


@pytest.mark.parametrize("direct_scope", (False, True), ids=("normal", "exact"))
@pytest.mark.asyncio
async def test_rebuild_claim_cas_rechecks_current_head_after_listing(
    queue_store,
    monkeypatch,
    direct_scope: bool,
) -> None:
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    target_source = "rebuild:manifest-resequenced"
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="listed-child",
                board_id=BOARD_ID,
                artifact_type="refinement",
                artifact_id="listed-child",
                work_kind="consolidate",
                generation=0,
                priority="high",
                source=target_source,
                status="pending",
                triggered_at=now + timedelta(microseconds=1),
            )
        )
        await session.commit()

    listing_name = "list_ready_pending_exact" if direct_scope else "list_ready_pending"
    original_listing = getattr(adapter, listing_name)
    inserted = False

    async def list_then_insert_predecessor(context, **kwargs):  # noqa: ANN001, ANN003
        nonlocal inserted
        listed = await original_listing(context, **kwargs)
        if not inserted:
            inserted = True
            context.add(
                ConsolidationQueue(
                    id="new-predecessor",
                    board_id=BOARD_ID,
                    artifact_type="ideation",
                    artifact_id="new-predecessor",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source=target_source,
                    status="pending",
                    triggered_at=now,
                )
            )
            await context.flush()
        return listed

    async def reservation(_db, *, board_id: str):  # noqa: ANN001
        assert board_id == BOARD_ID
        return target_source

    processed: list[str] = []

    async def process(_db, entry, **_kwargs):  # noqa: ANN001, ANN003
        processed.append(entry.id)
        return True

    monkeypatch.setattr(adapter, listing_name, list_then_insert_predecessor)
    monkeypatch.setattr(adapter, "board_administrative_rebuild_source", reservation)
    monkeypatch.setattr(consolidation, "_process_queue_entry_serialized", process)
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(factory, batch_size=1)
        claim_scope = (
            ConsolidationClaimScope(board_id=BOARD_ID, source=target_source)
            if direct_scope
            else None
        )
        assert await processor.process_batch(claim_scope=claim_scope) == 0
        assert processed == []

        monkeypatch.setattr(adapter, listing_name, original_listing)
        assert await processor.process_batch(claim_scope=claim_scope) == 1
        assert await processor.process_batch(claim_scope=claim_scope) == 1
    finally:
        reset_consolidation_persistence_port_for_tests()

    assert processed == ["new-predecessor", "listed-child"]


@pytest.mark.asyncio
async def test_rebuild_head_backoff_blocks_child_in_exact_and_normal_worker(
    queue_store,
    monkeypatch,
) -> None:
    from okto_pulse.core.application.processors import consolidation

    factory, adapter = queue_store
    target_source = "rebuild:manifest-ordered"
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add_all(
            [
                ConsolidationQueue(
                    id="parent-row",
                    board_id=BOARD_ID,
                    artifact_type="ideation",
                    artifact_id="parent",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source=target_source,
                    status="pending",
                    triggered_at=now,
                    next_retry_at=now + timedelta(hours=1),
                ),
                ConsolidationQueue(
                    id="child-row",
                    board_id=BOARD_ID,
                    artifact_type="refinement",
                    artifact_id="child",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source=target_source,
                    status="pending",
                    triggered_at=now + timedelta(microseconds=1),
                ),
            ]
        )
        await session.commit()

    async def reservation(_db, *, board_id: str):  # noqa: ANN001
        assert board_id == BOARD_ID
        return target_source

    processed: list[str] = []

    async def process(_db, entry, **_kwargs):  # noqa: ANN001, ANN003
        processed.append(entry.id)
        return True

    monkeypatch.setattr(
        adapter,
        "board_administrative_rebuild_source",
        reservation,
    )
    monkeypatch.setattr(consolidation, "_process_queue_entry_serialized", process)
    register_consolidation_persistence_port(adapter)
    try:
        exact = consolidation.ConsolidationProcessor(factory, batch_size=1)
        scope = ConsolidationClaimScope(
            board_id=BOARD_ID,
            source=target_source,
        )
        assert await exact.process_batch(claim_scope=scope) == 0
        assert (
            await consolidation.ConsolidationProcessor(
                factory,
                batch_size=1,
            ).process_batch()
            == 0
        )
        assert processed == []

        async with factory() as session:
            parent = await session.get(ConsolidationQueue, "parent-row")
            assert parent is not None
            parent.next_retry_at = now - timedelta(seconds=1)
            await session.commit()

        assert await exact.process_batch(claim_scope=scope) == 1
        assert await exact.process_batch(claim_scope=scope) == 1
    finally:
        reset_consolidation_persistence_port_for_tests()

    assert processed == ["parent-row", "child-row"]


@pytest.mark.asyncio
async def test_repend_claimed_entry_is_an_exact_neutral_cas(queue_store) -> None:
    factory, adapter = queue_store
    retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    payload = {"revision": 7}
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="preclaimed-live",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="preclaimed-live",
                work_kind="consolidate",
                generation=0,
                priority="high",
                source="event:spec.updated",
                status="claimed",
                attempts=3,
                payload=payload,
                next_retry_at=retry_at,
                claimed_at=datetime.now(timezone.utc),
                claim_timeout_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                worker_id="worker-live",
                claimed_by_session_id="worker-live",
                claim_token="token-live",
            )
        )
        await session.commit()

        exact = dict(
            entry_id="preclaimed-live",
            claim_token="token-live",
            board_id=BOARD_ID,
            source="event:spec.updated",
            work_kind="consolidate",
            generation=0,
            delete_event_id=None,
        )
        assert not await adapter.repend_claimed_queue_entry(
            session,
            **{**exact, "source": "rebuild:other"},
        )
        assert await adapter.repend_claimed_queue_entry(session, **exact)
        await session.commit()

        row = await session.get(ConsolidationQueue, "preclaimed-live")
        assert row is not None
        await session.refresh(row)
        assert row.status == "pending"
        assert row.source == "event:spec.updated"
        assert row.payload == payload
        assert row.attempts == 3
        observed_retry_at = row.next_retry_at
        assert observed_retry_at is not None
        if observed_retry_at.tzinfo is None:
            observed_retry_at = observed_retry_at.replace(tzinfo=timezone.utc)
        assert observed_retry_at == retry_at
        assert row.claimed_at is None
        assert row.claim_timeout_at is None
        assert row.worker_id is None
        assert row.claimed_by_session_id is None
        assert row.claim_token is None


@pytest.mark.asyncio
async def test_exact_pending_inventory_includes_delayed_rows_in_claim_order(
    queue_store,
) -> None:
    factory, adapter = queue_store
    source = "rebuild:manifest-exact-inventory"
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add_all(
            [
                ConsolidationQueue(
                    id="exact-inventory-later",
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id="later",
                    work_kind="consolidate",
                    generation=0,
                    payload={"ordinal": 2},
                    priority="high",
                    source=source,
                    status="pending",
                    triggered_at=now + timedelta(seconds=1),
                ),
                ConsolidationQueue(
                    id="exact-inventory-delayed",
                    board_id=BOARD_ID,
                    artifact_type="refinement",
                    artifact_id="delayed",
                    work_kind="consolidate",
                    generation=0,
                    payload={"ordinal": 1},
                    priority="high",
                    source=source,
                    status="pending",
                    triggered_at=now,
                    next_retry_at=now + timedelta(hours=1),
                ),
                ConsolidationQueue(
                    id="exact-inventory-other-source",
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id="other",
                    work_kind="consolidate",
                    generation=0,
                    payload={"ordinal": 0},
                    priority="high",
                    source="rebuild:other",
                    status="pending",
                    triggered_at=now - timedelta(seconds=1),
                ),
            ]
        )
        await session.commit()

        rows = await adapter.list_pending_exact(
            session,
            board_id=BOARD_ID,
            source=source,
            work_kind="consolidate",
        )

    assert tuple(row.id for row in rows) == (
        "exact-inventory-delayed",
        "exact-inventory-later",
    )


@pytest.mark.asyncio
async def test_exact_disposition_save_accepts_compact_raw_payload_with_full_state_cas(
    queue_store,
    monkeypatch,
) -> None:
    factory, adapter = queue_store
    source = "rebuild:manifest-exact-disposition"
    row_id = "exact-disposition-row"
    membership = {
        "run_id": "manifest-exact-disposition",
        "source_ref": "spec:exact-disposition-spec",
        "source_version": "7",
        "content_hash": "a" * 64,
    }
    expected_payload = {"_rebuild_membership": membership}
    marker = {
        "schema_version": 1,
        "queue_id": row_id,
        "board_id": BOARD_ID,
        "source": source,
        "work_kind": "consolidate",
        "artifact_type": "spec",
        "artifact_id": "exact-disposition-spec",
        "generation": 0,
        "membership_source_ref": membership["source_ref"],
        "membership_source_version": membership["source_version"],
        "membership_content_hash": membership["content_hash"],
        "attempt_ordinal": 1,
        "queue_attempts": 1,
        "disposition": "terminal_failure",
        "retryable": False,
        "mutation_state": "unchanged",
        "error_code": "connectivity_constraint_violated",
        "error_message": "canonical graph connectivity refused",
        "next_retry_at": None,
        "diagnostic_json": None,
    }
    payload = {**expected_payload, "_exact_rebuild_disposition": marker}
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id=row_id,
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="exact-disposition-spec",
                work_kind="consolidate",
                generation=0,
                payload=expected_payload,
                priority="high",
                source=source,
                status="claimed",
                attempts=0,
                triggered_at=now,
                claimed_at=now,
                claim_timeout_at=now + timedelta(minutes=5),
                worker_id="worker-exact",
                claimed_by_session_id="worker-exact",
                claim_token="token-exact",
            )
        )
        await session.commit()
        compact_expected_payload = json.dumps(
            expected_payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        await session.execute(
            text(
                "UPDATE consolidation_queue SET payload = :payload WHERE id = :row_id"
            ),
            {"payload": compact_expected_payload, "row_id": row_id},
        )
        await session.commit()
        assert (
            await session.scalar(
                text("SELECT payload FROM consolidation_queue WHERE id = :row_id"),
                {"row_id": row_id},
            )
            == compact_expected_payload
        )

        async def reservation(_context, *, board_id: str):  # noqa: ANN001
            assert board_id == BOARD_ID
            return source

        monkeypatch.setattr(adapter, "board_administrative_rebuild_source", reservation)
        authority_calls = 0

        def authority() -> bool:
            nonlocal authority_calls
            authority_calls += 1
            return True

        stored = await adapter.save_exact_rebuild_disposition(
            session,
            entry_id=row_id,
            claim_token="token-exact",
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id="exact-disposition-spec",
            source=source,
            work_kind="consolidate",
            generation=0,
            delete_event_id=None,
            expected_attempts=0,
            expected_last_error=None,
            expected_next_retry_at=None,
            expected_payload=expected_payload,
            reservation_authority_probe=authority,
            payload=payload,
            attempts=1,
            last_error=(
                "connectivity_constraint_violated:canonical graph connectivity refused"
            ),
            next_retry_at=None,
        )
        assert stored is not None
        assert stored.status == "pending"
        assert stored.payload == payload
        assert stored.attempts == 1
        assert stored.claim_token is None
        assert authority_calls == 3
        await session.commit()

        unchanged = await adapter.save_exact_rebuild_disposition(
            session,
            entry_id=row_id,
            claim_token="token-exact",
            board_id=BOARD_ID,
            artifact_type="refinement",
            artifact_id="exact-disposition-spec",
            source=source,
            work_kind="consolidate",
            generation=0,
            delete_event_id=None,
            expected_attempts=0,
            expected_last_error=None,
            expected_next_retry_at=None,
            expected_payload=expected_payload,
            reservation_authority_probe=lambda: True,
            payload=payload,
            attempts=1,
            last_error=(
                "connectivity_constraint_violated:canonical graph connectivity refused"
            ),
            next_retry_at=None,
        )
        assert unchanged is None


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_results", ((False,), (True, False)))
async def test_exact_disposition_save_refuses_lost_authority_before_update(
    queue_store,
    monkeypatch,
    authority_results,
) -> None:
    factory, adapter = queue_store
    source = "rebuild:manifest-authority-lost"
    expected_payload = {
        "_rebuild_membership": {
            "run_id": "manifest-authority-lost",
            "source_ref": "spec:authority-lost",
            "source_version": "1",
            "content_hash": "b" * 64,
        }
    }
    payload = {
        **expected_payload,
        "_exact_rebuild_disposition": {"schema_version": 1},
    }
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="authority-lost-row",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="authority-lost",
                work_kind="consolidate",
                generation=0,
                payload=expected_payload,
                priority="high",
                source=source,
                status="claimed",
                attempts=0,
                triggered_at=now,
                claimed_at=now,
                claim_timeout_at=now + timedelta(minutes=5),
                worker_id="worker-authority",
                claimed_by_session_id="worker-authority",
                claim_token="token-authority",
            )
        )
        await session.commit()

        async def reservation(_context, *, board_id: str):  # noqa: ANN001
            assert board_id == BOARD_ID
            return source

        monkeypatch.setattr(adapter, "board_administrative_rebuild_source", reservation)
        probe_calls = 0
        authority_sequence = iter(authority_results)

        def authority_replaced_before_update() -> bool:
            nonlocal probe_calls
            probe_calls += 1
            return next(authority_sequence)

        assert (
            await adapter.save_exact_rebuild_disposition(
                session,
                entry_id="authority-lost-row",
                claim_token="token-authority",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="authority-lost",
                source=source,
                work_kind="consolidate",
                generation=0,
                delete_event_id=None,
                expected_attempts=0,
                expected_last_error=None,
                expected_next_retry_at=None,
                expected_payload=expected_payload,
                reservation_authority_probe=authority_replaced_before_update,
                payload=payload,
                attempts=1,
                last_error="connectivity_constraint_violated:blocked",
                next_retry_at=None,
            )
            is None
        )
        assert probe_calls == len(authority_results)
        row = await session.get(ConsolidationQueue, "authority-lost-row")
        assert row is not None
        await session.refresh(row)
        assert row.status == "claimed"
        assert row.attempts == 0
        assert row.payload == expected_payload
        assert row.claim_token == "token-authority"


@pytest.mark.asyncio
async def test_exact_connectivity_terminal_replays_without_debt_or_dlq(
    queue_store,
    monkeypatch,
) -> None:
    from okto_pulse.community.adapters.sqlalchemy_models import (
        CanonicalDebt,
        ConsolidationDeadLetter,
    )
    from okto_pulse.core.application.processors import consolidation
    from okto_pulse.core.ports.consolidation import (
        ExactConsolidationDisposition,
        ExactConsolidationResultOrigin,
    )

    factory, adapter = queue_store
    manifest_ref = "manifest-exact-connectivity"
    source = f"rebuild:{manifest_ref}"
    lineage_id = "d" * 64
    payload = {
        "_rebuild_membership": {
            "run_id": manifest_ref,
            "source_ref": "spec:exact-connectivity-spec",
            "source_version": "3",
            "content_hash": "e" * 64,
        }
    }
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="exact-connectivity-row",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="exact-connectivity-spec",
                work_kind="consolidate",
                generation=0,
                payload=payload,
                priority="high",
                source=source,
                status="pending",
                attempts=0,
                triggered_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    async def reservation(_context, *, board_id: str):  # noqa: ANN001
        assert board_id == BOARD_ID
        return source

    process_calls = 0

    async def connectivity_failure(_db, _entry, **_kwargs):  # noqa: ANN001
        nonlocal process_calls
        process_calls += 1
        raise consolidation.KGPrimitiveError(
            consolidation.CONNECTIVITY_ERROR_CODE,
            "deterministic connectivity terminal",
            details={"connectivity": {"violations": [{"reason_code": "denied"}]}},
        )

    monkeypatch.setattr(adapter, "board_administrative_rebuild_source", reservation)
    monkeypatch.setattr(
        consolidation,
        "_process_queue_entry_serialized",
        connectivity_failure,
    )
    register_consolidation_persistence_port(adapter)
    scope = ConsolidationClaimScope(
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
    )
    try:
        first_processor = consolidation.ConsolidationProcessor(factory, batch_size=1)
        first = await first_processor.process_exact_batch(
            claim_scope=scope,
            reservation_authority_probe=lambda: True,
        )
        successor_processor = consolidation.ConsolidationProcessor(
            factory, batch_size=1
        )
        replayed = await successor_processor.process_exact_batch(
            claim_scope=scope,
            reservation_authority_probe=lambda: True,
        )
    finally:
        reset_consolidation_persistence_port_for_tests()

    assert process_calls == 1
    assert first.new_attempt_count == 1
    assert len(first.terminal_failures) == 1
    assert first.terminal_failures[0].disposition is (
        ExactConsolidationDisposition.TERMINAL_FAILURE
    )
    assert replayed.new_attempt_count == 0
    assert replayed.replayed_count == 1
    assert replayed.terminal_failures[0].origin is (
        ExactConsolidationResultOrigin.REPLAYED
    )
    async with factory() as session:
        stored = await session.get(ConsolidationQueue, "exact-connectivity-row")
        assert stored is not None
        assert stored.status == "pending"
        assert stored.attempts == 1
        assert stored.claim_token is None
        assert stored.payload is not None
        marker = stored.payload.get("_exact_rebuild_disposition")
        assert marker is not None
        assert marker["reservation_lineage_id"] == lineage_id
        assert marker["disposition"] == "terminal_failure"
        assert (await session.execute(select(CanonicalDebt.id))).all() == []
        assert (await session.execute(select(ConsolidationDeadLetter.id))).all() == []


@pytest.mark.asyncio
async def test_exact_retry_claim_crash_recovers_marker_then_acks_attempt_two(
    queue_store,
    monkeypatch,
) -> None:
    from okto_pulse.community.adapters.materialization_health import (
        materialization_generation_key,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import (
        AppSetting,
        ConsolidationAudit,
        DomainEventRow,
        GlobalUpdateOutbox,
        KuzuNodeRef,
    )
    from okto_pulse.core.application.processors import consolidation
    from okto_pulse.core.ports.consolidation import ExactConsolidationDisposition

    factory, adapter = queue_store
    manifest_ref = "manifest-exact-retry-crash"
    source = f"rebuild:{manifest_ref}"
    lineage_id = "f" * 64
    retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    membership = {
        "content_hash": "7" * 64,
        "run_id": manifest_ref,
        "source_ref": "spec:retry-crash-spec",
        "source_version": "9",
    }
    marker = {
        "schema_version": 1,
        "queue_id": "exact-retry-crash-row",
        "board_id": BOARD_ID,
        "source": source,
        "reservation_lineage_id": lineage_id,
        "work_kind": "consolidate",
        "artifact_type": "spec",
        "artifact_id": "retry-crash-spec",
        "generation": 0,
        "membership_source_ref": membership["source_ref"],
        "membership_source_version": membership["source_version"],
        "membership_content_hash": membership["content_hash"],
        "attempt_ordinal": 1,
        "queue_attempts": 1,
        "disposition": "retry_scheduled",
        "retryable": True,
        "mutation_state": "unchanged",
        "error_code": "relational_projection_endpoint_pending",
        "error_message": "projection is not materialized",
        "next_retry_at": retry_at.isoformat(),
        "diagnostic_json": None,
    }
    payload = {
        "_rebuild_membership": membership,
        "_exact_rebuild_disposition": marker,
    }
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="exact-retry-crash-row",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id="retry-crash-spec",
                work_kind="consolidate",
                generation=0,
                payload=payload,
                priority="high",
                source=source,
                status="claimed",
                attempts=1,
                last_error=(
                    "relational_projection_endpoint_pending:"
                    "projection is not materialized"
                ),
                next_retry_at=retry_at,
                triggered_at=now - timedelta(minutes=1),
                claimed_at=now,
                claim_timeout_at=now + timedelta(minutes=5),
                worker_id="crashed-worker",
                claimed_by_session_id="crashed-worker",
                claim_token="crashed-claim-token",
            )
        )
        await session.commit()

    async def reservation(_context, *, board_id: str):  # noqa: ANN001
        assert board_id == BOARD_ID
        return source

    async def settle_deferred(*_args, **_kwargs) -> None:
        return None

    async def success(db, entry, **kwargs):  # noqa: ANN001
        deferred_session_ids = kwargs.get("deferred_session_ids")
        assert isinstance(deferred_session_ids, list)
        session_id = "retry-crash-success-session"
        committed_at = datetime.now(timezone.utc)
        previous_generation = "unmaterialized-v1"
        materialization_generation = "mg_retry_crash_success"
        generation_event_id = "00000000-0000-0000-0000-000000000301"
        db.add_all(
            [
                ConsolidationAudit(
                    session_id=session_id,
                    board_id=BOARD_ID,
                    artifact_id=entry.artifact_id,
                    artifact_type=entry.artifact_type,
                    agent_id="system:historical_consolidation",
                    started_at=committed_at,
                    committed_at=committed_at,
                    nodes_added=1,
                    nodes_updated=0,
                    nodes_superseded=0,
                    edges_added=0,
                    content_hash=membership["content_hash"],
                    undo_status="none",
                ),
                KuzuNodeRef(
                    id="retry-crash-success-ref",
                    session_id=session_id,
                    board_id=BOARD_ID,
                    kuzu_node_id="retry-crash-success-node",
                    kuzu_node_type="Entity",
                    operation="add",
                    timestamp=committed_at,
                ),
                GlobalUpdateOutbox(
                    id="retry-crash-success-outbox-row",
                    event_id="retry-crash-success-outbox",
                    board_id=BOARD_ID,
                    session_id=session_id,
                    event_type="consolidation_committed",
                    payload={
                        "session_id": session_id,
                        "artifact_id": entry.artifact_id,
                        "nodes_added": 1,
                        "nodes_updated": 0,
                        "nodes_superseded": 0,
                        "edges_added": 0,
                    },
                    retry_count=0,
                ),
                DomainEventRow(
                    id=generation_event_id,
                    event_type="kg.materialization_generation_advanced",
                    board_id=BOARD_ID,
                    actor_type="agent",
                    payload_json={
                        "correlation_id": session_id,
                        "previous_materialization_generation": previous_generation,
                        "materialization_generation": materialization_generation,
                    },
                    occurred_at=committed_at,
                ),
            ]
        )
        head = await db.get(AppSetting, materialization_generation_key(BOARD_ID))
        if head is None:
            db.add(
                AppSetting(
                    key=materialization_generation_key(BOARD_ID),
                    value=materialization_generation,
                )
            )
        else:
            head.value = materialization_generation
        await db.flush()
        deferred_session_ids.append(session_id)
        return True

    monkeypatch.setattr(adapter, "board_administrative_rebuild_source", reservation)
    monkeypatch.setattr(consolidation, "_process_queue_entry_serialized", success)
    monkeypatch.setattr(
        consolidation, "finalize_deferred_consolidation", settle_deferred
    )
    register_consolidation_persistence_port(adapter)
    scope = ConsolidationClaimScope(
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
    )
    try:
        recovery_processor = consolidation.ConsolidationProcessor(
            factory,
            batch_size=1,
        )
        assert (
            await recovery_processor.recover_exact_claims(
                claim_scope=scope,
                recovery_authority_probe=lambda: True,
            )
            == 1
        )
        async with factory() as session:
            recovered = await session.get(
                ConsolidationQueue,
                "exact-retry-crash-row",
            )
            assert recovered is not None
            assert recovered.status == "pending"
            assert recovered.claim_token is None
            assert recovered.payload == payload
            assert recovered.attempts == 1

        successor = consolidation.ConsolidationProcessor(factory, batch_size=1)
        completed = await successor.process_exact_batch(
            claim_scope=scope,
            reservation_authority_probe=lambda: True,
        )
    finally:
        reset_consolidation_persistence_port_for_tests()

    assert completed.acked_count == 1
    assert completed.rows[0].attempt_ordinal == 2
    assert completed.rows[0].disposition is ExactConsolidationDisposition.ACKED
    async with factory() as session:
        assert await session.get(ConsolidationQueue, "exact-retry-crash-row") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ("missing_token", "empty_token", "missing_expiry"))
async def test_incompatible_claim_is_refused_without_recovery_writes(queue_store, damage):
    factory, adapter = queue_store
    now = datetime.now(timezone.utc)
    async with factory() as session:
        invalid = _queue_row(row_id="incompatible-claim", work_kind="consolidate",
            generation=0, delete_event_id=None, claim_token="current-token")
        if damage == "missing_token":
            invalid.claim_token = None
        elif damage == "empty_token":
            invalid.claim_token = ""
        else:
            invalid.claim_timeout_at = None
        expired = _queue_row(row_id="native-expired", work_kind="consolidate",
            generation=0, delete_event_id=None, claim_token="expired-token", artifact_id="expired-artifact")
        expired.claim_timeout_at = now - timedelta(seconds=1)
        session.add_all([invalid, expired])
        await session.commit()
        before = (invalid.status, invalid.claim_token, invalid.claim_timeout_at,
                  expired.status, expired.claim_token, expired.claim_timeout_at)
        with pytest.raises(ValueError, match="consolidation_claim_format_incompatible"):
            await adapter.list_stale_claims(session, now=now)
        assert (invalid.status, invalid.claim_token, invalid.claim_timeout_at,
                expired.status, expired.claim_token, expired.claim_timeout_at) == before
        assert not session.dirty and not session.deleted


@pytest.mark.asyncio
async def test_native_expiry_selects_only_expired_claims_without_mutation(queue_store):
    factory, adapter = queue_store
    now = datetime.now(timezone.utc)
    async with factory() as session:
        rows = []
        for identity, seconds in (("expired", -1), ("boundary", 0), ("live", 60)):
            row = _queue_row(row_id=identity, work_kind="consolidate",
                generation=0, delete_event_id=None, claim_token=identity + "-token", artifact_id=identity + "-artifact")
            row.claim_timeout_at = now + timedelta(seconds=seconds)
            rows.append(row)
        session.add_all(rows)
        await session.commit()
        assert [row.id for row in await adapter.list_stale_claims(session, now=now)] == ["expired"]
        assert all(row.status == "claimed" for row in rows)
        assert not session.dirty and not session.deleted


@pytest.mark.asyncio
async def test_ack_is_exact_compare_and_delete_for_reconcile(queue_store):
    """Every divergent claim/generation/event leaves the row durable."""

    factory, adapter = queue_store
    async with factory() as session:
        session.add(
            _queue_row(
                row_id="ack-reconcile",
                work_kind="stale_reconcile",
                generation=3,
                delete_event_id="delete-event-3",
                claim_token="ack-token",
            )
        )
        await session.commit()

        exact = dict(
            entry_id="ack-reconcile",
            claim_token="ack-token",
            board_id=BOARD_ID,
            source="governed_delete",
            work_kind="stale_reconcile",
            generation=3,
            delete_event_id="delete-event-3",
        )
        for divergent in (
            {"claim_token": "old-token"},
            {"generation": 2},
            {"delete_event_id": "delete-event-2"},
            {"delete_event_id": None},
        ):
            assert not await adapter.ack_claimed_queue_entry(
                session, **{**exact, **divergent}
            )
            assert await session.get(ConsolidationQueue, "ack-reconcile") is not None

        assert await adapter.ack_claimed_queue_entry(session, **exact)
        assert await session.get(ConsolidationQueue, "ack-reconcile") is None
        assert not await adapter.ack_claimed_queue_entry(session, **exact)


@pytest.mark.asyncio
async def test_ack_uses_null_safe_delete_event_comparison_for_legacy(queue_store):
    """A legacy NULL event is distinct from every supplied event id."""

    factory, adapter = queue_store
    async with factory() as session:
        session.add(
            _queue_row(
                row_id="ack-legacy",
                work_kind="consolidate",
                generation=0,
                delete_event_id=None,
                claim_token="legacy-ack-token",
            )
        )
        await session.commit()

        assert not await adapter.ack_claimed_queue_entry(
            session,
            entry_id="ack-legacy",
            claim_token="legacy-ack-token",
            board_id=BOARD_ID,
            source="test",
            work_kind="consolidate",
            generation=0,
            delete_event_id="not-null",
        )
        assert await session.get(ConsolidationQueue, "ack-legacy") is not None
        assert await adapter.ack_claimed_queue_entry(
            session,
            entry_id="ack-legacy",
            claim_token="legacy-ack-token",
            board_id=BOARD_ID,
            source="test",
            work_kind="consolidate",
            generation=0,
            delete_event_id=None,
        )
        assert await session.get(ConsolidationQueue, "ack-legacy") is None


@pytest.mark.asyncio
async def test_ack_reads_deferred_live_marker_only_for_rebuild_source(
    queue_store,
) -> None:
    factory, adapter = queue_store
    async with factory() as session:
        row = _queue_row(
            row_id="ordinary-marker-payload",
            work_kind="consolidate",
            generation=0,
            delete_event_id=None,
            claim_token="ordinary-token",
        )
        row.payload = {
            "_rebuild_deferred_live": {
                "source": "event:spec.updated",
                "triggered_by_event": "spec.updated",
                "payload": {"revision": 2},
            }
        }
        session.add(row)
        await session.commit()

        assert await adapter.ack_claimed_queue_entry(
            session,
            entry_id=row.id,
            claim_token="ordinary-token",
            board_id=BOARD_ID,
            source="test",
            work_kind="consolidate",
            generation=0,
            delete_event_id=None,
        )
        assert await session.get(ConsolidationQueue, row.id) is None
