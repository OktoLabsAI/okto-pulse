"""Card 4 -- durable worker fencing and compare-and-delete acknowledgements."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone,
    Board,
    ConsolidationQueue,
)


BOARD_ID = "card4-worker-fence-board"
ARTIFACT_ID = "card4-deleted-spec"


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
        session.add(Board(id=BOARD_ID, name="Card 4", owner_id="tester"))
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
            generation=0,
            delete_event_id=None,
        )
        # Any tombstone permanently fences legacy canonical publication.
        assert not await adapter.queue_claim_is_current_and_unfenced(
            session, **legacy
        )

        reconcile = dict(
            entry_id="reconcile-g1",
            claim_token="reconcile-token",
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id=ARTIFACT_ID,
            work_kind="stale_reconcile",
            generation=1,
            delete_event_id="delete-event-1",
        )
        assert await adapter.queue_claim_is_current_and_unfenced(
            session, **reconcile
        )
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
        assert await adapter.queue_claim_is_current_and_unfenced(
            session, **identity
        )

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
            generation=0,
            delete_event_id=None,
        )
        await worker_session.commit()
        await asyncio.wait_for(delete_task, timeout=3)
    finally:
        await worker_session.rollback()
        await worker_session.close()

    async with factory() as verification:
        assert await verification.get(
            ConsolidationQueue,
            identity["entry_id"],
        ) is None
        assert await verification.get(
            ArtifactDeletionTombstone,
            "wal-worker-wins-tombstone",
        ) is not None


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
async def test_tokenless_migrated_claim_is_recovered_immediately(queue_store):
    """A pre-migration claim cannot remain stuck until its legacy timeout."""

    factory, adapter = queue_store
    async with factory() as session:
        session.add(
            _queue_row(
                row_id="tokenless-migrated-claim",
                work_kind="consolidate",
                generation=0,
                delete_event_id=None,
                claim_token=None,
                status="claimed",
            )
        )
        await session.commit()

        now = datetime.now(timezone.utc)
        stale = await adapter.list_stale_claims(
            session,
            now=now,
            legacy_cutoff=now - timedelta(hours=1),
        )

    assert [entry.id for entry in stale] == ["tokenless-migrated-claim"]


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
            generation=0,
            delete_event_id="not-null",
        )
        assert await session.get(ConsolidationQueue, "ack-legacy") is not None
        assert await adapter.ack_claimed_queue_entry(
            session,
            entry_id="ack-legacy",
            claim_token="legacy-ack-token",
            generation=0,
            delete_event_id=None,
        )
        assert await session.get(ConsolidationQueue, "ack-legacy") is None
