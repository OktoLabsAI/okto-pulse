"""Community integration for SK-A/v1 permission inheritance and ceilings."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.core.domain.permissions import (
    SKA_PERMISSION_INTRODUCTION_V1,
    _get_nested,
)
from okto_pulse.community.adapters.permission_preset_reconciliation import (
    reconcile_community_permission_presets,
)
from okto_pulse.community.adapters.relational_application import (
    CommunityAgentAuthenticationGateway,
    CommunityPermissionPresetGateway,
)
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent,
    AgentBoard,
    Base,
    Board,
    PermissionPreset,
)
from okto_pulse.community.api.presets import PresetCreate, PresetUpdate


async def _factory():
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


def test_ska_tests_import_core_from_the_active_sibling_checkout() -> None:
    import okto_pulse.core.domain.permissions as permissions_module
    from pathlib import Path

    expected = (
        Path(__file__).resolve().parents[2]
        / "okto-pulse-core"
        / "src"
        / "okto_pulse"
        / "core"
    ).resolve()
    origin = Path(permissions_module.__file__).resolve()
    assert origin.is_relative_to(expected), (
        f"SK-A tests loaded Core from {origin}, expected sibling {expected}"
    )


def test_custom_lineage_inherits_base_preserves_false_and_honors_ceiling() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            await reconcile_community_permission_presets(session_factory=factory)
            async with factory() as session:
                spec = (
                    await session.execute(
                        select(PermissionPreset).where(
                            PermissionPreset.name == "Spec",
                            PermissionPreset.is_builtin.is_(True),
                        )
                    )
                ).scalar_one()
                custom = PermissionPreset(
                    id="custom-spec",
                    owner_id="owner-1",
                    name="Custom Spec",
                    description="inherits Spec",
                    is_builtin=False,
                    base_preset_id=spec.id,
                    flags={"ideation": {"quality": {"read": False}}},
                )
                board = Board(id="board-1", name="Board", owner_id="owner-1")
                agent = Agent(
                    id="agent-1",
                    name="Agent",
                    api_key="hidden-agent-1",
                    api_key_hash="hash-agent-1",
                    created_by="owner-1",
                    permission_flags={},
                    preset_id=custom.id,
                )
                grant = AgentBoard(
                    id="grant-1",
                    agent_id=agent.id,
                    board_id=board.id,
                    granted_by="owner-1",
                    permission_overrides=None,
                )
                session.add_all([custom, board, agent, grant])
                await session.commit()

            async with factory() as session:
                gateway = CommunityPermissionPresetGateway(session)
                inherited = await gateway.get_effective_permissions(
                    user_id="owner-1",
                    board_id="board-1",
                )
                grant = await session.get(AgentBoard, "grant-1")
                assert grant is not None
                grant.permission_overrides = {
                    "ideation": {"quality": {"assess": True}}
                }
                await session.commit()
                ceiling = await gateway.get_effective_permissions(
                    user_id="owner-1",
                    board_id="board-1",
                )
                persistence = CommunitySqlAlchemyApplicationPersistence()
                application_permissions = await persistence.resolve_user_permissions(
                    session,
                    user_id="owner-1",
                    board_id="board-1",
                )
                return inherited, ceiling, application_permissions
        finally:
            await engine.dispose()

    inherited, ceiling, application_permissions = asyncio.run(drive())
    assert inherited.owner_review_required is False
    assert _get_nested(inherited.flags, "ideation.quality.read") is False
    assert _get_nested(inherited.flags, "ideation.quality.assess") is True
    assert _get_nested(inherited.flags, "spec.quality.assess") is False

    assert _get_nested(ceiling.flags, "ideation.quality.assess") is True
    assert _get_nested(ceiling.flags, "ideation.quality.read") is False
    assert _get_nested(ceiling.flags, "refinement.quality.assess") is False
    assert application_permissions.flags == ceiling.flags


def test_clone_stores_lineage_delta_and_inherits_future_base_leaves() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            await reconcile_community_permission_presets(session_factory=factory)
            async with factory() as session:
                source = (
                    await session.execute(
                        select(PermissionPreset).where(
                            PermissionPreset.name == "Spec",
                            PermissionPreset.is_builtin.is_(True),
                        )
                    )
                ).scalar_one()
                gateway = CommunityPermissionPresetGateway(session)
                cloned = await gateway.clone_preset(
                    source_preset_id=source.id,
                    user_id="owner-1",
                    name="Spec clone",
                    description="",
                    flags={},
                )
                assert cloned is not None
                await session.commit()
                stored = await session.get(PermissionPreset, cloned.id)
                assert stored is not None
                stored_flags = stored.flags
                base_id = stored.base_preset_id
                assigned = Agent(
                    id="clone-assigned-agent",
                    name="Clone assigned",
                    api_key="clone-assigned-key",
                    api_key_hash="clone-assigned-hash",
                    created_by="clone-assigned-owner",
                    preset_id=cloned.id,
                    permission_flags={},
                )
                session.add(assigned)
                await session.commit()
                assigned_effective = await gateway.get_effective_permissions(
                    user_id="clone-assigned-owner",
                    board_id="no-ceiling",
                )

                source.flags = {
                    **source.flags,
                    "spec": {
                        **source.flags["spec"],
                        "quality": {"read": False, "assess": False},
                    },
                }
                await session.commit()
                refreshed = next(
                    preset
                    for preset in await gateway.list_presets(user_id="owner-1")
                    if preset.id == cloned.id
                )
                return (
                    stored_flags,
                    base_id,
                    source.id,
                    assigned_effective,
                    refreshed,
                )
        finally:
            await engine.dispose()

    stored_flags, base_id, source_id, assigned_effective, refreshed = asyncio.run(
        drive()
    )
    assert stored_flags == {}
    assert base_id == source_id
    assert _get_nested(assigned_effective.flags, "ideation.quality.assess") is True
    assert _get_nested(assigned_effective.flags, "spec.quality.assess") is False
    assert _get_nested(refreshed.flags, "spec.quality.read") is False
    assert refreshed.owner_review_required is False


def test_cycle_dangling_and_unknown_direct_preset_fail_closed() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            async with factory() as session:
                cycle_a = PermissionPreset(
                    id="cycle-a",
                    owner_id="owner-1",
                    name="Cycle A",
                    is_builtin=False,
                    base_preset_id="cycle-b",
                    flags={"board": {"read": True}},
                )
                cycle_b = PermissionPreset(
                    id="cycle-b",
                    owner_id="owner-1",
                    name="Cycle B",
                    is_builtin=False,
                    base_preset_id="cycle-a",
                    flags={},
                )
                dangling = PermissionPreset(
                    id="dangling",
                    owner_id="owner-1",
                    name="Dangling",
                    is_builtin=False,
                    base_preset_id="gone",
                    flags={"spec": {"quality": {"read": True}}},
                )
                session.add_all([cycle_a, cycle_b, dangling])
                await session.commit()
                gateway = CommunityPermissionPresetGateway(session)
                views = {
                    preset.id: preset
                    for preset in await gateway.list_presets(user_id="owner-1")
                }

                board = Board(id="board-1", name="Board", owner_id="unknown-owner")
                agent = Agent(
                    id="agent-unknown",
                    name="Unknown",
                    api_key="hidden-unknown",
                    api_key_hash="hash-unknown",
                    created_by="unknown-owner",
                    permission_flags={},
                    preset_id="unknown-direct",
                )
                grant = AgentBoard(
                    id="grant-unknown",
                    agent_id=agent.id,
                    board_id=board.id,
                    granted_by="unknown-owner",
                )
                session.add_all([board, agent, grant])
                await session.commit()
                unknown = await gateway.get_effective_permissions(
                    user_id="unknown-owner",
                    board_id="board-1",
                )
                auth_context = await CommunityAgentAuthenticationGateway(
                    session
                ).resolve_agent_permission_context(
                    "agent-unknown",
                    board_id="board-1",
                )
                persistence_context = (
                    await CommunitySqlAlchemyApplicationPersistence().resolve_user_permissions(
                        session,
                        user_id="unknown-owner",
                        board_id="board-1",
                    )
                )
                return views, unknown, auth_context, persistence_context
        finally:
            await engine.dispose()

    views, unknown, auth_context, persistence_context = asyncio.run(drive())
    assert views["cycle-a"].owner_review_required is True
    assert views["cycle-a"].review_reason == "preset_lineage_cycle"
    assert views["dangling"].owner_review_required is True
    assert views["dangling"].review_reason == "dangling_base_preset"
    assert unknown.owner_review_required is True
    assert unknown.review_reason == "unknown_preset"
    assert auth_context is not None
    assert auth_context.permissions.owner_review_required is True
    assert persistence_context.owner_review_required is True

    for value in (
        *views.values(),
        unknown,
        auth_context.permissions,
        persistence_context,
    ):
        assert all(
            _get_nested(value.flags, leaf) is False
            for leaf in SKA_PERMISSION_INTRODUCTION_V1.leaves
        )


def test_partial_root_is_compatible_but_non_object_flags_require_review() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            async with factory() as session:
                partial = PermissionPreset(
                    id="partial-root",
                    owner_id="owner-1",
                    name="Partial root",
                    is_builtin=False,
                    base_preset_id=None,
                    flags={"board": {"read": False}},
                )
                malformed = PermissionPreset(
                    id="malformed-root",
                    owner_id="owner-1",
                    name="Malformed root",
                    is_builtin=False,
                    base_preset_id=None,
                    flags=["not", "an", "object"],  # type: ignore[arg-type]
                )
                agent = Agent(
                    id="malformed-agent",
                    name="Malformed agent",
                    api_key="malformed-key",
                    api_key_hash="malformed-hash",
                    created_by="malformed-owner",
                    preset_id=malformed.id,
                    permission_flags={},
                )
                session.add_all([partial, malformed, agent])
                await session.commit()
                gateway = CommunityPermissionPresetGateway(session)
                views = {
                    preset.id: preset
                    for preset in await gateway.list_presets(user_id="owner-1")
                }
                effective = await gateway.get_effective_permissions(
                    user_id="malformed-owner",
                    board_id="no-ceiling",
                )
                persistence = (
                    await CommunitySqlAlchemyApplicationPersistence().resolve_user_permissions(
                        session,
                        user_id="malformed-owner",
                        board_id="no-ceiling",
                    )
                )
                return views, effective, persistence
        finally:
            await engine.dispose()

    views, effective, persistence = asyncio.run(drive())
    partial = views["partial-root"]
    assert partial.owner_review_required is False
    assert _get_nested(partial.flags, "board.read") is False
    assert _get_nested(partial.flags, "profile.update") is True
    assert _get_nested(partial.flags, "spec.quality.read") is False

    malformed = views["malformed-root"]
    assert malformed.owner_review_required is True
    assert malformed.review_reason == "invalid_preset_flags"
    for value in (malformed, effective, persistence):
        assert value.owner_review_required is True
        assert value.review_reason == "invalid_preset_flags"
        assert _get_nested(value.flags, "board.read") is False
        assert all(
            _get_nested(value.flags, leaf) is False
            for leaf in SKA_PERMISSION_INTRODUCTION_V1.leaves
        )


def test_unrecognized_direct_agent_requires_owner_review_across_adapters() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            async with factory() as session:
                board = Board(
                    id="direct-review-board",
                    name="Direct review",
                    owner_id="direct-review-owner",
                )
                agent = Agent(
                    id="direct-review-agent",
                    name="Direct review agent",
                    api_key="direct-review-key",
                    api_key_hash="direct-review-hash",
                    created_by="direct-review-owner",
                    permission_flags={"board": {"read": True}},
                    preset_id=None,
                )
                grant = AgentBoard(
                    id="direct-review-grant",
                    agent_id=agent.id,
                    board_id=board.id,
                    granted_by="direct-review-owner",
                )
                session.add_all([board, agent, grant])
                await session.commit()

                gateway = CommunityPermissionPresetGateway(session)
                effective = await gateway.get_effective_permissions(
                    user_id=agent.created_by,
                    board_id=board.id,
                )
                authentication = await CommunityAgentAuthenticationGateway(
                    session
                ).resolve_agent_permission_context(
                    agent.id,
                    board_id=board.id,
                )
                application = (
                    await CommunitySqlAlchemyApplicationPersistence().resolve_user_permissions(
                        session,
                        user_id=agent.created_by,
                        board_id=board.id,
                    )
                )
                return effective, authentication, application
        finally:
            await engine.dispose()

    effective, authentication, application = asyncio.run(drive())
    assert authentication is not None
    for value in (effective, authentication.permissions, application):
        assert value.owner_review_required is True
        assert value.review_reason == "unrecognized_direct_permissions"
        assert _get_nested(value.flags, "board.read") is False
        assert all(
            _get_nested(value.flags, leaf) is False
            for leaf in SKA_PERMISSION_INTRODUCTION_V1.leaves
        )


def test_delete_preset_refuses_agent_assignment_and_child_lineage() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            async with factory() as session:
                gateway = CommunityPermissionPresetGateway(session)
                assigned = PermissionPreset(
                    id="assigned-preset",
                    owner_id="owner-1",
                    name="Assigned",
                    is_builtin=False,
                    flags={},
                )
                base = PermissionPreset(
                    id="base-preset",
                    owner_id="owner-1",
                    name="Base",
                    is_builtin=False,
                    flags={},
                )
                child = PermissionPreset(
                    id="child-preset",
                    owner_id="owner-1",
                    name="Child",
                    is_builtin=False,
                    base_preset_id=base.id,
                    flags={},
                )
                agent = Agent(
                    id="assigned-agent",
                    name="Assigned",
                    api_key="assigned-key",
                    api_key_hash="assigned-hash",
                    created_by="owner-1",
                    permission_flags={},
                    preset_id=assigned.id,
                )
                session.add_all([assigned, base, child, agent])
                await session.commit()

                with pytest.raises(PermissionError, match="cannot be deleted"):
                    await gateway.delete_preset(
                        preset_id=assigned.id,
                        user_id="owner-1",
                    )
                with pytest.raises(PermissionError, match="cannot be deleted"):
                    await gateway.delete_preset(
                        preset_id=base.id,
                        user_id="owner-1",
                    )
                assert await session.get(PermissionPreset, assigned.id) is not None
                assert await session.get(PermissionPreset, base.id) is not None
        finally:
            await engine.dispose()

    asyncio.run(drive())


@pytest.mark.parametrize(
    "malformed",
    (
        ["not", "an", "object"],
        {"board": {"read": "true"}},
        {"vendor_extension": {"grant": 1}},
    ),
    ids=("top-level-list", "canonical-leaf-string", "extension-int"),
)
def test_malformed_board_ceiling_fails_closed_across_community_adapters(
    malformed,
) -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            async with factory() as session:
                board = Board(
                    id="malformed-ceiling-board",
                    name="Malformed ceiling",
                    owner_id="malformed-ceiling-owner",
                )
                agent = Agent(
                    id="malformed-ceiling-agent",
                    name="Malformed ceiling agent",
                    api_key="malformed-ceiling-key",
                    api_key_hash="malformed-ceiling-hash",
                    created_by="malformed-ceiling-owner",
                    permission_flags=None,
                )
                grant = AgentBoard(
                    id="malformed-ceiling-grant",
                    agent_id=agent.id,
                    board_id=board.id,
                    granted_by="malformed-ceiling-owner",
                    permission_overrides=malformed,
                )
                session.add_all([board, agent, grant])
                await session.commit()

                effective = await CommunityPermissionPresetGateway(
                    session
                ).get_effective_permissions(
                    user_id="malformed-ceiling-owner",
                    board_id=board.id,
                )
                auth_context = await CommunityAgentAuthenticationGateway(
                    session
                ).resolve_agent_permission_context(
                    agent.id,
                    board_id=board.id,
                )
                persistence = (
                    await CommunitySqlAlchemyApplicationPersistence().resolve_user_permissions(
                        session,
                        user_id="malformed-ceiling-owner",
                        board_id=board.id,
                    )
                )
                return effective, auth_context, persistence
        finally:
            await engine.dispose()

    effective, auth_context, persistence = asyncio.run(drive())
    assert auth_context is not None
    for value in (effective, auth_context.permissions, persistence):
        assert value.owner_review_required is True
        assert value.review_reason == "invalid_board_overrides"
        assert _get_nested(value.flags, "board.read") is False
        assert all(
            _get_nested(value.flags, leaf) is False
            for leaf in SKA_PERMISSION_INTRODUCTION_V1.leaves
        )
    assert auth_context.permissions.has("vendor_extension.grant") is False


@pytest.mark.parametrize(
    "model",
    (
        lambda: PresetCreate(
            name="invalid",
            flags={"board": {"read": "true"}},
        ),
        lambda: PresetUpdate(flags={"board": {"read": 1}}),
        lambda: PresetUpdate(flags={"extension": {"grant": None}}),
    ),
    ids=("create-string", "update-int", "extension-null"),
)
def test_preset_rest_schemas_require_exact_boolean_leaves(model) -> None:
    with pytest.raises(ValidationError, match="must be boolean"):
        model()
