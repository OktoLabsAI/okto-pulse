"""Community Local First conformance for the pure Core authentication port."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from okto_pulse.community.auth import LOCAL_USER, LocalAuthProvider
from okto_pulse.community.adapters.mcp_auth import principal_from_auth_session
from okto_pulse.core.ports import AgentAuthSession
from okto_pulse.core.ports.authentication import AuthenticationPort, Principal


def test_local_auth_adapter_satisfies_pure_core_contract() -> None:
    provider = LocalAuthProvider()
    principal = asyncio.run(provider.authenticate(None))

    assert isinstance(provider, AuthenticationPort)
    assert isinstance(principal, Principal)
    assert principal.subject == "local-user"
    assert principal.realm_id is None
    assert principal.legacy_user() == LOCAL_USER


def test_local_auth_adapter_does_not_import_fastapi() -> None:
    source_path = Path(__file__).resolve().parents[1] / "src/okto_pulse/community/auth.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "fastapi" not in imported_roots
    assert "starlette" not in imported_roots


def test_mcp_session_projects_to_the_same_principal_dto() -> None:
    principal = principal_from_auth_session(
        AgentAuthSession(agent_id="agent-01", agent_name="Automation", is_active=True)
    )

    assert principal == Principal(
        subject="agent-01",
        claims={"agent_name": "Automation", "auth_channel": "mcp"},
    )
    assert principal_from_auth_session(None) is None
    assert (
        principal_from_auth_session(
            AgentAuthSession(agent_id="agent-01", agent_name="Automation", is_active=False)
        )
        is None
    )
