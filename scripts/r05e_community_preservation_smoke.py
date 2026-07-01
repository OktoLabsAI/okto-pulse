#!/usr/bin/env python3
"""R05-E IMP2 — Community functional-preservation smoke (offline, deterministic).

Card R05-E IMP2 (spec d9d30831, scenario ts_8de4c5f6 / TS06): prove the Community
edition stays FUNCTIONAL after ``asyncpg`` is removed from the core default. The
check runs entirely OFFLINE (no network / AWS / PyPI / HF download) and
deterministically against the working-tree (editable) install — which reflects
the full R05-E refactor (much of ``okto_pulse.core`` is still uncommitted).

It exercises six runtime surfaces with NO port binding and NO model download:

  1. imports     — community ``cli`` / ``seed`` / ``main`` / composition + the core
                   ``create_app`` and ``build_mcp_asgi_app`` import clean.
  2. composition — ``configure_community_kg_registry`` wires the Community-owned
                   providers (no missing-provider error) with the STUB embedding.
  3. seed        — ``seed_community_defaults`` runs against an in-memory/temp
                   SQLite DB and returns the seeded board (idempotent core path).
  4. serve       — ``create_community_app`` builds the FastAPI app and its route
                   inventory is preserved (``/health`` + the ``/api/v1`` surface).
  5. mcp         — the MCP tool inventory is preserved (the FastMCP registry still
                   exposes the full tool set, e.g. ``okto_pulse_create_ideation``).
  6. cli         — the CLI still advertises its sub-commands (init/serve/...).

Offline is forced via ``KG_EMBEDDING_MODE=stub`` (no sentence-transformers load)
plus ``HF_HUB_OFFLINE`` / ``TRANSFORMERS_OFFLINE``. ``run_preservation_smoke``
saves and restores the process-global settings / DB / KG-registry so it is safe
to call in-process from pytest as well as standalone.

Usage:
    python scripts/r05e_community_preservation_smoke.py        # prints evidence
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

_SUCCESS_SENTINEL = "PRESERVATION_OK"
_FAILURE_SENTINEL = "PRESERVATION_FAIL"

# Minimum preserved-surface thresholds. Deliberately conservative: they assert
# the surface is INTACT (not empty / not collapsed) without pinning an exact
# count that would churn on every unrelated feature add.
_MIN_ROUTES = 50
_MIN_MCP_TOOLS = 100


def _force_offline_env() -> Path:
    """Point the runtime at a throwaway data dir and forbid any network model load."""
    tmp = Path(tempfile.mkdtemp(prefix="r05e_imp2_community_"))
    os.environ["DATA_DIR"] = str(tmp)
    os.environ["KG_BASE_DIR"] = str(tmp / "boards")
    os.environ["KG_EMBEDDING_MODE"] = "stub"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    return tmp


def run_preservation_smoke() -> dict[str, object]:
    """Run the six offline preservation checks; return structured evidence.

    Raises ``AssertionError`` on a real regression (a collapsed surface). Restores
    every process-global it mutates so the caller's environment is untouched.
    """
    tmp = _force_offline_env()

    # --- 1. imports --------------------------------------------------------- #
    from okto_pulse.community import cli, seed  # noqa: F401
    from okto_pulse.community import main as community_main  # noqa: F401
    from okto_pulse.community.adapters import composition as comp
    from okto_pulse.core.app import create_app  # noqa: F401
    from okto_pulse.core.infra import config as _config
    from okto_pulse.core.infra import database as _db
    from okto_pulse.core.infra.config import CoreSettings
    from okto_pulse.core.kg.interfaces import registry as _reg
    from okto_pulse.core.mcp import server as _srv

    evidence: dict[str, object] = {"imports_ok": True}

    # Save process-global state for restoration.
    saved_settings = _config._settings_instance
    saved_engine = _db._engine
    saved_factory = _db._session_factory
    saved_reg = (_reg._registry, _reg._configured)

    try:
        settings = CoreSettings()
        evidence["embedding_mode"] = settings.kg_embedding_mode
        assert settings.kg_embedding_mode == "stub", "offline smoke must run the stub embedding"
        _config.configure_settings(settings)
        _reg.reset_registry_for_tests()

        async def _init_db() -> None:
            _db.create_database(f"sqlite+aiosqlite:///{tmp / 'r05e_imp2.db'}")
            await _db.init_db()

        asyncio.run(_init_db())

        # --- 2. composition ------------------------------------------------- #
        comp.configure_community_kg_registry(_db.get_session_factory(), include_graph=True)
        reg = _reg.get_kg_registry()
        provider_types = {
            "event_bus": type(reg.event_bus).__name__,
            "audit_repo": type(reg.audit_repo).__name__,
            "config": type(reg.config).__name__,
        }
        evidence["composition_providers"] = provider_types
        assert reg.event_bus is not None and reg.audit_repo is not None and reg.config is not None, (
            "Community data providers missing after composition"
        )
        assert provider_types["event_bus"] == "CommunityOutboxEventBus", provider_types
        assert provider_types["audit_repo"] == "CommunityAuditRepository", provider_types
        assert provider_types["config"] == "CommunityKGConfig", provider_types
        emb = getattr(reg, "embedding_provider", None)
        emb_meta = emb.embedding_metadata() if emb is not None else {}
        evidence["embedding_is_stub"] = bool(emb_meta.get("is_stub"))
        assert evidence["embedding_is_stub"] is True, "expected the deterministic stub embedding offline"

        # --- 3. seed -------------------------------------------------------- #
        async def _seed() -> object:
            async with _db.get_session_factory()() as session:
                return await seed.seed_community_defaults(session)

        seed_result = asyncio.run(_seed())
        evidence["seed_returned"] = seed_result is not None
        assert seed_result is not None, "seed_community_defaults returned None (board not seeded)"

        asyncio.run(_db.close_db())

        # --- 4. serve (route inventory, no port binding) -------------------- #
        app = community_main.create_community_app()
        routes = sorted({getattr(r, "path", None) for r in app.routes if getattr(r, "path", None)})
        evidence["routes"] = routes
        evidence["route_count"] = len(routes)
        evidence["has_health_route"] = "/health" in routes
        api_routes = [p for p in routes if p.startswith("/api/v1")]
        evidence["api_v1_routes"] = api_routes
        evidence["api_v1_route_count"] = len(api_routes)
        assert "/health" in routes, "the /health route disappeared"
        assert api_routes, "the /api/v1 surface disappeared"
        assert len(routes) >= _MIN_ROUTES, f"route inventory collapsed: {len(routes)} < {_MIN_ROUTES}"

        # --- 5. mcp (tool inventory) ---------------------------------------- #
        # build_mcp_asgi_app constructs the same MCP ASGI surface the dual-port
        # runner serves; the tool registry is the FastMCP instance behind it.
        mcp_app = _srv.build_mcp_asgi_app()
        evidence["mcp_asgi_app"] = type(mcp_app).__name__
        tools = asyncio.run(_srv.mcp.get_tools())
        tool_names = list(tools.keys()) if hasattr(tools, "keys") else [getattr(t, "name", t) for t in tools]
        tool_names = sorted(str(name) for name in tool_names)
        evidence["mcp_tools"] = tool_names
        evidence["mcp_tool_count"] = len(tool_names)
        evidence["has_create_ideation_tool"] = any("create_ideation" in str(n) for n in tool_names)
        assert len(tool_names) >= _MIN_MCP_TOOLS, (
            f"MCP tool inventory collapsed: {len(tool_names)} < {_MIN_MCP_TOOLS}"
        )
        assert evidence["has_create_ideation_tool"], "okto_pulse_create_ideation tool missing"

        # --- 6. cli --------------------------------------------------------- #
        cli_src = inspect.getsource(cli)
        cli_commands = [c for c in ("init", "serve", "status", "reset", "api-key") if f'"{c}"' in cli_src]
        evidence["cli_commands_present"] = cli_commands
        assert {"init", "serve", "status", "reset"} <= set(cli_commands), cli_commands

    finally:
        try:
            asyncio.run(_db.close_db())
        except Exception:
            pass
        _config._settings_instance = saved_settings
        _db._engine = saved_engine
        _db._session_factory = saved_factory
        _reg._registry, _reg._configured = saved_reg

    evidence["ok"] = True
    return evidence


def run_runtime_smoke_evidence(
    *,
    core_version: str,
    community_version: str,
    core_commit: str,
    community_commit: str,
    core_wheel_hash: str,
    community_wheel_hash: str,
    removed_dependencies: tuple[str, ...] = (),
    community_adapters_registered: tuple[str, ...] = (),
) -> dict[str, object]:
    """Run the Community smoke and emit the R15B evidence package."""
    from okto_pulse.community.adapters.smoke_evidence import (
        build_community_runtime_smoke_evidence,
    )

    evidence = run_preservation_smoke()
    return build_community_runtime_smoke_evidence(
        core_version=core_version,
        community_version=community_version,
        core_commit=core_commit,
        community_commit=community_commit,
        core_wheel_hash=core_wheel_hash,
        community_wheel_hash=community_wheel_hash,
        routes=tuple(evidence.get("routes") or ()),
        mcp_tools=tuple(evidence.get("mcp_tools") or ()),
        cli_commands=tuple(evidence.get("cli_commands_present") or ()),
        commands_executed=("python scripts/r05e_community_preservation_smoke.py",),
        artifact_paths={"runner": "scripts/r05e_community_preservation_smoke.py"},
        removed_dependencies=removed_dependencies,
        community_adapters_registered=community_adapters_registered,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        if "--json-evidence" in argv:
            payload = run_runtime_smoke_evidence(
                core_version=os.environ.get("R15B_CORE_VERSION", "0.3.0"),
                community_version=os.environ.get("R15B_COMMUNITY_VERSION", "0.3.0"),
                core_commit=os.environ.get("R15B_CORE_COMMIT", "working-tree"),
                community_commit=os.environ.get("R15B_COMMUNITY_COMMIT", "working-tree"),
                core_wheel_hash=os.environ.get("R15B_CORE_WHEEL_HASH", "sha256:working-tree-core"),
                community_wheel_hash=os.environ.get(
                    "R15B_COMMUNITY_WHEEL_HASH", "sha256:working-tree-community"
                ),
                removed_dependencies=tuple(
                    item for item in os.environ.get("R15B_REMOVED_DEPENDENCIES", "").split(",") if item
                ),
                community_adapters_registered=tuple(
                    item for item in os.environ.get("R15B_COMMUNITY_ADAPTERS", "").split(",") if item
                ),
            )
            print(json.dumps(payload, sort_keys=True))
            return 0
        evidence = run_preservation_smoke()
    except AssertionError as exc:
        print(f"{_FAILURE_SENTINEL}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # environment / import error — not a preservation regression
        print(f"SMOKE_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print("R05-E IMP2 Community functional preservation (offline):")
    for key in (
        "embedding_mode",
        "embedding_is_stub",
        "composition_providers",
        "seed_returned",
        "route_count",
        "api_v1_route_count",
        "has_health_route",
        "mcp_asgi_app",
        "mcp_tool_count",
        "has_create_ideation_tool",
        "cli_commands_present",
    ):
        print(f"  {key}: {evidence.get(key)}")
    print(_SUCCESS_SENTINEL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
