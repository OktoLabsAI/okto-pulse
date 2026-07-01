"""R08-A (COMMUNITY target) — CommunityMcpAuthenticator adapter.

Scenarios covered here (community-target):

  ts_b22ce5b0 — conformance: isinstance(CommunityMcpAuthenticator,
                McpAuthenticator); the resolved session is the canonical
                core.ports AgentAuthSession (no parallel DTOs); R06 permits the
                Community-owned MCPAuthContext bridge in this adapter; + no
                SaaS-redesign symbols in the adapter (ts_178da21e parallel).
  ts_75846b3a — a valid key authenticates: SHA-256 -> api_key_hash lookup,
                last_used_at touched, identity mapped to AgentAuthSession.
  ts_5f381019 — fail-closed: invalid key / inactive agent / absent credential ->
                None, with no raw-secret leak.

Async DB work is driven via ``asyncio.run`` in sync tests (single loop each).
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

# Register every ORM model on Base.metadata (production-faithful) so create_all
# builds the agents table; creates no engine.
import okto_pulse.core.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
import okto_pulse.community.adapters.mcp_auth as _adapter_mod
from okto_pulse.community.adapters.mcp_auth import (
    CommunityMcpAuthenticator,
    make_community_mcp_authenticator,
)
from okto_pulse.core.ports import AgentAuthSession, McpAuthenticator, McpCredential

ADAPTER_PY = Path(_adapter_mod.__file__)


@pytest.fixture
def _isolate_engine():
    saved_engine = _db_mod._engine
    saved_factory = _db_mod._session_factory
    try:
        yield
    finally:
        _db_mod._engine = saved_engine
        _db_mod._session_factory = saved_factory


async def _seed_agent(api_key: str, *, is_active: bool = True) -> str:
    from okto_pulse.core.infra.database import Base
    from okto_pulse.core.models.db import Agent
    from okto_pulse.core.services.main import AgentService

    async with _db_mod.get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with _db_mod.get_session_factory()() as session:
        agent = Agent(
            name="Test Agent",
            api_key=api_key,
            api_key_hash=AgentService.hash_api_key(api_key),
            is_active=is_active,
            created_by="user-1",
        )
        session.add(agent)
        await session.commit()
        return agent.id


async def _reload_last_used(agent_id: str):
    from okto_pulse.core.models.db import Agent

    async with _db_mod.get_session_factory()() as session:
        agent = await session.get(Agent, agent_id)
        return agent.last_used_at


# ===========================================================================
# ts_b22ce5b0 — conformance + canonical DTOs + no parallel DTOs + no SaaS.
# ===========================================================================
def test_ts_b22ce5b0_isinstance_of_port_protocol():
    auth = make_community_mcp_authenticator(session_factory=lambda: None)
    assert isinstance(auth, McpAuthenticator)
    assert isinstance(CommunityMcpAuthenticator(session_factory=lambda: None), McpAuthenticator)


def test_ts_b22ce5b0_adapter_defines_no_parallel_dtos():
    tree = ast.parse(ADAPTER_PY.read_text(encoding="utf-8"))
    class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert class_names == {"CommunityMcpAuthenticator", "MCPAuthContext"}
    for forbidden in {"McpCredential", "AgentAuthSession", "AuthSession"}:
        assert forbidden not in class_names

    # No SaaS-redesign symbols (ts_178da21e parallel for the adapter).
    defined = {
        n.name.lower()
        for n in ast.walk(tree)
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for token in ("credentialstore", "jwt", "realm", "oauth", "password", "bcrypt"):
        assert not any(token in name for name in defined)


def test_ts_b22ce5b0_resolved_session_is_canonical_dto(tmp_path, _isolate_engine):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'conf.db'}")
        agent_id = await _seed_agent("conf-key-123")
        auth = make_community_mcp_authenticator(
            session_factory=_db_mod.get_session_factory()
        )
        session = await auth.authenticate(
            McpCredential(source="query_param", value="conf-key-123")
        )
        await _db_mod.get_engine().dispose()
        return agent_id, session

    agent_id, session = asyncio.run(drive())
    assert type(session) is AgentAuthSession  # the canonical core.ports DTO
    assert session.agent_id == agent_id


# ===========================================================================
# ts_75846b3a — valid key authenticates (hash + last_used_at).
# ===========================================================================
def test_ts_75846b3a_valid_key_authenticates_and_touches_last_used(
    tmp_path, _isolate_engine
):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'valid.db'}")
        agent_id = await _seed_agent("valid-key-abc")
        before = await _reload_last_used(agent_id)  # None at creation
        auth = make_community_mcp_authenticator(
            session_factory=_db_mod.get_session_factory()
        )
        session = await auth.authenticate(
            McpCredential(source="x_api_key_header", value="valid-key-abc")
        )
        after = await _reload_last_used(agent_id)
        await _db_mod.get_engine().dispose()
        return agent_id, session, before, after

    agent_id, session, before, after = asyncio.run(drive())
    assert session is not None
    assert session.agent_id == agent_id
    assert session.agent_name == "Test Agent"
    assert session.is_active is True
    assert before is None and after is not None  # last_used_at was touched
    # secret-free session: no raw key anywhere in the session.
    assert "valid-key-abc" not in repr(session)
    assert "valid-key-abc" not in str(dict(session.metadata))


# ===========================================================================
# ts_5f381019 — fail-closed (invalid / inactive / absent), no secret leak.
# ===========================================================================
def test_ts_5f381019_invalid_key_returns_none(tmp_path, _isolate_engine):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'inv.db'}")
        await _seed_agent("the-real-key")
        auth = make_community_mcp_authenticator(
            session_factory=_db_mod.get_session_factory()
        )
        result = await auth.authenticate(
            McpCredential(source="query_param", value="WRONG-KEY")
        )
        await _db_mod.get_engine().dispose()
        return result

    assert asyncio.run(drive()) is None


def test_ts_5f381019_inactive_agent_returns_none(tmp_path, _isolate_engine):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'inactive.db'}")
        await _seed_agent("inactive-key", is_active=False)
        auth = make_community_mcp_authenticator(
            session_factory=_db_mod.get_session_factory()
        )
        result = await auth.authenticate(
            McpCredential(source="authorization_bearer", value="inactive-key")
        )
        await _db_mod.get_engine().dispose()
        return result

    assert asyncio.run(drive()) is None


def test_ts_5f381019_absent_or_empty_credential_returns_none():
    auth = make_community_mcp_authenticator(session_factory=lambda: None)
    # None credential and empty value are fail-closed WITHOUT touching the DB
    # (session_factory would crash if called) -> proves the short-circuit.
    assert asyncio.run(auth.authenticate(None)) is None
    assert asyncio.run(
        auth.authenticate(McpCredential(source="query_param", value=""))
    ) is None


def test_ts_5f381019_credential_repr_redacts_secret():
    cred = McpCredential(source="query_param", value="top-secret-value")
    assert "top-secret-value" not in repr(cred)
    assert "redacted" in repr(cred)
