"""R05-E IMP2 — Community functional-preservation smoke (in-process pytest).

Card R05-E IMP2 (spec d9d30831, ts_8de4c5f6 / TS06). Runs the offline, no-network
preservation smoke (``scripts/r05e_community_preservation_smoke.py``) IN-PROCESS
and asserts the six preserved runtime surfaces with ``asyncpg`` removed from the
core default. The standalone script is the re-executable evidence twin; this test
is the gate the validator/codex re-runs with ``uv run pytest``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import r05e_community_preservation_smoke as smoke  # noqa: E402


def test_community_functional_preservation_offline() -> None:
    evidence = smoke.run_preservation_smoke()

    # offline determinism: the stub embedding ran (no model download).
    assert evidence["embedding_mode"] == "stub"
    assert evidence["embedding_is_stub"] is True

    # composition wired the Community-owned providers (no missing-provider error).
    providers = evidence["composition_providers"]
    assert providers["event_bus"] == "CommunityOutboxEventBus"
    assert providers["audit_repo"] == "CommunityAuditRepository"
    assert providers["config"] == "CommunityKGConfig"

    # seed produced the board against an offline SQLite DB.
    assert evidence["seed_returned"] is True

    # serve surface preserved (route inventory intact, /health + /api/v1 present).
    assert evidence["has_health_route"] is True
    assert evidence["api_v1_route_count"] > 0
    assert evidence["route_count"] >= smoke._MIN_ROUTES

    # MCP tool inventory preserved.
    assert evidence["mcp_tool_count"] >= smoke._MIN_MCP_TOOLS
    assert evidence["has_create_ideation_tool"] is True

    # CLI sub-commands preserved.
    assert {"init", "serve", "status", "reset"} <= set(evidence["cli_commands_present"])

    assert evidence["ok"] is True
