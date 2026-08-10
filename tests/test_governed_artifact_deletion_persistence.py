"""Community persistence coverage for governed artifact deletion intents."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
    ArtifactDeletionTombstone,
    Board,
    CanonicalDebt,
    ConsolidationDeadLetter,
    ConsolidationQueue,
)
from okto_pulse.community.adapters.sqlalchemy_schema_contract import (
    COMMUNITY_SCHEMA_EXTENSION_TABLES,
)
from okto_pulse.core.ports.reconcile_intent import ReconcileIntentCreate
from okto_pulse.core.ports.tombstone import DeletionTombstoneAdvance


async def _database(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'governed-deletion.db'}"
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Board(id="board-1", name="Board", owner_id="agent"))
        await session.commit()
    return engine, factory


def _advance(*, artifact_id: str, event_id: str) -> DeletionTombstoneAdvance:
    return DeletionTombstoneAdvance(
        board_id="board-1",
        artifact_type="card",
        artifact_id=artifact_id,
        delete_event_id=event_id,
    )


def _intent(
    *, artifact_id: str, event_id: str, generation: int
) -> ReconcileIntentCreate:
    return ReconcileIntentCreate(
        board_id="board-1",
        artifact_type="card",
        artifact_id=artifact_id,
        generation=generation,
        delete_event_id=event_id,
        source_refs=(f"card:{artifact_id}",),
    )


@pytest.mark.asyncio
async def test_ts_28c3edda_tombstone_and_intent_advance_and_replay_idempotently(
    tmp_path,
):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyConsolidationPersistence()

    async with factory() as session:
        first = await adapter.advance_deletion_tombstone(
            session, _advance(artifact_id="card-1", event_id="delete-1")
        )
        replay = await adapter.advance_deletion_tombstone(
            session, _advance(artifact_id="card-1", event_id="delete-1")
        )
        assert first.generation == replay.generation == 1

        intent = await adapter.persist_reconcile_intent(
            session,
            _intent(
                artifact_id="card-1",
                event_id="delete-1",
                generation=first.generation,
            ),
        )
        intent_replay = await adapter.persist_reconcile_intent(
            session,
            _intent(
                artifact_id="card-1",
                event_id="delete-1",
                generation=first.generation,
            ),
        )
        assert intent_replay == intent
        await session.commit()

    async with factory() as session:
        second = await adapter.advance_deletion_tombstone(
            session, _advance(artifact_id="card-1", event_id="delete-2")
        )
        assert second.generation == 2
        second_intent = await adapter.persist_reconcile_intent(
            session,
            _intent(
                artifact_id="card-1",
                event_id="delete-2",
                generation=second.generation,
            ),
        )
        await session.commit()

    async with factory() as session:
        tombstone = (
            await session.execute(select(ArtifactDeletionTombstone))
        ).scalar_one()
        assert tombstone.generation == 2
        assert tombstone.delete_event_id == "delete-2"
        assert tombstone.created_at is not None
        assert tombstone.updated_at is not None
        rows = (
            await session.execute(
                select(ConsolidationQueue)
                .where(ConsolidationQueue.work_kind == "stale_reconcile")
                .order_by(ConsolidationQueue.generation)
            )
        ).scalars().all()
        assert [(row.generation, row.delete_event_id) for row in rows] == [
            (1, "delete-1"),
            (2, "delete-2"),
        ]
        assert rows[0].payload == {
            "schema_version": 1,
            "delete_event_id": "delete-1",
            "source_refs": ["card:card-1"],
        }
        assert second_intent.intent_id == rows[1].id

    await engine.dispose()


@pytest.mark.asyncio
async def test_ts_28c3edda_rejects_divergent_governed_delete_identity(tmp_path):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyConsolidationPersistence()

    async with factory() as session:
        with pytest.raises(ValueError, match="governed_deletion_identity_invalid"):
            await adapter.advance_deletion_tombstone(
                session,
                DeletionTombstoneAdvance(
                    board_id="board-1",
                    artifact_type="story",
                    artifact_id="story-1",
                    delete_event_id="delete-story",
                ),
            )

        tombstone = await adapter.advance_deletion_tombstone(
            session,
            _advance(artifact_id="card-1", event_id="delete-1"),
        )
        with pytest.raises(ValueError, match="governed_reconcile_intent_invalid"):
            await adapter.persist_reconcile_intent(
                session,
                ReconcileIntentCreate(
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id="card-1",
                    generation=tombstone.generation,
                    delete_event_id="delete-1",
                    source_refs=("card:card-1", "card:other"),
                ),
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_ts_7eaf5452_discard_preserves_durable_stale_intents(tmp_path):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyConsolidationPersistence()

    async with factory() as session:
        session.add_all(
            [
                ConsolidationQueue(
                    id="consolidate",
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id="card-1",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source="test",
                    status="pending",
                ),
                ConsolidationQueue(
                    id="stale",
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id="card-1",
                    work_kind="stale_reconcile",
                    generation=1,
                    delete_event_id="old-delete",
                    priority="high",
                    source="governed_delete",
                    status="pending",
                ),
                ConsolidationQueue(
                    id="sweep",
                    board_id="board-1",
                    artifact_type="board",
                    artifact_id="board-1",
                    work_kind="stale_sweep",
                    generation=0,
                    priority="high",
                    source="governed_delete",
                    status="pending",
                ),
                ConsolidationDeadLetter(
                    id="dead",
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id="card-1",
                    attempts=3,
                ),
                CanonicalDebt(
                    id="debt",
                    board_id="board-1",
                    artifact_type="card",
                    artifact_id="card-1",
                    source_ref="card:card-1",
                    content_hash="hash",
                    target_status="done",
                ),
            ]
        )
        await session.commit()

    async with factory() as session:
        await adapter.discard_artifact_work(
            session,
            board_id="board-1",
            artifact_type="card",
            artifact_id="card-1",
        )
        await session.commit()

    async with factory() as session:
        queue_ids = set(
            (
                await session.execute(select(ConsolidationQueue.id))
            ).scalars().all()
        )
        assert queue_ids == {"stale", "sweep"}
        assert await session.scalar(select(func.count(ConsolidationDeadLetter.id))) == 0
        assert await session.scalar(select(func.count(CanonicalDebt.id))) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_ts_09c73b12_uow_rollback_restores_discard_and_durable_writes(
    tmp_path,
):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyConsolidationPersistence()

    async with factory() as session:
        session.add(
            ConsolidationQueue(
                id="legacy",
                board_id="board-1",
                artifact_type="card",
                artifact_id="card-1",
                priority="high",
                source="test",
                status="pending",
            )
        )
        await session.commit()

    async with factory() as session:
        await adapter.discard_artifact_work(
            session,
            board_id="board-1",
            artifact_type="card",
            artifact_id="card-1",
        )
        tombstone = await adapter.advance_deletion_tombstone(
            session, _advance(artifact_id="card-1", event_id="delete-rollback")
        )
        await adapter.persist_reconcile_intent(
            session,
            _intent(
                artifact_id="card-1",
                event_id="delete-rollback",
                generation=tombstone.generation,
            ),
        )
        await session.rollback()

    async with factory() as session:
        assert await session.get(ConsolidationQueue, "legacy") is not None
        assert await session.scalar(select(func.count(ArtifactDeletionTombstone.id))) == 0
        assert (
            await session.scalar(
                select(func.count(ConsolidationQueue.id)).where(
                    ConsolidationQueue.work_kind == "stale_reconcile"
                )
            )
            == 0
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_governed_deletion_conflicts_fail_closed(tmp_path):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyConsolidationPersistence()

    async with factory() as session:
        tombstone = await adapter.advance_deletion_tombstone(
            session, _advance(artifact_id="card-1", event_id="delete-1")
        )
        await adapter.persist_reconcile_intent(
            session,
            _intent(
                artifact_id="card-1",
                event_id="delete-1",
                generation=tombstone.generation,
            ),
        )
        await session.commit()

    async with factory() as session:
        row = (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.work_kind == "stale_reconcile"
                )
            )
        ).scalar_one()
        row.payload = {"schema_version": 1, "source_refs": ["card:other"]}
        await session.commit()

    async with factory() as session:
        with pytest.raises(RuntimeError, match="replay_conflict"):
            await adapter.persist_reconcile_intent(
                session,
                _intent(artifact_id="card-1", event_id="delete-1", generation=1),
            )
        await session.rollback()

    async with factory() as session:
        with pytest.raises(RuntimeError, match="delete_event_conflict"):
            await adapter.advance_deletion_tombstone(
                session, _advance(artifact_id="card-2", event_id="delete-1")
            )
        await session.rollback()

    await engine.dispose()


def test_deletion_tombstone_participates_in_schema_and_revision_manifests():
    assert "artifact_deletion_tombstones" in COMMUNITY_SCHEMA_EXTENSION_TABLES
    assert "artifact_deletion_tombstones" in (
        GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES
    )
    assert GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION == (
        "gdsr-trigger-manifest-v6"
    )
