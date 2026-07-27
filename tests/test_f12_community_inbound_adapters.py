from __future__ import annotations

import ast
from pathlib import Path

import pytest

from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWorkFactory,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from repo_layout import resolve_core_repo


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = resolve_core_repo(ROOT)


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_f12_community_uow_owns_local_realm_and_teardown() -> None:
    sessions: list[_Session] = []

    def session_factory() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    factory = CommunityUnitOfWorkFactory(session_factory)

    assert factory.resolve_realm_scope().realm_id == LOCAL_REALM_ID
    async with factory(realm_scope=factory.resolve_realm_scope()) as uow:
        assert uow.realm_scope.is_local is True
        assert not hasattr(uow, "session")
        assert uow.services is not None

    assert len(sessions) == 1
    assert sessions[0].closed is True


def test_f12_community_owns_http_mcp_and_server_runtime() -> None:
    main_source = (ROOT / "src/okto_pulse/community/main.py").read_text(
        encoding="utf-8"
    )
    host_source = (
        ROOT / "src/okto_pulse/community/adapters/mcp_host.py"
    ).read_text(encoding="utf-8")
    core_mcp_source = (
        CORE_ROOT / "src/okto_pulse/core/mcp/server.py"
    ).read_text(encoding="utf-8")

    assert "uvicorn.Config(" in main_source
    assert "uvicorn.Server(" in main_source
    assert "register_community_mcp_host()" in main_source
    assert "FastMCP(" in host_source
    assert ".http_app(" in host_source
    assert "get_db_for_current_mcp_request" not in main_source
    assert "get_db_for_mcp" not in ast.dump(ast.parse(core_mcp_source))


def test_f12_core_catalog_has_no_host_runtime_imports() -> None:
    core_mcp = ast.parse(
        (
            CORE_ROOT / "src/okto_pulse/core/mcp/server.py"
        ).read_text(encoding="utf-8")
    )
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(core_mcp)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(core_mcp)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    )

    assert {"fastmcp", "uvicorn", "starlette"}.isdisjoint(imported_roots)
