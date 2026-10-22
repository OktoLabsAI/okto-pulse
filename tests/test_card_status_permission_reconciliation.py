"""Startup materializes the return permission without widening old authority."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.card_status_permission_reconciliation import (
    reconcile_card_status_permission_documents,
)
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.permission_preset_reconciliation import (
    reconcile_community_permission_presets,
)
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent, AgentBoard, Board, PermissionIntroductionAudit, PermissionPreset,
)
from okto_pulse.core.ports.permission_policy import get_permission_flag


@pytest.mark.asyncio
async def test_custom_same_name_direct_grants_and_board_ceilings_are_converted(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'permissions.db'}")
    try:
        await initialize_current_schema(engine, current_schema_contract())
        sessions = build_community_session_factory(engine)
        await reconcile_community_permission_presets(session_factory=sessions)
        async with sessions() as session:
            builtin = (await session.execute(select(PermissionPreset).where(
                PermissionPreset.name == "Full Control", PermissionPreset.is_builtin.is_(True),
            ))).scalar_one()
            session.add(Board(id="board", realm_id="local", name="Board", owner_id="owner"))
            session.add(Board(id="board-invalid", realm_id="local", name="Other", owner_id="owner"))
            session.add(Board(id="board-allow", realm_id="local", name="Allowed", owner_id="owner"))
            session.add(PermissionPreset(
                id="custom-same-name", name="Full Control", owner_id="owner",
                is_builtin=False, base_preset_id=builtin.id,
                flags={"card": {"move": {"in_progress_to_started": False}}},
            ))
            session.add(PermissionPreset(
                id="custom-allow", name="Custom", owner_id="owner",
                is_builtin=False, base_preset_id=builtin.id, flags={},
            ))
            session.add(Agent(
                id="agent", name="Agent", api_key="marker", api_key_hash="hash",
                created_by="owner", preset_id=builtin.id,
                permission_flags={"card": {"move": {"started_to_not_started": False}}},
            ))
            session.add(Agent(
                id="agent-allow", name="Allowed Agent", api_key="marker-allow", api_key_hash="hash-allow",
                created_by="owner", preset_id=builtin.id, permission_flags={},
            ))
            session.add(AgentBoard(
                id="ceiling-deny", agent_id="agent", board_id="board",
                granted_by="owner",
                permission_overrides={"card": {"move": {"started_to_not_started": False}}},
            ))
            session.add(AgentBoard(
                id="ceiling-invalid", agent_id="agent", board_id="board-invalid",
                granted_by="owner", permission_overrides={"card": {"move": "bad"}},
            ))
            session.add(AgentBoard(
                id="ceiling-allow", agent_id="agent-allow", board_id="board-allow",
                granted_by="owner", permission_overrides={"card": {
                    "interact_in": {"in_progress": True, "started": True},
                    "move": {
                        "in_progress_to_started": True,
                        "started_to_not_started": True,
                    },
                }},
            ))
            await session.commit()
        assert await reconcile_card_status_permission_documents(session_factory=sessions) == 6
        assert await reconcile_card_status_permission_documents(session_factory=sessions) == 0
        async with sessions() as session:
            custom_deny = await session.get(PermissionPreset, "custom-same-name")
            custom_allow = await session.get(PermissionPreset, "custom-allow")
            agent = await session.get(Agent, "agent")
            ceiling = await session.get(AgentBoard, "ceiling-deny")
            invalid = await session.get(AgentBoard, "ceiling-invalid")
            allowed_agent = await session.get(Agent, "agent-allow")
            allowed_ceiling = await session.get(AgentBoard, "ceiling-allow")
            assert get_permission_flag(custom_deny.flags, "card.move.in_progress_to_not_started") is False
            assert get_permission_flag(custom_allow.flags, "card.move.in_progress_to_not_started") is True
            assert get_permission_flag(agent.permission_flags, "card.move.in_progress_to_not_started") is False
            assert get_permission_flag(ceiling.permission_overrides, "card.move.in_progress_to_not_started") is False
            assert invalid.permission_overrides == {"card": {"move": "bad"}}
            assert get_permission_flag(allowed_agent.permission_flags, "card.move.in_progress_to_not_started") is True
            assert get_permission_flag(allowed_ceiling.permission_overrides, "card.move.in_progress_to_not_started") is True
            assert get_permission_flag(custom_deny.flags, "card.move.in_progress_to_started") is False
            assert (await session.scalar(select(func.count(PermissionIntroductionAudit.id)).where(
                PermissionIntroductionAudit.manifest_version == "CARD-STATUS-UNIFICATION/v1",
            ))) == 7
            review = (await session.execute(select(PermissionIntroductionAudit).where(
                PermissionIntroductionAudit.subject_id == "ceiling-invalid",
            ))).scalar_one()
            assert review.owner_review_required is True
            assert review.mutation_count == 0
    finally:
        await engine.dispose()
