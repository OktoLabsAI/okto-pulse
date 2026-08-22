from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_effects import (
    CommunitySqlAlchemyRelationalEffects,
    register_community_relational_effects,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    ConsolidationQueue,
    KGTickRun,
)
from okto_pulse.core.ports.relational_effects import (
    ConsolidationQueueUpsert,
    KGTickRunUpsert,
    SpecDependencyProjectionQueueMetadata,
    get_relational_effects_port,
)
from okto_pulse.core.ports.consolidation import get_consolidation_persistence_port
from okto_pulse.core.ports.reconcile_intent import get_reconcile_intent_port
from okto_pulse.core.ports.tombstone import get_tombstone_port
from okto_pulse.core.ports.knowledge_propagation import (
    get_knowledge_mutation_audit_sink,
    get_knowledge_propagation_port,
)


@pytest.mark.asyncio
async def test_af30_3c_community_relational_effects_register_and_persist(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'effects.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    registered = register_community_relational_effects()
    assert isinstance(registered, CommunitySqlAlchemyRelationalEffects)
    assert get_relational_effects_port() is registered
    governed_deletion_persistence = get_consolidation_persistence_port()
    assert get_tombstone_port() is governed_deletion_persistence
    assert get_reconcile_intent_port() is governed_deletion_persistence
    knowledge_propagation = get_knowledge_propagation_port()
    assert get_knowledge_mutation_audit_sink() is knowledge_propagation

    board_id = str(uuid.uuid4())
    tick_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    dependency_payload = SpecDependencyProjectionQueueMetadata(
        mutation_event_id="dependency-mutation-1",
        mutation_event_type="spec.dependency_added",
        dependency_id="dependency-1",
        projection_owner_spec_id="spec-1",
        target_role="projection_owner",
    ).to_payload()
    async with factory() as session:
        session.add(Board(id=board_id, name="AF30-3cR", owner_id="test-owner"))
        await session.commit()

        port = get_relational_effects_port()
        assert await port.list_board_ids(session) == [board_id]
        assert (
            await port.count_active_consolidation_queue(session, board_id=board_id) == 0
        )
        changed = await port.upsert_consolidation_queue_unless_tombstoned(
            session,
            ConsolidationQueueUpsert(
                board_id=board_id,
                artifact_type="spec",
                artifact_id="spec-1",
                priority="normal",
                source="event:spec.dependency_added",
                triggered_by_event="spec.dependency_added",
                payload=dependency_payload,
            ),
        )
        assert changed is True
        await port.upsert_kg_tick_run(
            session,
            KGTickRunUpsert(
                tick_id=tick_id,
                started_at=now,
                completed_at=now,
                nodes_recomputed=3,
                duration_ms=12.5,
                boards_processed=1,
            ),
        )
        await session.commit()

        queue_row = (await session.execute(select(ConsolidationQueue))).scalar_one()
        tick_row = (await session.execute(select(KGTickRun))).scalar_one()
        latest_tick = await port.read_latest_kg_tick_completed_at(session)

    await engine.dispose()

    assert queue_row.board_id == board_id
    assert queue_row.status == "pending"
    assert queue_row.triggered_by_event == "spec.dependency_added"
    assert queue_row.payload == dependency_payload
    assert tick_row.tick_id == tick_id
    assert tick_row.nodes_recomputed == 3
    assert latest_tick is not None
    if latest_tick.tzinfo is None:
        latest_tick = latest_tick.replace(tzinfo=timezone.utc)
    assert latest_tick == now
