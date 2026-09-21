"""Critical context keeps live gates and audit history without Sprint queries."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_critical_context import CommunitySqlAlchemyCriticalContextReader
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import ActivityLog, Base, Board, Card, CardDependency, Spec, Sprint
from okto_pulse.core.domain.realm import RealmScope
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
@pytest.mark.parametrize("migrated", [False, True])
async def test_live_fingerprint_ignores_sprint_but_keeps_context_and_historical_audit(kind, migrated):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements = []
    historical = {"critical_action": "sprint.closeout", "entity_id": "legacy",
                  "context_fingerprint": "ctx_sha256_v1:" + "a" * 64, "outcome": "allow"}
    policy = {"contract_version": "card-validation-compatibility/v1", "card_id": "card", "board_id": "board",
              "source_sprint_id": "legacy", "source_spec_id": "spec", "migration_id": "offline",
              "overrides": {"min_confidence": 90}} if migrated else None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = build_community_session_factory(engine)
        async with sessions() as db:
            db.add_all([
                Board(id="board", name="Board", owner_id="owner", realm_id=RealmScope.local().realm_id),
                Spec(id="spec", board_id="board", title="Spec", description="Original content", created_by="owner"),
                Sprint(id="legacy", board_id="board", spec_id="spec", title="Historical Sprint", created_by="owner"),
                Card(id="card", board_id="board", spec_id="spec", title="Task", created_by="owner",
                     sprint_id=None if migrated else "legacy", migrated_validation_policy=policy,
                     linked_test_task_ids=["regression"], test_scenario_ids=["scenario"]),
                Card(id="regression", board_id="board", spec_id="spec", title="Regression", created_by="owner", card_type="test"),
                CardDependency(id="dependency", card_id="card", depends_on_id="regression"),
                ActivityLog(id="audit", board_id="board", action="critical_context_guard_decision",
                            actor_type="user", actor_id="owner", actor_name="Owner", details=deepcopy(historical)),
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
            assert before["card"]["migrated_validation_policy"] == policy
            assert before["relations"]["depends_on_ids"] == ["regression"]
            assert before["relations"]["linked_test_task_ids"] == ["regression"]
            assert before["relations"]["test_scenario_ids"] == ["scenario"]
        else:
            assert before["relations"]["card_count"] == 2
            assert {card["id"] for card in before["relations"]["cards"]} == {"card", "regression"}
        async with sessions() as db:
            await db.execute(update(Sprint).values(title="Only historical content changed", validation_min_confidence=60))
            await db.commit()
        assert ContextFingerprintProvider.compute(await resolve()) == ContextFingerprintProvider.compute(before)
        async with sessions() as db:
            await db.execute(update(Spec).values(description="Live obligation changed"))
            await db.commit()
        assert ContextFingerprintProvider.compute(await resolve()) != ContextFingerprintProvider.compute(before)
        async with sessions() as db:
            assert (await db.get(ActivityLog, "audit")).details == historical
            assert (await db.get(Card, "card")).migrated_validation_policy == policy
    finally:
        await engine.dispose()
