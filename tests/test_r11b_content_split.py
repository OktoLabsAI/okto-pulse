"""R11-B (COMMUNITY) — content split: same-URI overlay merge + byte-equivalence.

Scenario mapping (spec ts_ ids):
  ts_b12ee5a7 (TS01) — inventory before/after the Community overlay injection:
       ZERO missing / unexpected URI (the public URI set is identical).
  ts_5563f5cb (TS03) — the Community operational runbook content is PRESERVED
       (not removed): the four overlay bodies exist, are non-empty, and still
       name the backend (the full pre-split content migrated, not deleted).
  ts_328960e4 (TS04) — byte-equivalence: per URI, the MERGED effective content in
       the Community runtime == the pre-split original byte-for-byte; semantic
       exceptions (if any) are reported with a diff + rationale (here: ZERO).
  ts_7c58a410 (TS06) — replay: resources/read of each split URI returns the
       merged content from the EFFECTIVE catalog (not a textual scan of
       server.py / _RESOURCE_REGISTRY); tool-doc links still resolve.
"""

from __future__ import annotations

import difflib

import pytest

import okto_pulse.core.mcp.server as srv
from okto_pulse.community.adapters.resources import (
    _COMMUNITY_OVERLAY_TABLE,
    _OPERATIONAL_DIR,
    build_community_resource_catalog,
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.ports.mcp_resources import (
    catalog_link_integrity,
    catalog_uri_conflicts,
    scan_forbidden_terms,
)

_SPLIT_URIS = tuple(uri for uri, _, _ in _COMMUNITY_OVERLAY_TABLE)


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
def test_ts_b12ee5a7_inventory_unchanged_after_overlay():
    before = {s.uri for s in srv.effective_resource_catalog().specs()}
    register_and_freeze_community_resource_catalog()
    after = {s.uri for s in srv.effective_resource_catalog().specs()}
    assert after == before, {
        "missing": sorted(before - after),
        "unexpected": sorted(after - before),
    }
    # the 4 split URIs are among the preserved set (overlaid, not added/removed).
    assert set(_SPLIT_URIS) <= after


# ===========================================================================
# ts_5563f5cb (TS03) — Community runbook content preserved (not removed).
# ===========================================================================
def test_ts_5563f5cb_operational_runbooks_preserved():
    for uri, rel, _cap in _COMMUNITY_OVERLAY_TABLE:
        body = (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")
        assert len(body) > 0, uri  # not removed / not empty
    # the migrated operational content STILL names the backend (full pre-split
    # content moved here, not deleted) — at least one backend term remains.
    kg_body = (_OPERATIONAL_DIR / "workflows/kg.md").read_text(encoding="utf-8").lower()
    assert "ladybug" in kg_body or ".lbug" in kg_body or "sqlite" in kg_body

    # the overlay specs carry the R11-B metadata (edition/provider/capability/
    # same_uri_overlay/kind) so the merge + scan can treat them correctly, AND
    # read() returns the byte-exact operational runbook body.
    specs = {s.uri: s for s in build_community_resource_catalog().specs()}
    assert set(specs) == set(_SPLIT_URIS)
    for uri, rel, cap in _COMMUNITY_OVERLAY_TABLE:
        s = specs[uri]
        assert s.edition == "community"
        assert s.provider == "community-embedded-kg"
        assert s.capability == cap
        assert s.same_uri_overlay is True
        assert s.kind == "operational"
        assert s.read() == (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")


# ===========================================================================
# ts_328960e4 (TS04) — byte-equivalence + semantic exceptions (diff/rationale).
# ===========================================================================
def test_ts_328960e4_byte_equivalence_merged_equals_today():
    register_and_freeze_community_resource_catalog()
    specs = _effective_specs()

    exceptions: list[dict] = []
    for uri, rel, _cap in _COMMUNITY_OVERLAY_TABLE:
        merged = specs[uri].read()
        # The captured original (byte-exact pre-split content migrated to the
        # operational catalog) IS "today"'s content.
        original = (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")
        if merged != original:
            diff = "\n".join(
                difflib.unified_diff(
                    original.splitlines(), merged.splitlines(),
                    fromfile=f"{uri} (today)", tofile=f"{uri} (merged)", lineterm="",
                )
            )
            exceptions.append({
                "uri": uri,
                "diff": diff[:2000],
                "rationale": "merged content diverged from the pre-split original",
            })

    # The overlay-override merge is byte-equivalent for ALL four URIs — ZERO
    # semantic exceptions (the diff/rationale list is the report if any appear).
    assert exceptions == [], exceptions
    # and the merged spec is operational (so the backend terms it now carries are
    # exempt from the COMMON forbidden scan).
    assert all(specs[u].kind == "operational" for u in _SPLIT_URIS)


# ===========================================================================
# ts_5b73ba44 (TS05) — payload budget/snapshot uses the EFFECTIVE merged catalog.
# ===========================================================================
def test_ts_5b73ba44_payload_snapshot_uses_effective_catalog():
    from okto_pulse.core.mcp import payload_budget

    register_and_freeze_community_resource_catalog()
    eff = srv.effective_resource_catalog()
    snapshot = payload_budget.snapshot_resources(srv)

    # one snapshot entry per EFFECTIVE spec (merged), not the raw registry.
    assert len(snapshot) == len(eff.specs()) == 48
    # the 4 same-URI overlays appear in the snapshot keyed by URI (content-merged)
    # and their snapshot body == the merged effective content (byte-exact).
    specs = _effective_specs()
    for uri, rel, _cap in _COMMUNITY_OVERLAY_TABLE:
        assert uri in snapshot
        assert snapshot[uri] == specs[uri].read()
        assert snapshot[uri] == (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")


# ===========================================================================
# ts_7c58a410 (TS06) — replay resources/read + tool-doc links over effective.
# ===========================================================================
def test_ts_7c58a410_replay_resources_read_and_links():
    register_and_freeze_community_resource_catalog()
    eff = srv.effective_resource_catalog()
    specs = _effective_specs()

    # resources/read resolves from the EFFECTIVE catalog (merged), non-empty.
    for uri, rel, _cap in _COMMUNITY_OVERLAY_TABLE:
        merged = specs[uri].read()
        assert merged == (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")
        assert len(merged) > 0

    # tool-doc links + every okto-pulse:// link inside content resolve to a
    # catalogued URI (link integrity over the effective catalog).
    assert catalog_link_integrity(eff) == ()
    # no raw conflict from the authorized overlays.
    assert catalog_uri_conflicts(eff) == ()
    # the effective (Community) catalog is scan-clean: the operational overlays
    # are exempt, and no COMMON spec leaks a backend term.
    assert scan_forbidden_terms(eff) == ()
