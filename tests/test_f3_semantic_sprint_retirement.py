"""Native Card listeners and refusal of unsupported Sprint mutation targets."""

import pytest
from sqlalchemy import event, select, text

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    SemanticSubjectVersionRow,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    bind_semantic_subject_actor,
    queue_semantic_subject_mutation,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from test_skb3_semantic_guideline_persistence import (
    _seed_semantic_authority,
    _sqlite_engine,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["status", "details", "test_scenario_ids", "create", "delete"]
)
async def test_card_listener_uses_only_native_subjects(tmp_path, operation):
    engine = _sqlite_engine(tmp_path / "native-card.db")
    sessions = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    try:
        async with sessions() as session, session.begin():
            board_id, spec_id, _, _ = await _seed_semantic_authority(
                session, metric_count=1, entity_type=PolicyEntityType.SPEC
            )
            session.add(
                Card(
                    id="native-card",
                    board_id=board_id,
                    spec_id=spec_id,
                    title="Card",
                    created_by="writer",
                )
            )
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with sessions() as session:
            async with CommunityUnitOfWork(
                session, actor=ActorContext("writer", "mcp", board_id=board_id)
            ) as uow:
                card = await session.get(Card, "native-card")
                if operation == "delete":
                    await session.delete(card)
                elif operation == "create":
                    session.add(
                        Card(
                            id="new-card",
                            board_id=board_id,
                            spec_id=spec_id,
                            title="Card",
                            created_by="writer",
                        )
                    )
                else:
                    setattr(
                        card,
                        operation,
                        {
                            "status": "in_progress",
                            "details": "New content",
                            "test_scenario_ids": ["scenario"],
                        }[operation],
                    )
                await uow.commit()
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert not any("sprints" in sql or "sprint_qa" in sql for sql in statements)
        async with sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM sqlite_master WHERE name = 'sprints'")
                )
                == 0
            )
            heads = tuple(
                (await session.execute(select(SemanticSubjectVersionRow))).scalars()
            )
            assert all(row.subject_type != "sprint" for row in heads)
            if operation in {"details", "test_scenario_ids", "create"}:
                assert any(
                    row.subject_type == "card"
                    and row.last_semantic_editor_id == "writer"
                    for row in heads
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_bound", [False, True])
async def test_explicit_sprint_mutation_queue_is_rejected(tmp_path, actor_bound):
    engine = _sqlite_engine(tmp_path / "native-queue.db")
    sessions = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with sessions() as session:
            if actor_bound:
                bind_semantic_subject_actor(
                    session, ActorContext("writer", "mcp", board_id="board")
                )
            original = dict(session.sync_session.info)
            with pytest.raises(
                ValueError, match="semantic_subject_bridge_entity_type_retired"
            ):
                queue_semantic_subject_mutation(
                    session,
                    entity_type=PolicyEntityType.SPRINT,
                    board_id="board",
                    subject_id="unsupported",
                )
            assert session.sync_session.info == original
    finally:
        await engine.dispose()
