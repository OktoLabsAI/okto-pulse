"""R06 — Community-owned concrete MCP AuthContext bridge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from okto_pulse.community.adapters.mcp_auth import (
    MCPAuthContext,
    auth_context_from_session,
    create_mcp_auth_factory,
)
from okto_pulse.core.kg.interfaces.auth_context import AuthContext
from okto_pulse.core.ports.mcp_auth import AgentAuthSession


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def test_community_mcp_auth_context_resolves_agent_and_acl(monkeypatch) -> None:
    calls: list[str] = []
    db = _FakeDb()

    class _FakeAgentService:
        def __init__(self, session):
            self.session = session

        async def list_boards_for_agent(self, agent_id: str):
            calls.append(agent_id)
            return [SimpleNamespace(id="B1"), SimpleNamespace(id="B2")]

    import okto_pulse.core.services.main as services_main

    monkeypatch.setattr(services_main, "AgentService", _FakeAgentService)

    async def get_agent():
        return SimpleNamespace(id="A1")

    ctx = MCPAuthContext(get_agent=get_agent, get_db=lambda: db)
    assert isinstance(ctx, AuthContext)

    async def drive():
        first = await ctx.get_accessible_boards()
        second = await ctx.get_accessible_boards()
        return await ctx.get_agent_id(), first, second, ctx.has_admin_role()

    agent_id, first, second, admin = asyncio.run(drive())
    assert agent_id == "A1"
    assert first == second == ["B1", "B2"]
    assert admin is False
    assert calls == ["A1"]
    assert db.commits == 1


def test_auth_context_from_session_fails_closed_for_absent_or_inactive() -> None:
    active = auth_context_from_session(
        AgentAuthSession(agent_id="A1", agent_name="Agent One", is_active=True),
        lambda: _FakeDb(),
    )
    inactive = auth_context_from_session(
        AgentAuthSession(agent_id="A1", agent_name="Agent One", is_active=False),
        lambda: _FakeDb(),
    )
    absent = auth_context_from_session(None, lambda: _FakeDb())

    assert isinstance(active, MCPAuthContext)
    assert asyncio.run(active.get_agent_id()) == "A1"
    assert asyncio.run(inactive.get_agent_id()) is None
    assert asyncio.run(absent.get_agent_id()) is None


def test_create_mcp_auth_factory_builds_community_context() -> None:
    factory = create_mcp_auth_factory(
        lambda: SimpleNamespace(id="A1"),
        lambda: _FakeDb(),
    )
    produced = factory()
    assert isinstance(produced, MCPAuthContext)
