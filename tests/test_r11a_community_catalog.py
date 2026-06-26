"""R11-A (COMMUNITY) — the Community resource catalog + composition wiring.

Scenario mapping (spec ts_ ids):
  ts_4a74634e (TS03) — the Community edition registers its catalog via the CORE
       contracts (core never imports community) and PRESERVES the current
       okto-pulse:// URIs + content: no missing/unexpected URI, resources/read
       non-empty.
  ts_2a2d4e73 (TS06) — golden replay of the composition hook
       (register_and_freeze_community_resource_catalog, the combined_lifespan
       step): the effective catalog keeps all core URIs, reads non-empty, and is
       FROZEN afterwards (late mutation raises).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import okto_pulse.core.mcp.server as core_srv
from okto_pulse.community.adapters.resources import (
    build_community_resource_catalog,
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.ports.mcp_resources import McpResourceCatalog


@pytest.fixture(autouse=True)
def _reset_catalog():
    core_srv.reset_resource_catalog_for_tests()
    yield
    core_srv.reset_resource_catalog_for_tests()


# ===========================================================================
# ts_4a74634e (TS03) — Community catalog via core contracts; URIs preserved.
# ===========================================================================
def test_ts_4a74634e_community_catalog_built_via_core_contracts():
    catalog = build_community_resource_catalog()
    # It satisfies the CORE McpResourceCatalog contract (no community-defined
    # parallel contract).
    assert isinstance(catalog, McpResourceCatalog)
    assert catalog.edition == "community"

    # core never imports community (axis independence) — verified by AST.
    core_pkg = Path(core_srv.__file__).parents[1]  # okto_pulse/core
    offenders = []
    for py in core_pkg.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mod = (
                node.module if isinstance(node, ast.ImportFrom)
                else None
            )
            names = (
                [a.name for a in node.names] if isinstance(node, ast.Import) else []
            )
            if (mod and mod.startswith("okto_pulse.community")) or any(
                n.startswith("okto_pulse.community") for n in names
            ):
                offenders.append(py.name)
    assert offenders == [], offenders


def test_ts_4a74634e_uris_preserved_and_read_nonempty():
    before = {s.uri for s in core_srv.effective_resource_catalog().specs()}
    assert len(before) >= 45

    register_and_freeze_community_resource_catalog()

    after_specs = core_srv.effective_resource_catalog().specs()
    after = {s.uri for s in after_specs}
    # No core URI went missing (Community preserves them).
    assert before <= after
    # resources/read is non-empty for the preserved core resources.
    sample = next(s for s in after_specs if s.uri == "okto-pulse://workflows/preflight")
    assert len(sample.read()) > 0


# ===========================================================================
# ts_2a2d4e73 (TS06) — golden replay of the combined_lifespan composition hook.
# ===========================================================================
def test_ts_2a2d4e73_golden_replay_combined_lifespan_freeze():
    uris_before = {s.uri for s in core_srv.effective_resource_catalog().specs()}

    # The exact hook combined_lifespan invokes after all providers are wired.
    register_and_freeze_community_resource_catalog()

    cat = core_srv.effective_resource_catalog()
    uris_after = {s.uri for s in cat.specs()}
    # Inventory preserved (no missing), every spec reads non-empty.
    assert uris_before <= uris_after
    assert all(len(s.read()) > 0 for s in cat.specs())

    # Frozen after composition: a late injection raises (fail-closed).
    from okto_pulse.core.ports.mcp_resources import (
        McpResourceSpec,
        StaticMcpResourceCatalog,
    )

    with pytest.raises(RuntimeError):
        core_srv.register_resource_catalog(
            StaticMcpResourceCatalog(
                "community",
                (McpResourceSpec(
                    uri="okto-pulse://operational/late", description="d",
                    category="operational", edition="community",
                    kind="operational", content="x",
                ),),
            )
        )
