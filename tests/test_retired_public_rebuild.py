"""F4: retired rebuild entry points are absent before any data access."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client
import pytest

from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.router import api_router
from okto_pulse.core.domain.permissions import ALL_FLAGS, get_builtin_presets
from okto_pulse.core.domain.mcp_permission_registry import MCP_TOOL_PERMISSION_POLICIES
from okto_pulse.core.mcp import server
from okto_pulse.core.ports.mcp_resources import (
    StaticMcpResourceCatalog,
    freeze_mcp_resource_catalog,
)


RETIRED = tuple(f"okto_pulse_kg_rebuild_{action}" for action in ("preflight", "confirm", "run"))


def test_removed_modules_and_console_entrypoint_are_not_distributed():
    for name in ("kg_recovery_only", "api.kg_rebuild"):
        assert importlib.util.find_spec(f"okto_pulse.community.{name}") is None
    assert "okto-pulse-kg-recovery-only" not in {
        entry.name for entry in importlib.metadata.distribution("okto-pulse").entry_points
    }
    scripts = Path(sysconfig.get_path("scripts"))
    assert not (scripts / "okto-pulse-kg-recovery-only.exe").exists()
    assert not (scripts / "okto-pulse-kg-recovery-only").exists()


@pytest.mark.parametrize("action", ["preflight", "confirm", "run"])
@pytest.mark.parametrize("board", ["missing", "foreign", "owned"])
def test_registered_rest_app_returns_uniform_absence_without_authorization_or_data_access(action, board):
    app = FastAPI()
    app.include_router(api_router)

    async def forbidden_uow():
        pytest.fail("retired route resolved a unit of work")
        yield  # pragma: no cover

    app.dependency_overrides[get_unit_of_work] = forbidden_uow
    client = TestClient(app)
    path = f"/api/v1/kg/rebuild/{action}"
    assert path not in client.get("/openapi.json").json()["paths"]
    response = client.post(path, params={"board_id": board}, json={
        "board_id": board, "operation": "rebuild", "manifest_ref": "historical-manifest",
        "confirmation_id": "historical-confirmation", "preflight_hash": "a" * 64,
        "reason": "must never reach a recovery executor",
    })
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.asyncio
async def test_materialized_mcp_transport_rejects_removed_tools_before_handlers(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("removed tool resolved authority or storage")

    monkeypatch.setattr(server, "_get_agent_ctx", forbidden)
    monkeypatch.setattr(server, "get_unit_of_work_factory_for_mcp", forbidden)
    frozen = freeze_mcp_resource_catalog(StaticMcpResourceCatalog("absence", (), precedence=1))
    host = CommunityMcpHostProvider().materialize_catalog(
        server.mcp, resource_catalog=frozen, projection_identity=frozen.identity,
    )
    async with Client(host) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert names.isdisjoint(RETIRED)
        for name in RETIRED:
            assert not hasattr(server, name)
            result = await client.call_tool(name, {"board_id": "owned"}, raise_on_error=False)
            assert result.is_error
            assert "Unknown tool" in result.content[0].text


def test_removed_permissions_are_absent_from_registry_and_presets():
    assert not any(flag.startswith("kg.operations.rebuild.") for flag in ALL_FLAGS)
    assert {policy.tool_name for policy in MCP_TOOL_PERMISSION_POLICIES}.isdisjoint(RETIRED)
    for preset in get_builtin_presets():
        assert "rebuild" not in preset["flags"].get("kg", {}).get("operations", {})


@pytest.mark.parametrize("arguments", [
    ["--help"], ["--inspect-install"], ["--execute"],
    ["--rehearsal-copy-of", "missing-copy", "--rehearsal-receipt-out", "receipt.json"],
])
def test_installed_module_invocation_cannot_open_or_mutate_existing_history(tmp_path, arguments):
    for name, data in {
        "data/pulse.db": b"opaque relational history",
        "boards/owned/graph.lbug": b"opaque retired graph history",
        "boards/owned/grafx/generation/grafx.meta": b"opaque Grafx identity",
        "receipt.json": json.dumps({"historical": True}).encode(),
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def snapshot():
        return {str(p.relative_to(tmp_path)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in tmp_path.rglob("*") if p.is_file()}

    before = snapshot()
    env = os.environ.copy()
    for key in ("PYTHONPATH", "DATABASE_URL"):
        env.pop(key, None)
    env.update(DATA_DIR=str(tmp_path), KG_BASE_DIR=str(tmp_path / "boards"))
    result = subprocess.run([
        sys.executable, "-m", "okto_pulse.community.kg_recovery_only",
        "--data-home", str(tmp_path), "--board-id", "owned", *arguments,
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "No module named okto_pulse.community.kg_recovery_only" in result.stderr
    assert not result.stdout
    assert snapshot() == before
