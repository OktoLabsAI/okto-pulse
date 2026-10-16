"""Community operational composition: preserve common contracts and URI identity.

The effective KG workflow is the injected Core body plus edition-only notes.
Errors and tool docs are Core-owned, without alternate handwritten copies.
"""

from __future__ import annotations


import pytest

import okto_pulse.core.mcp.server as srv
from okto_pulse.community.adapters.resources import (
    COMMUNITY_RESOURCE_PRECEDENCE,
    _COMMUNITY_REPLACEMENT_TABLE,
    _OPERATIONAL_DIR,
    build_community_resource_catalog,
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.ports.mcp_resources import (
    McpResourceCatalogError,
    McpResourceSpec,
    StaticMcpResourceCatalog,
    McpResourceReplacementTarget,
    catalog_link_integrity,
    catalog_uri_conflicts,
    scan_forbidden_terms,
)

_SPLIT_URIS = tuple(uri for uri, _, _ in _COMMUNITY_REPLACEMENT_TABLE)


@pytest.fixture(autouse=True)
def _reset_catalog():
    srv.reset_resource_catalog_for_tests()
    global _COMMON_BODIES
    _COMMON_BODIES = {s.uri: s.read() for s in srv.effective_resource_catalog().specs()}
    yield
    srv.reset_resource_catalog_for_tests()


def _expected_body(uri, relative_path):
    return _COMMON_BODIES[uri].rstrip() + "\n\n" + (
        _OPERATIONAL_DIR / relative_path
    ).read_text(encoding="utf-8")


def _effective_specs():
    return {s.uri: s for s in srv.effective_resource_catalog().specs()}


def test_common_update_is_consumed_without_editing_community_notes():
    """The regression is a divergent copy, so exercise an injected update."""
    uri = "okto-pulse://workflows/kg"
    source = McpResourceSpec(
        uri=uri, description="Common", category="workflows", edition="core",
        path="workflows/kg.md", content="# Updated common contract\n",
    )
    catalog = build_community_resource_catalog(StaticMcpResourceCatalog("core", (source,)))
    body = catalog.specs()[0].read()
    assert body.startswith("# Updated common contract\n\n")
    assert body.count("## Community runtime notes") == 1
    assert "Transparent Grafx checkpoint recovery" in body
    assert "## Consolidation Primitives" not in body  # no hidden old copy


def test_composition_refuses_missing_or_already_replaced_common_workflow():
    with pytest.raises(McpResourceCatalogError, match="common_resource_required"):
        build_community_resource_catalog(StaticMcpResourceCatalog("core", ()))
    composed = build_community_resource_catalog(srv.effective_resource_catalog())
    with pytest.raises(McpResourceCatalogError, match="common_resource_required"):
        build_community_resource_catalog(composed)


# ===========================================================================
# ts_b12ee5a7 (TS01) — inventory before/after: zero missing/unexpected.
# ===========================================================================
def test_ts_b12ee5a7_inventory_unchanged_after_replacement(active_runtime_registry):
    before = {s.uri for s in srv.effective_resource_catalog().specs()}
    register_and_freeze_community_resource_catalog(active_runtime_registry)
    after = {s.uri for s in srv.effective_resource_catalog().specs()}
    assert after == before, {
        "missing": sorted(before - after),
        "unexpected": sorted(after - before),
    }
    # The edition-extended URIs are replaced in-place, never added or removed.
    assert set(_SPLIT_URIS) <= after


# ===========================================================================
# ts_5563f5cb (TS03) — Community runbook content preserved (not removed).
# ===========================================================================
def test_ts_5563f5cb_operational_runbooks_preserved():
    for uri, rel, _cap in _COMMUNITY_REPLACEMENT_TABLE:
        body = (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")
        assert len(body) > 0, uri  # not removed / not empty
    # the edition notes still explain the concrete runtime — at least one backend term remains.
    kg_body = (_OPERATIONAL_DIR / "workflows/kg.md").read_text(encoding="utf-8").lower()
    assert "ladybug" in kg_body or ".lbug" in kg_body or "sqlite" in kg_body

    # Every replacement names both stable source identities, has higher explicit
    # precedence, and serves the composed body.
    catalog = build_community_resource_catalog(srv.effective_resource_catalog())
    assert catalog.precedence == COMMUNITY_RESOURCE_PRECEDENCE
    assert catalog.precedence > 0
    specs = {s.uri: s for s in catalog.specs()}
    assert set(specs) == set(_SPLIT_URIS)
    for uri, rel, cap in _COMMUNITY_REPLACEMENT_TABLE:
        s = specs[uri]
        assert s.edition == "community"
        assert s.provider == "community-embedded-kg"
        assert s.capability == cap
        assert s.same_uri_overlay is False
        assert s.kind == "operational"
        assert s.mime_type == "text/markdown"
        assert s.source_identity == f"community:{rel}"
        assert s.replace_target == McpResourceReplacementTarget(
            edition="core",
            source_identity=f"core:{rel}",
        )
        assert s.read() == _expected_body(uri, rel)


# ===========================================================================
# ts_328960e4 (TS04) — exact common-prefix and edition-suffix contract.
# ===========================================================================
def test_ts_328960e4_byte_equivalence_replacement_equals_today(
    active_runtime_registry,
):
    register_and_freeze_community_resource_catalog(active_runtime_registry)
    specs = _effective_specs()

    for uri, rel, _cap in _COMMUNITY_REPLACEMENT_TABLE:
        assert specs[uri].read() == _expected_body(uri, rel)
    # Shared contract changes must reach Community unchanged; only runtime
    # notes are edition-owned. No pre-split copy can mask an update.
    for uri in ("okto-pulse://reference/errors", "okto-pulse://reference/tool-docs/kg",
                "okto-pulse://reference/tool-docs/decision"):
        assert specs[uri].edition == "core"
        assert specs[uri].read() == _COMMON_BODIES[uri]
    # The winning spec is operational, so its backend terms are exempt from the
    # common-resource forbidden scan.
    assert all(specs[u].kind == "operational" for u in _SPLIT_URIS)
    for uri, rel, _cap in _COMMUNITY_REPLACEMENT_TABLE:
        assert specs[uri].source_identity == f"community:{rel}"
        assert specs[uri].replaced_source_identity == f"core:{rel}"


# ===========================================================================
# ts_5b73ba44 (TS05) — payload budget uses the EFFECTIVE winning catalog.
# ===========================================================================
def test_ts_5b73ba44_payload_snapshot_uses_effective_catalog(active_runtime_registry):
    from okto_pulse.core.mcp import payload_budget

    register_and_freeze_community_resource_catalog(active_runtime_registry)
    eff = srv.effective_resource_catalog()
    snapshot = payload_budget.snapshot_resources(srv)

    # One snapshot entry per effective winner, not per raw declaration.
    assert len(snapshot) == len(eff.specs())
    # The composed replacements appear under the effective spec's stable projection
    # key and retain the exact frozen body.
    specs = _effective_specs()
    for uri, rel, _cap in _COMMUNITY_REPLACEMENT_TABLE:
        projection_key = specs[uri].path or uri
        assert projection_key in snapshot
        assert snapshot[projection_key] == specs[uri].read()
        assert snapshot[projection_key] == _expected_body(uri, rel)


# ===========================================================================
# ts_7c58a410 (TS06) — replay resources/read + tool-doc links over effective.
# ===========================================================================
def test_ts_7c58a410_replay_resources_read_and_links(active_runtime_registry):
    register_and_freeze_community_resource_catalog(active_runtime_registry)
    eff = srv.effective_resource_catalog()
    specs = _effective_specs()

    # resources/read resolves from the effective replacement, non-empty.
    for uri, rel, _cap in _COMMUNITY_REPLACEMENT_TABLE:
        replacement = specs[uri].read()
        assert replacement == _expected_body(uri, rel)
        assert len(replacement) > 0

    # tool-doc links + every okto-pulse:// link inside content resolve to a
    # catalogued URI (link integrity over the effective catalog).
    assert catalog_link_integrity(eff) == ()
    # No raw conflict remains after explicit replacement validation.
    assert catalog_uri_conflicts(eff) == ()
    # The operational replacements are exempt; no common spec leaks a backend.
    assert scan_forbidden_terms(eff) == ()


def test_related_context_contract_survives_community_replacement(
    active_runtime_registry,
):
    """The Community replacement must not restore raw-UUID examples."""
    register_and_freeze_community_resource_catalog(active_runtime_registry)
    specs = _effective_specs()

    workflow = specs["okto-pulse://workflows/kg"].read()
    tool_docs = specs["okto-pulse://reference/tool-docs/kg"].read()

    assert 'artifact_id="spec:<uuid>"' in workflow
    assert '"card:<uuid>"' in workflow
    assert "Typed source reference: ``spec:<uuid>`` or ``card:<uuid>``" in tool_docs
    assert "artifact_id=<spec_id>" not in workflow
    assert "<formalized_node_or_artifact_id>" not in workflow


def test_retired_recovery_and_outbox_are_absent_from_effective_community_resources(active_runtime_registry):
    register_and_freeze_community_resource_catalog(active_runtime_registry)
    specs = _effective_specs()
    workflow = specs["okto-pulse://workflows/kg"].read()
    tool_docs = specs["okto-pulse://reference/tool-docs/kg"].read()
    assert "Health reports terminal delivery failures separately" in workflow
    for family in ("global_outbox_dead_letter", "global_discovery_recovery", "quarantine_restore"):
        assert f"okto_pulse_kg_{family}" not in workflow
        assert f"okto_pulse_kg_{family}" not in tool_docs
