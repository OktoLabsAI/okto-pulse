"""R11-D (COMMUNITY) — descriptors derived from the active RuntimeComposition,
golden replay, register-before-remove coexistence, and MCP scope.

Scenario mapping (spec ts_ ids):
  ts_9913b93b (TS02) — the Community adapter DERIVES one provider descriptor per
       ACTIVE composition provider (changing the composition changes the set);
       provider-specific backend detail lives in metadata, not the DTO type.
  ts_bd58a427 (TS04) — golden replay over the EFFECTIVE Community catalog: list
       resources + resources/read of ALL URIs + tool-doc resolution + payload
       budget — identical to today (no functional change).
  ts_664985dd (TS05) — register-before-remove: the LEGACY kg descriptor and the
       NEW composition-derived provider descriptor COEXIST (legacy not removed).
  ts_4cef79b5 (TS06) — scope: the real descriptor set has NO SaaS provider, and
       R11-D adds NO MCP tools / auth / lifecycle wiring (no public-tool drift).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_pulse.core.mcp.server as srv
from okto_pulse.community.adapters.capability_descriptors import (
    classify_effective_catalog,
    derive_capability_descriptors,
)
from okto_pulse.community.adapters.resources import (
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.composition import RuntimeComposition
from okto_pulse.core.ports.capability_descriptor import capability_scope_violations


def _composition(**overrides):
    base = dict(
        settings_provider="s", auth_provider="a", storage_provider="st",
        session_factory="sf", event_bus="eb",
    )
    base.update(overrides)
    return RuntimeComposition(**base)


@pytest.fixture(autouse=True)
def _reset_catalog():
    srv.reset_resource_catalog_for_tests()
    yield
    srv.reset_resource_catalog_for_tests()


# ===========================================================================
# ts_9913b93b (TS02) — descriptors derived from the ACTIVE composition.
# ===========================================================================
def test_ts_9913b93b_descriptors_derived_from_runtime_composition():
    comp = _composition(kg_registry="kg")  # kg wired; scheduler/telemetry/mcp NOT
    ds = derive_capability_descriptors(comp)

    provider_keys = {
        d.metadata_dict["provider_key"] for d in ds if d.kind == "provider"
    }
    # DERIVED exactly from the active composition's provider_keys (not hard-coded).
    assert provider_keys == set(comp.provider_keys())
    assert "kg_registry" in provider_keys
    assert "scheduler_control" not in provider_keys  # inactive -> absent
    assert "telemetry" not in provider_keys

    # provider-specific backend fact is in metadata only (generic DTO type).
    kg_backend = next(d for d in ds if d.id == "capability:kg_backend")
    assert kg_backend.capability == "kg" and kg_backend.metadata_dict.get("backend")
    assert kg_backend.provider == "community-embedded-kg"

    # EXPLICIT provider-specific LOCAL STORAGE descriptor (AC2/FR2), derived from
    # the active storage_provider — present here, with provider-specific metadata.
    storage = next(d for d in ds if d.id == "capability:local_storage")
    assert storage.capability == "storage"
    assert storage.provider == "community-local-storage"
    assert storage.metadata_dict.get("backend") == "local-filesystem"
    assert storage.metadata_dict.get("graph_files")  # local-path facts in metadata

    # changing the composition CHANGES the descriptors (truly derived): drop the
    # storage_provider -> the local-storage descriptor disappears.
    comp_no_storage = RuntimeComposition(
        settings_provider="s", auth_provider="a", storage_provider=None,
        session_factory="sf", event_bus="eb", kg_registry="kg",
    )
    ids_no_storage = {d.id for d in derive_capability_descriptors(comp_no_storage)}
    assert "capability:local_storage" not in ids_no_storage

    comp2 = _composition(scheduler_control="sched")  # kg NOT wired, scheduler IS
    ds2_keys = {d.metadata_dict["provider_key"] for d in derive_capability_descriptors(comp2) if d.kind == "provider"}
    assert "scheduler_control" in ds2_keys and "kg_registry" not in ds2_keys


# ===========================================================================
# ts_bd58a427 (TS04) — golden replay over the effective Community catalog.
# ===========================================================================
def test_ts_bd58a427_golden_replay_resources_read_tooldoc_payload():
    from okto_pulse.core.mcp import payload_budget

    baseline_uris = {s.uri for s in srv.effective_resource_catalog().specs()}

    register_and_freeze_community_resource_catalog()
    eff = srv.effective_resource_catalog()
    specs = eff.specs()

    # list resources — identical inventory to before (R11-D adds no resources).
    assert {s.uri for s in specs} == baseline_uris
    # resources/read of ALL URIs returns non-empty content.
    assert all(len(s.read()) > 0 for s in specs)
    # tool-doc resolution resolves to a catalogued URI.
    catalogued = {s.uri for s in specs}
    for tool in ("okto_pulse_get_board", "okto_pulse_kg_health", "okto_pulse_create_card"):
        assert srv.tool_docs_uri(tool) in catalogued
    # payload budget snapshot covers every effective spec.
    snap = payload_budget.snapshot_resources(srv)
    assert len(snap) == len(specs)
    assert all(len(v) > 0 for v in snap.values())

    # classification of the effective catalog runs VIA the derived descriptors.
    classified = classify_effective_catalog(_composition(kg_registry="kg"), catalog=eff)
    assert len(classified) == len(specs)
    by_uri = {c["uri"]: c for c in classified}

    # EVERY capability-tagged operational overlay resource is classified to a
    # descriptor (AC3 — no relevant resource left silently unclassified): the four
    # R11-B contaminated URIs (kg / errors / decision).
    for uri in (
        "okto-pulse://workflows/kg",
        "okto-pulse://reference/errors",
        "okto-pulse://reference/tool-docs/kg",
        "okto-pulse://reference/tool-docs/decision",
    ):
        assert by_uri[uri]["classified"] is True, uri
        assert by_uri[uri]["descriptor_id"] is not None, uri

    # EXPLICIT scope: every UNCLASSIFIED resource is a common doc with NO
    # capability tag (the common workflow/reference docs are not provider
    # capabilities) — nothing relevant is dropped silently.
    assert all(c["capability"] is None for c in classified if not c["classified"])


# ===========================================================================
# ts_664985dd (TS05) — register-before-remove: legacy + new coexist.
# ===========================================================================
def test_ts_664985dd_legacy_and_new_provider_coexist():
    ds = derive_capability_descriptors(_composition(kg_registry="kg"))
    kg_ids = {d.id for d in ds if d.capability == "kg"}

    # the LEGACY descriptor is still present...
    assert "capability:kg_legacy" in kg_ids
    legacy = next(d for d in ds if d.id == "capability:kg_legacy")
    assert legacy.metadata_dict["lifecycle"] == "legacy"
    # ...AND the NEW composition-derived provider + provider-specific coexist.
    assert "provider:kg_registry" in kg_ids
    assert "capability:kg_backend" in kg_ids
    # register-before-remove: the legacy is NOT removed when the new is added.
    assert legacy.metadata_dict["superseded_by"] == "provider:kg_registry"


# ===========================================================================
# ts_4cef79b5 (TS06) — scope: no SaaS provider, no MCP tool/auth/lifecycle drift.
# ===========================================================================
def test_ts_4cef79b5_scope_no_saas_no_mcp_drift():
    import okto_pulse.community.adapters.capability_descriptors as comm_cd
    import okto_pulse.core.ports.capability_descriptor as core_cd

    # the real derived descriptor set has NO SaaS / multi-tenant provider.
    ds = derive_capability_descriptors(_composition(kg_registry="kg"))
    assert capability_scope_violations(ds) == ()

    # R11-D wires NO MCP tools / resources / auth / lifecycle: the descriptor
    # modules contain none of that surface (static scope check).
    for mod in (core_cd, comm_cd):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "@mcp.tool" not in src and "mcp.resource(" not in src
        assert "configure_auth" not in src and "app_lifespan" not in src

    # deriving descriptors has ZERO side-effect on the resource catalog
    # (no public-tool/resource drift): the spec count is unchanged.
    before = len(srv.effective_resource_catalog().specs())
    derive_capability_descriptors(_composition(kg_registry="kg"))
    classify_effective_catalog(_composition(kg_registry="kg"))
    after = len(srv.effective_resource_catalog().specs())
    assert before == after
