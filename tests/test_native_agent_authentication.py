"""Global MCP authentication resolves the same native policy as Board access."""
import hashlib

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, Board, PermissionPreset


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["native", "unknown_preset", "cycle", "incomplete_direct", "local"])
async def test_global_auth_never_discards_native_restrictions_or_mutates_facts(tmp_path, case):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await initialize_current_schema(engine, current_schema_contract())
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda conn: {column["name"] for column in inspect(conn).get_columns("agents")}
            )
            assert "permissions" not in columns

        flags = registered_permission_flags()
        flags["profile"]["update"] = True
        preset_id = {"unknown_preset": "missing", "incomplete_direct": None, "local": None}.get(case, "child")
        direct = {"profile": {"update": False}} if case == "incomplete_direct" else (None if case == "local" else {})
        async with factory() as session:
            session.add_all([
                PermissionPreset(id="base", name="Base", owner_id="owner", flags=flags,
                                 base_preset_id="child" if case == "cycle" else None),
                PermissionPreset(id="child", name="Child", owner_id="owner",
                                 base_preset_id="base", flags={"board": {"read": False}}),
                Board(id="board", name="Board", realm_id="local", owner_id="owner"),
                Agent(id="agent", name="Agent", created_by="owner", api_key="marker",
                      api_key_hash=hashlib.sha256(b"native-key").hexdigest(),
                      preset_id=preset_id, permission_flags=direct),
                AgentBoard(id="grant", agent_id="agent", board_id="board", granted_by="owner",
                           permission_overrides={"profile": {"update": False}}),
            ])
            await session.commit()

        async def snapshot():
            async with factory() as session:
                return (
                    (await session.execute(select(Agent.__table__))).all(),
                    (await session.execute(select(PermissionPreset.__table__))).all(),
                    (await session.execute(select(AgentBoard.__table__))).all(),
                )

        before = await snapshot()
        async with factory() as session:
            gateway = CommunityAgentAuthenticationGateway(session)
            auth = await gateway.authenticate_agent_by_api_key("native-key", credential_source="test")
            global_context = await gateway.resolve_agent_permission_context("agent")
            scoped = await gateway.resolve_agent_permission_context("agent", board_id="board")
            assert auth is not None and scoped is not None
            assert auth.permissions.flags == global_context.permissions.flags
            assert auth.permissions.owner_review_required == (case in {"unknown_preset", "cycle", "incomplete_direct"})
            assert auth.permissions.has("profile.update") == (case in {"native", "local"})
            assert auth.permissions.has("board.read") == (case == "local")
            assert not scoped.permissions.has("profile.update")
            assert await gateway.resolve_agent_permission_context("agent", board_id="foreign") is None
            assert "native-key" not in repr(auth)
            await session.commit()
        assert await snapshot() == before

        if case == "native":
            # No cached global grant survives a native preset edit.
            async with factory() as session:
                row = await session.get(PermissionPreset, "base")
                changed = registered_permission_flags()
                changed["profile"]["update"] = False
                row.flags = changed
                await session.commit()
            async with factory() as session:
                auth = await CommunityAgentAuthenticationGateway(session).authenticate_agent_by_api_key(
                    "native-key", credential_source="test",
                )
                assert not auth.permissions.has("profile.update")
    finally:
        await engine.dispose()
