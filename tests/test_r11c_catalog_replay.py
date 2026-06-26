"""R11-C (COMMUNITY) — consolidated golden replay over the EFFECTIVE composed
Community catalog (catalog-aware). Test-only; EXTENDS R11-B/D, does not duplicate.

Scenario mapping:
  TS04 — golden replay over the effective Community catalog in ONE flow: list
       resources + resources/read of ALL URIs + tool-doc link resolution +
       byte-equivalence of the operational overlays + the selective common/
       operational forbidden scan — identical to today (no functional change).
"""

from __future__ import annotations

import pytest

import okto_pulse.core.mcp.server as srv
from okto_pulse.community.adapters.resources import (
    _COMMUNITY_OVERLAY_TABLE,
    _OPERATIONAL_DIR,
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.ports.mcp_resources import (
    catalog_link_integrity,
    catalog_uri_conflicts,
    scan_forbidden_terms,
)


@pytest.fixture(autouse=True)
def _reset_catalog():
    srv.reset_resource_catalog_for_tests()
    yield
    srv.reset_resource_catalog_for_tests()


def test_ts04_consolidated_golden_replay_effective_community_catalog():
    from okto_pulse.core.mcp import payload_budget

    baseline_uris = {s.uri for s in srv.effective_resource_catalog().specs()}

    # compose the Community catalog exactly as combined_lifespan does.
    register_and_freeze_community_resource_catalog()
    eff = srv.effective_resource_catalog()
    specs = eff.specs()
    by_uri = {s.uri: s for s in specs}

    # (a) list resources — inventory identical to before (no missing/unexpected).
    assert {s.uri for s in specs} == baseline_uris

    # (b) resources/read of ALL URIs returns non-empty content (read from the
    #     effective catalog, not a server.py text scan).
    assert all(len(s.read()) > 0 for s in specs)

    # (c) tool-doc link resolution: each tool resolves to a catalogued URI.
    catalogued = set(by_uri)
    for tool in ("okto_pulse_get_board", "okto_pulse_kg_health",
                 "okto_pulse_create_card", "okto_pulse_add_decision"):
        assert srv.tool_docs_uri(tool) in catalogued

    # (d) byte-equivalence: each operational overlay's merged content == the
    #     byte-exact pre-split original (today's content).
    for uri, rel, _cap in _COMMUNITY_OVERLAY_TABLE:
        assert by_uri[uri].read() == (_OPERATIONAL_DIR / rel).read_text(encoding="utf-8")

    # (e) selective scan: the effective catalog is clean (operational overlays are
    #     exempt; no COMMON spec leaks a backend term), links resolve, no conflict.
    assert scan_forbidden_terms(eff) == ()
    assert catalog_link_integrity(eff) == ()
    assert catalog_uri_conflicts(eff) == ()

    # (f) payload budget snapshot covers EVERY effective spec (incl. overlays).
    snap = payload_budget.snapshot_resources(srv)
    assert len(snap) == len(specs)
    assert all(len(v) > 0 for v in snap.values())
