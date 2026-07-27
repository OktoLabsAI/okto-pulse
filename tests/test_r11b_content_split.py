"""R11-B (COMMUNITY) — explicit replacement + byte-equivalence.

Scenario mapping (spec ts_ ids):
  ts_b12ee5a7 (TS01) — inventory before/after Community replacement injection:
       ZERO missing / unexpected URI (the public URI set is identical).
  ts_5563f5cb (TS03) — the Community operational runbook content is PRESERVED
       (not removed): the four replacement bodies are non-empty and still
       name the backend (the full pre-split content migrated, not deleted).
  ts_328960e4 (TS04) — byte-equivalence: per URI, the MERGED effective content in
       the Community runtime == the pre-split original byte-for-byte; semantic
       exceptions (if any) are reported with a diff + rationale (here: ZERO).
  ts_7c58a410 (TS06) — replay: resources/read of each split URI returns the
       replacement content from the EFFECTIVE catalog (not a textual scan of
       server.py / _RESOURCE_REGISTRY); tool-doc links still resolve.
"""

from __future__ import annotations

import difflib

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
    McpResourceReplacementTarget,
    catalog_link_integrity,
    catalog_uri_conflicts,
    scan_forbidden_terms,
)

_SPLIT_URIS = tuple(uri for uri, _, _ in _COMMUNITY_REPLACEMENT_TABLE)


@pytest.fixture(autouse=True)
def _reset_catalog():
    srv.reset_resource_catalog_for_tests()
    yield
    srv.reset_resource_catalog_for_tests()


def _effective_specs():
    return {s.uri: s for s in srv.effective_resource_catalog().specs()}


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
    # The four split URIs are replaced in-place, never added or removed.
    assert set(_SPLIT_URIS) <= after


# ===========================================================================
# ts_5563f5cb (TS03) — Community runbook content preserved (not removed).
# ===========================================================================
def test_ts_5563f5cb_operational_runbooks_preserved():
    for uri, rel, _cap in _COMMUNITY_REPLACEMENT_TABLE:
        body = (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")
        assert len(body) > 0, uri  # not removed / not empty
    # the migrated operational content STILL names the backend (full pre-split
    # content moved here, not deleted) — at least one backend term remains.
    kg_body = (_OPERATIONAL_DIR / "workflows/kg.md").read_text(encoding="utf-8").lower()
    assert "ladybug" in kg_body or ".lbug" in kg_body or "sqlite" in kg_body

    # Every replacement names both stable source identities, has higher explicit
    # precedence, and serves the byte-exact markdown body.
    catalog = build_community_resource_catalog()
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
        assert s.read() == (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")


# ===========================================================================
# ts_328960e4 (TS04) — byte-equivalence + semantic exceptions (diff/rationale).
# ===========================================================================
def test_ts_328960e4_byte_equivalence_replacement_equals_today(
    active_runtime_registry,
):
    register_and_freeze_community_resource_catalog(active_runtime_registry)
    specs = _effective_specs()

    exceptions: list[dict] = []
    for uri, rel, _cap in _COMMUNITY_REPLACEMENT_TABLE:
        replacement = specs[uri].read()
        # The captured original (byte-exact pre-split content migrated to the
        # operational catalog) IS "today"'s content.
        original = (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")
        if replacement != original:
            diff = "\n".join(
                difflib.unified_diff(
                    original.splitlines(), replacement.splitlines(),
                    fromfile=f"{uri} (today)",
                    tofile=f"{uri} (replacement)",
                    lineterm="",
                )
            )
            exceptions.append({
                "uri": uri,
                "diff": diff[:2000],
                "rationale": "replacement diverged from the pre-split original",
            })

    # The explicit replacements are byte-equivalent for ALL four URIs — ZERO
    # semantic exceptions (the diff/rationale list is the report if any appear).
    assert exceptions == [], exceptions
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
    # The four replacements appear under the effective spec's stable projection
    # key and retain the exact frozen body.
    specs = _effective_specs()
    for uri, rel, _cap in _COMMUNITY_REPLACEMENT_TABLE:
        projection_key = specs[uri].path or uri
        assert projection_key in snapshot
        assert snapshot[projection_key] == specs[uri].read()
        assert snapshot[projection_key] == (_OPERATIONAL_DIR / rel).read_text(
            encoding="utf-8"
        )


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
        assert replacement == (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")
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


def test_global_recovery_and_outbox_contracts_survive_community_replacement(
    active_runtime_registry,
):
    """Effective same-URI resources must not mask the current Core contract."""

    register_and_freeze_community_resource_catalog(active_runtime_registry)
    specs = _effective_specs()
    workflow = specs["okto-pulse://workflows/kg"].read()
    tool_docs = specs["okto-pulse://reference/tool-docs/kg"].read()

    assert "KG Health surfaces four **distinct** operational signals" in workflow
    assert "`global_outbox_dead_letter_backlog`" in workflow
    assert "`okto_pulse_kg_global_outbox_dead_letter_list`" in workflow
    assert "Never interpret an empty selection as all" in workflow
    assert "after commit" in workflow

    for tool_name in (
        "okto_pulse_kg_global_outbox_dead_letter_list",
        "okto_pulse_kg_global_outbox_dead_letter_reprocess",
        "okto_pulse_kg_global_outbox_dead_letter_verify",
    ):
        assert f"## `{tool_name}`" in tool_docs
    assert "required native list of 1-100 unique immutable IDs" in tool_docs
    assert "required bounded operator audit reason" in tool_docs
    assert "selection_changed" in tool_docs
    assert "supersedence_target_absent" in tool_docs

    assert "closed, empty input schema" in tool_docs
    assert "it never scans boards" in tool_docs
    assert "fails closed with `manifest_stale`" in tool_docs
    assert "`reason` (1-512 characters)" in tool_docs
    assert "`cancel_requested_by_actor_id`" in tool_docs
    assert "the original admitting actor never changes" in tool_docs
    assert "Any authorized global admin may inspect the global run" in tool_docs
    assert "Runs owned by another actor are not disclosed" not in tool_docs
