"""Critical context keeps live gates and audit history without Sprint queries."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_critical_context import CommunitySqlAlchemyCriticalContextReader
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import ActivityLog, Board, Card, CardDependency, Spec
from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.services.critical_context_guard import ContextFingerprintProvider


@pytest.mark.asyncio
async def test_retired_context_is_refused_before_any_persistence_lookup():
    context = AsyncMock()
    with pytest.raises(ValueError, match="unsupported_full_context_entity_type"):
        await CommunitySqlAlchemyCriticalContextReader().resolve_full_context(context,
            board_id="board", entity_type="sprint", entity_id="historical", critical_action="sprint.closeout")
    context.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["card", "spec"])
async def test_native_fingerprint_keeps_context_and_audit_without_sprint(kind):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements = []
    audit = {"critical_action": "card.move_status", "entity_id": "card",
                  "context_fingerprint": "ctx_sha256_v1:" + "a" * 64, "outcome": "allow"}
    try:
        await initialize_current_schema(engine, current_schema_contract())
        sessions = build_community_session_factory(engine)
        async with sessions() as db:
            db.add_all([
                Board(id="board", name="Board", owner_id="owner", realm_id=RealmScope.local().realm_id),
                Spec(architecture_adoption=ArchitectureAdoptionScope(board_id="board", spec_id="spec", adopted_in_edition=1, actor_id="owner", inherited_resource_ids=()).model_dump(mode="json"),
                     execution_contract=new_execution_contract(board_id="board", spec_id="spec", edition=1, actor_id="owner", origin="new_spec"), id="spec", board_id="board", title="Spec", description="Original content", created_by="owner"),
                Card(id="card", board_id="board", spec_id="spec", title="Task", created_by="owner",
                     linked_test_task_ids=["regression"], test_scenario_ids=["scenario"]),
                Card(id="regression", board_id="board", spec_id="spec", title="Regression", created_by="owner", card_type="test"),
                CardDependency(id="dependency", card_id="card", depends_on_id="regression"),
                ActivityLog(id="audit", board_id="board", action="critical_context_guard_decision",
                            actor_type="user", actor_id="owner", actor_name="Owner", details=deepcopy(audit)),
            ])
            await db.commit()

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        async def resolve():
            statements.clear()
            event.listen(engine.sync_engine, "before_cursor_execute", capture)
            try:
                async with sessions() as db:
                    result = await CommunitySqlAlchemyCriticalContextReader().resolve_full_context(db,
                        board_id="board", entity_type=kind, entity_id=kind, critical_action=f"{kind}.move_status")
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", capture)
            assert not any("from sprints" in sql or "join sprints" in sql for sql in statements)
            assert not any(sql.lstrip().startswith(("insert ", "update ", "delete ")) for sql in statements)
            return result

        before = await resolve()
        assert "sprint" not in before["relations"] and "sprint_count" not in before["relations"]
        if kind == "card":
            assert "sprint_id" not in before["card"]
            assert "migrated_validation_policy" not in before["card"]
            assert before["relations"]["depends_on_ids"] == ["regression"]
            assert before["relations"]["linked_test_task_ids"] == ["regression"]
            assert before["relations"]["test_scenario_ids"] == ["scenario"]
        else:
            assert before["relations"]["card_count"] == 2
            assert {card["id"] for card in before["relations"]["cards"]} == {"card", "regression"}
        assert ContextFingerprintProvider.compute(await resolve()) == ContextFingerprintProvider.compute(before)
        async with sessions() as db:
            await db.execute(update(Spec).values(description="Live obligation changed"))
            await db.commit()
        assert ContextFingerprintProvider.compute(await resolve()) != ContextFingerprintProvider.compute(before)
        async with sessions() as db:
            assert (await db.get(ActivityLog, "audit")).details == audit
            assert not hasattr(await db.get(Card, "card"), "migrated_validation_policy")
    finally:
        await engine.dispose()
