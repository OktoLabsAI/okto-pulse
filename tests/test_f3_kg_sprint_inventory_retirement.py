"""F3: inventory/retry preserve Card work without reading or reviving Sprint."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.kg_operational import (
    CommunitySqlAlchemyKGWorkerQueue,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_models import Board, ConsolidationDeadLetter, ConsolidationQueue, Spec
from legacy_sprint_schema import Card as LegacyCard, Base, Sprint
from okto_pulse.core.domain.enums import SpecStatus
from okto_pulse.community.adapters.legacy_sprint_values import HistoricalSprintStatus as SprintStatus
from okto_pulse.core.domain.realm import RealmScope


@pytest.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inventory.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False,
        sync_session_class=CommunitySemanticSession, info={"realm_scope": RealmScope.local()})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        db.add(Board(id="b", name="Board", owner_id="owner"))
        db.add(Spec(id="spec", board_id="b", title="Spec", status=SpecStatus.DONE, created_by="owner"))
        db.add(Sprint(id="old", board_id="b", spec_id="spec", title="Historical", status=SprintStatus.CLOSED, created_by="owner"))
        db.add(LegacyCard(id="card", board_id="b", spec_id="spec", sprint_id="old", title="Card", created_by="owner"))
        db.add(LegacyCard(id="orphan", board_id="b", spec_id=None, sprint_id="old", title="No Spec", created_by="owner"))
        for kind, identity in (("spec", "spec"), ("sprint", "old"), ("card", "card")):
            db.add(ConsolidationQueue(id=f"q-{kind}", board_id="b", artifact_type=kind,
                artifact_id=identity, status="failed", source="original", last_error="original failure"))
            db.add(ConsolidationDeadLetter(id=f"d-{kind}", board_id="b", artifact_type=kind,
                artifact_id=identity, errors=[{"message": "original"}], attempts=3))
        await db.commit()
    try:
        yield engine, factory
    finally:
        await engine.dispose()




@pytest.mark.asyncio
async def test_explicit_mixed_dlq_selection_is_atomic_and_implicit_replay_skips_sprint(database):
    _, factory = database
    async with factory() as db:
        queue = CommunitySqlAlchemyKGWorkerQueue()
        result = await queue.reprocess_dead_letter_rows(db, board_id="b",
            dead_letter_ids=("d-card", "d-sprint"), limit=10)
        assert result["blocked"] and not result["mutated"]
        assert result["error"] == "retired_sprint_work_requires_offline_cutover"
        assert (await db.get(ConsolidationQueue, "q-card")).status == "failed"
        assert await db.get(ConsolidationDeadLetter, "d-card") is not None
        result = await queue.reprocess_dead_letter_rows(db, board_id="b", dead_letter_ids=(), limit=10)
        assert result["success"]
        await db.flush()
        assert (await db.get(ConsolidationQueue, "q-card")).status == "pending"
        assert (await db.get(ConsolidationQueue, "q-sprint")).status == "failed"
        rows = (await db.execute(select(ConsolidationDeadLetter))).scalars().all()
        assert [row.id for row in rows] == ["d-sprint"]
        assert rows[0].errors == [{"message": "original"}]
