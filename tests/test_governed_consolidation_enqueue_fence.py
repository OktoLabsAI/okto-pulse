"""Atomic tombstone fence coverage for consolidation queue admission."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_effects import (
    CommunitySqlAlchemyRelationalEffects,
)
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone,
    Board,
    ConsolidationDeadLetter,
    ConsolidationQueue,
)
from okto_pulse.core.ports.relational_effects import ConsolidationQueueUpsert
from okto_pulse.core.ports.reconcile_intent import ReconcileIntentCreate
from okto_pulse.core.ports.tombstone import DeletionTombstoneAdvance


async def _database(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'governed-enqueue.db'}"
    )
    install_community_sqlite_pragmas(engine)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Board(id="board-1", name="Board", owner_id="agent"))
        await session.commit()
    return engine, factory


def _upsert(
    artifact_id: str,
    *,
    priority: str = "normal",
    source: str = "event:spec.moved",
    triggered_by_event: str = "spec.moved",
    payload: dict | None = None,
) -> ConsolidationQueueUpsert:
    return ConsolidationQueueUpsert(
        board_id="board-1",
        artifact_type="card",
        artifact_id=artifact_id,
        priority=priority,
        source=source,
        triggered_by_event=triggered_by_event,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_queue_upsert_compiles_with_its_bound_sql_dialect() -> None:
    class _CompileResult:
        @staticmethod
        def scalar_one_or_none() -> str:
            return "compiled-row"

    class _CompileSession:
        def __init__(self, dialect) -> None:  # noqa: ANN001
            self._bind = SimpleNamespace(dialect=dialect)
            self.sql = ""

        def get_bind(self):  # noqa: ANN201
            return self._bind

        async def execute(self, statement):  # noqa: ANN001, ANN201
            self.sql = str(statement.compile(dialect=self._bind.dialect))
            return _CompileResult()

    adapter = CommunitySqlAlchemyRelationalEffects()
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        session = _CompileSession(dialect)
        assert await adapter.upsert_consolidation_queue_unless_tombstoned(
            session,
            _upsert("dialect-compile", payload={"revision": 1}),
        )
        assert "ON CONFLICT" in session.sql


@pytest.mark.asyncio
async def test_live_events_racing_rebuild_membership_survive_claim_and_ack(
    tmp_path,
):
    engine, factory = await _database(tmp_path)
    admission = CommunitySqlAlchemyRelationalEffects()
    persistence = CommunitySqlAlchemyConsolidationPersistence()
    try:
        async with factory() as session:
            session.add(
                ConsolidationQueue(
                    id="rebuild-member",
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id="card-race",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source="rebuild:manifest-1",
                    payload={"_rebuild_membership": {"run_id": "manifest-1"}},
                    status="pending",
                    attempts=0,
                )
            )
            await session.commit()

        async with factory() as session:
            first = await admission.upsert_consolidation_queue_unless_tombstoned(
                session,
                _upsert(
                    "card-race",
                    source="event:card.updated:first",
                    triggered_by_event="card.updated",
                    payload={"revision": 1},
                ),
            )
            await session.commit()
        assert first is True

        async with factory() as session:
            row = await session.get(ConsolidationQueue, "rebuild-member")
            assert row is not None
            assert row.status == "pending"
            assert row.source == "rebuild:manifest-1"
            assert row.payload == {
                "_rebuild_deferred_live": {
                    "source": "event:card.updated:first",
                    "triggered_by_event": "card.updated",
                    "payload": {"revision": 1},
                }
            }
            row.status = "claimed"
            row.claim_token = "stale-rebuild-claim"
            row.claimed_by_session_id = "stale-session"
            row.worker_id = "stale-worker"
            await session.commit()

        async with factory() as session:
            second = await admission.upsert_consolidation_queue_unless_tombstoned(
                session,
                _upsert(
                    "card-race",
                    source="event:card.updated:second",
                    triggered_by_event="card.updated",
                    payload={"revision": 2},
                ),
            )
            await session.commit()
        assert second is True

        async with factory() as session:
            row = await session.get(ConsolidationQueue, "rebuild-member")
            assert row is not None
            assert row.status == "pending"
            assert row.claim_token is None
            assert row.source == "rebuild:manifest-1"
            assert row.payload["_rebuild_deferred_live"]["source"] == (
                "event:card.updated:second"
            )
            assert row.payload["_rebuild_deferred_live"]["payload"] == {"revision": 2}
            row.status = "claimed"
            row.claim_token = "current-rebuild-claim"
            row.claimed_by_session_id = "current-session"
            row.worker_id = "current-worker"
            await session.commit()

        async with factory() as session:
            acknowledged = await persistence.ack_claimed_queue_entry(
                session,
                entry_id="rebuild-member",
                claim_token="current-rebuild-claim",
                board_id="board-1",
                source="rebuild:manifest-1",
                work_kind="consolidate",
                generation=0,
                delete_event_id=None,
            )
            await session.commit()
        assert acknowledged is True

        async with factory() as session:
            row = await session.get(ConsolidationQueue, "rebuild-member")
            assert row is not None
            assert row.status == "pending"
            assert row.source == "event:card.updated:second"
            assert row.payload == {"revision": 2}
            assert row.claim_token is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tombstone_suppresses_insert_and_terminal_reopen_preserving_intent(
    tmp_path,
):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyRelationalEffects()
    try:
        async with factory() as session:
            session.add_all(
                [
                    ArtifactDeletionTombstone(
                        id="tombstone-new",
                        board_id="board-1",
                        artifact_type="card",
                        artifact_id="deleted-without-work",
                        generation=3,
                        delete_event_id="delete-new",
                    ),
                    ArtifactDeletionTombstone(
                        id="tombstone-terminal",
                        board_id="board-1",
                        artifact_type="card",
                        artifact_id="deleted-with-terminal-work",
                        generation=7,
                        delete_event_id="delete-terminal",
                    ),
                    ConsolidationQueue(
                        id="terminal-consolidate",
                        board_id="board-1",
                        artifact_type="card",
                        artifact_id="deleted-with-terminal-work",
                        work_kind="consolidate",
                        generation=0,
                        priority="low",
                        source="pre-delete",
                        triggered_by_event="card.moved",
                        status="failed",
                        attempts=4,
                        last_error="terminal error",
                    ),
                    ConsolidationQueue(
                        id="durable-stale-intent",
                        board_id="board-1",
                        artifact_type="card",
                        artifact_id="deleted-with-terminal-work",
                        work_kind="stale_reconcile",
                        generation=7,
                        payload={"source_refs": ["card:deleted-with-terminal-work"]},
                        delete_event_id="delete-terminal",
                        priority="high",
                        source="governed_delete",
                        status="pending",
                    ),
                ]
            )
            await session.commit()

        async with factory() as session:
            inserted = await adapter.upsert_consolidation_queue_unless_tombstoned(
                session, _upsert("deleted-without-work")
            )
            reopened = await adapter.upsert_consolidation_queue_unless_tombstoned(
                session,
                _upsert(
                    "deleted-with-terminal-work",
                    priority="high",
                    source="late-event",
                ),
            )
            await session.commit()

        assert inserted is False
        assert reopened is False

        async with factory() as session:
            absent_rows = (
                (
                    await session.execute(
                        select(ConsolidationQueue).where(
                            ConsolidationQueue.artifact_id == "deleted-without-work"
                        )
                    )
                )
                .scalars()
                .all()
            )
            terminal_rows = (
                (
                    await session.execute(
                        select(ConsolidationQueue)
                        .where(
                            ConsolidationQueue.artifact_id
                            == "deleted-with-terminal-work"
                        )
                        .order_by(ConsolidationQueue.work_kind)
                    )
                )
                .scalars()
                .all()
            )
            dead_letters = await session.scalar(
                select(func.count(ConsolidationDeadLetter.id))
            )

        assert absent_rows == []
        assert len(terminal_rows) == 2
        consolidate = next(
            row for row in terminal_rows if row.work_kind == "consolidate"
        )
        stale_intents = [
            row for row in terminal_rows if row.work_kind == "stale_reconcile"
        ]
        assert consolidate.status == "failed"
        assert consolidate.attempts == 4
        assert consolidate.last_error == "terminal error"
        assert consolidate.priority == "low"
        assert consolidate.source == "pre-delete"
        assert len(stale_intents) == 1
        assert stale_intents[0].id == "durable-stale-intent"
        assert stale_intents[0].generation == 7
        assert stale_intents[0].status == "pending"
        assert dead_letters == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tombstone_for_another_artifact_does_not_block_admission(tmp_path):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyRelationalEffects()
    try:
        async with factory() as session:
            session.add(Board(id="board-2", name="Other Board", owner_id="agent"))
            await session.flush()
            session.add_all(
                [
                    ArtifactDeletionTombstone(
                        id="unrelated-artifact-tombstone",
                        board_id="board-1",
                        artifact_type="card",
                        artifact_id="deleted-card",
                        generation=1,
                        delete_event_id="delete-unrelated-artifact",
                    ),
                    ArtifactDeletionTombstone(
                        id="unrelated-type-tombstone",
                        board_id="board-1",
                        artifact_type="spec",
                        artifact_id="live-card",
                        generation=2,
                        delete_event_id="delete-unrelated-type",
                    ),
                    ArtifactDeletionTombstone(
                        id="unrelated-board-tombstone",
                        board_id="board-2",
                        artifact_type="card",
                        artifact_id="live-card",
                        generation=3,
                        delete_event_id="delete-unrelated-board",
                    ),
                ]
            )
            await session.commit()

        async with factory() as session:
            changed = await adapter.upsert_consolidation_queue_unless_tombstoned(
                session, _upsert("live-card")
            )
            await session.commit()

        assert changed is True
        async with factory() as session:
            row = (
                await session.execute(
                    select(ConsolidationQueue).where(
                        ConsolidationQueue.artifact_id == "live-card"
                    )
                )
            ).scalar_one()
        assert row.work_kind == "consolidate"
        assert row.status == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_atomic_admission_coalesces_active_work_and_reopens_terminal_work(
    tmp_path,
):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyRelationalEffects()
    statements: list[str] = []

    def capture_statement(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,  # noqa: ANN001
    ) -> None:
        statements.append(statement)

    try:
        async with factory() as session:
            event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
            try:
                inserted = await adapter.upsert_consolidation_queue_unless_tombstoned(
                    session, _upsert("card-1")
                )
            finally:
                event.remove(
                    engine.sync_engine, "before_cursor_execute", capture_statement
                )
            await session.commit()

        assert inserted is True
        assert len(statements) == 1
        insert_sql = " ".join(statements[0].split())
        assert insert_sql.upper().startswith("INSERT INTO CONSOLIDATION_QUEUE")
        assert " SELECT " in insert_sql.upper()
        assert re.search(r"\bNOT\s*\(?\s*EXISTS\b", insert_sql, re.IGNORECASE)
        assert " ON CONFLICT " in insert_sql.upper()
        assert not any(
            statement.lstrip().upper().startswith("SELECT")
            and "artifact_deletion_tombstones" in statement
            for statement in statements
        )

        async with factory() as session:
            active = (
                await session.execute(
                    select(ConsolidationQueue).where(
                        ConsolidationQueue.artifact_id == "card-1"
                    )
                )
            ).scalar_one()
            original_id = active.id
            replayed = await adapter.upsert_consolidation_queue_unless_tombstoned(
                session,
                _upsert(
                    "card-1",
                    priority="high",
                    source="active-replay",
                    triggered_by_event="card.updated",
                ),
            )
            await session.commit()

        assert replayed is False
        assert active.id == original_id
        assert active.priority == "normal"
        assert active.source == "event:spec.moved"
        assert active.triggered_by_event == "spec.moved"
        assert active.status == "pending"

        async with factory() as session:
            claimed = await session.get(ConsolidationQueue, original_id)
            assert claimed is not None
            claimed.status = "claimed"
            claimed.attempts = 2
            claimed.claim_token = "stale-claim-token"
            claimed.claimed_by_session_id = "stale-session"
            claimed.worker_id = "stale-worker"
            claimed.last_error = "prior-attempt"
            await session.commit()

        async with factory() as session:
            invalidated = await adapter.upsert_consolidation_queue_unless_tombstoned(
                session,
                _upsert(
                    "card-1",
                    priority="high",
                    source="event-during-claim",
                    triggered_by_event="card.cancelled",
                ),
            )
            await session.commit()

        assert invalidated is True
        async with factory() as session:
            pending_after_race = await session.get(ConsolidationQueue, original_id)
        assert pending_after_race is not None
        assert pending_after_race.status == "pending"
        assert pending_after_race.attempts == 0
        assert pending_after_race.claim_token is None
        assert pending_after_race.claimed_by_session_id is None
        assert pending_after_race.worker_id is None
        assert pending_after_race.last_error is None
        assert pending_after_race.priority == "high"
        assert pending_after_race.source == "event-during-claim"
        assert pending_after_race.triggered_by_event == "card.cancelled"

        async with factory() as session:
            terminal = await session.get(ConsolidationQueue, original_id)
            assert terminal is not None
            terminal.status = "failed"
            terminal.attempts = 5
            terminal.last_error = "retry exhausted"
            await session.commit()

        async with factory() as session:
            reopened = await adapter.upsert_consolidation_queue_unless_tombstoned(
                session,
                _upsert(
                    "card-1",
                    priority="high",
                    source="terminal-reopen",
                    triggered_by_event="card.updated",
                ),
            )
            await session.commit()

        assert reopened is True
        async with factory() as session:
            row = await session.get(ConsolidationQueue, original_id)
        assert row is not None
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.last_error is None
        assert row.priority == "high"
        assert row.source == "terminal-reopen"
        assert row.triggered_by_event == "card.updated"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_both_sqlite_race_linearizations_converge_to_only_stale_intent(
    tmp_path,
):
    """BR3: enqueue-first and delete-first histories cannot resurrect work."""

    engine, factory = await _database(tmp_path)
    admission = CommunitySqlAlchemyRelationalEffects()
    deletion = CommunitySqlAlchemyConsolidationPersistence()

    async def governed_delete(artifact_id: str, event_id: str) -> None:
        async with factory() as session:
            await deletion.discard_artifact_work(
                session,
                board_id="board-1",
                artifact_type="card",
                artifact_id=artifact_id,
            )
            tombstone = await deletion.advance_deletion_tombstone(
                session,
                DeletionTombstoneAdvance(
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id=artifact_id,
                    delete_event_id=event_id,
                ),
            )
            await deletion.persist_reconcile_intent(
                session,
                ReconcileIntentCreate(
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id=artifact_id,
                    generation=tombstone.generation,
                    delete_event_id=event_id,
                    source_refs=(f"card:{artifact_id}",),
                ),
            )
            await session.commit()

    try:
        # Enqueue obtains SQLite's writer slot first.  The later governed
        # deletion removes that legacy row and creates the permanent fence.
        async with factory() as session:
            assert (
                await admission.upsert_consolidation_queue_unless_tombstoned(
                    session, _upsert("enqueue-wins")
                )
                is True
            )
            await session.commit()
        await governed_delete("enqueue-wins", "delete-after-enqueue")

        # Governed deletion obtains the writer slot first.  The admission
        # statement observes the committed tombstone in its NOT EXISTS arm.
        await governed_delete("delete-wins", "delete-before-enqueue")
        async with factory() as session:
            assert (
                await admission.upsert_consolidation_queue_unless_tombstoned(
                    session, _upsert("delete-wins")
                )
                is False
            )
            await session.commit()

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsolidationQueue)
                        .where(
                            ConsolidationQueue.artifact_id.in_(
                                ("enqueue-wins", "delete-wins")
                            )
                        )
                        .order_by(ConsolidationQueue.artifact_id)
                    )
                )
                .scalars()
                .all()
            )
            dead_letters = await session.scalar(
                select(func.count(ConsolidationDeadLetter.id))
            )

        assert [
            (row.artifact_id, row.work_kind, row.generation, row.status) for row in rows
        ] == [
            ("delete-wins", "stale_reconcile", 1, "pending"),
            ("enqueue-wins", "stale_reconcile", 1, "pending"),
        ]
        assert dead_letters == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_enqueue_and_governed_delete_converge_under_wal(tmp_path):
    """BR3: competing production-style SQLite writers converge without retry."""

    engine, factory = await _database(tmp_path)
    admission = CommunitySqlAlchemyRelationalEffects()
    deletion = CommunitySqlAlchemyConsolidationPersistence()
    start = asyncio.Event()
    artifact_id = "concurrent-card"
    delete_event_id = "concurrent-delete"

    async def enqueue() -> bool:
        await start.wait()
        async with factory() as session:
            changed = await admission.upsert_consolidation_queue_unless_tombstoned(
                session, _upsert(artifact_id)
            )
            await session.commit()
            return changed

    async def governed_delete() -> None:
        await start.wait()
        async with factory() as session:
            await deletion.discard_artifact_work(
                session,
                board_id="board-1",
                artifact_type="card",
                artifact_id=artifact_id,
            )
            tombstone = await deletion.advance_deletion_tombstone(
                session,
                DeletionTombstoneAdvance(
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id=artifact_id,
                    delete_event_id=delete_event_id,
                ),
            )
            await deletion.persist_reconcile_intent(
                session,
                ReconcileIntentCreate(
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id=artifact_id,
                    generation=tombstone.generation,
                    delete_event_id=delete_event_id,
                    source_refs=(f"card:{artifact_id}",),
                ),
            )
            await session.commit()

    try:
        enqueue_task = asyncio.create_task(enqueue())
        delete_task = asyncio.create_task(governed_delete())
        start.set()
        changed, _ = await asyncio.gather(enqueue_task, delete_task)
        assert isinstance(changed, bool)

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ConsolidationQueue).where(
                            ConsolidationQueue.board_id == "board-1",
                            ConsolidationQueue.artifact_type == "card",
                            ConsolidationQueue.artifact_id == artifact_id,
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert [
            (row.work_kind, row.generation, row.delete_event_id) for row in rows
        ] == [("stale_reconcile", 1, delete_event_id)]
    finally:
        await engine.dispose()
