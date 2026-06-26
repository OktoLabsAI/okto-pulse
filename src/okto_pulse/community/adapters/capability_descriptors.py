"""Community capability descriptors (spec R11-D, IMP2) — DERIVE the capability
descriptors from the ACTIVE ``RuntimeComposition`` (never hard-coded) and declare
the Community provider-specific KG backend / storage / MCP-resource capabilities
through the CORE contracts (``okto_pulse.core.ports.capability_descriptor``). core
never imports community; the provider-specific detail lives only in each
descriptor's bounded ``metadata`` — it does NOT contaminate the common DTO type.

register-before-remove (IMP4): a LEGACY capability descriptor and the NEW
composition-derived provider descriptor COEXIST during the transition (the legacy
is not removed).
"""

from __future__ import annotations

from typing import Any

from okto_pulse.core.ports.capability_descriptor import (
    DESCRIPTOR_KIND_CAPABILITY,
    DESCRIPTOR_KIND_PROVIDER,
    CapabilityDescriptor,
    classify_resources,
)

COMMUNITY_EDITION = "community"
COMMUNITY_PROVIDER = "community-runtime"
COMMUNITY_KG_PROVIDER = "community-embedded-kg"

#: Maps a RuntimeComposition provider key -> the capability it satisfies. This is
#: the EDITION's knowledge (the core port stays generic); the descriptors are
#: still DERIVED from which keys the active composition actually supplies.
_PROVIDER_CAPABILITY: dict[str, str] = {
    "settings_provider": "settings",
    "auth_provider": "auth",
    "storage_provider": "storage",
    "session_factory": "persistence",
    "event_bus": "events",
    "kg_registry": "kg",
    "scheduler_control": "scheduler",
    "telemetry": "telemetry",
    "mcp_session_factory": "mcp",
}


def _active_provider_keys(composition: Any) -> tuple[str, ...]:
    """The provider keys the ACTIVE composition supplies (non-None) — duck-typed
    over ``RuntimeComposition.provider_keys`` so a test stub also works."""
    keys = composition.provider_keys()
    return tuple(keys)


def _derived_provider_descriptors(composition: Any) -> tuple[CapabilityDescriptor, ...]:
    """One PROVIDER descriptor per ACTIVE provider in the composition (derived,
    not hard-coded — only wired providers appear)."""
    return tuple(
        CapabilityDescriptor(
            id=f"provider:{key}",
            kind=DESCRIPTOR_KIND_PROVIDER,
            provider=COMMUNITY_PROVIDER,
            edition=COMMUNITY_EDITION,
            capability=_PROVIDER_CAPABILITY.get(key, key),
            metadata={"provider_key": key, "source": "runtime_composition"},
        )
        for key in _active_provider_keys(composition)
    )


COMMUNITY_STORAGE_PROVIDER = "community-local-storage"


def _provider_specific_descriptors() -> tuple[CapabilityDescriptor, ...]:
    """Community provider-specific capability declarations (KG backend / MCP
    resources). The backend facts live in ``metadata`` only — generic DTO."""
    return (
        CapabilityDescriptor(
            id="capability:kg_backend",
            kind=DESCRIPTOR_KIND_CAPABILITY,
            provider=COMMUNITY_KG_PROVIDER,
            edition=COMMUNITY_EDITION,
            capability="kg",
            metadata={"backend": "embedded-graph-db", "storage": "per-board-graph-store"},
        ),
        CapabilityDescriptor(
            id="capability:mcp_resources",
            kind=DESCRIPTOR_KIND_CAPABILITY,
            provider=COMMUNITY_KG_PROVIDER,
            edition=COMMUNITY_EDITION,
            capability="mcp_resources",
            metadata={"overlay": "operational", "catalog": "effective"},
        ),
    )


def _local_storage_descriptor() -> CapabilityDescriptor:
    """(R11-D point 1 / AC2-FR2) The EXPLICIT Community provider-specific LOCAL
    STORAGE descriptor — emitted only when the active composition wires a
    storage_provider (derived, not hard-coded). All local-path / file facts live
    in metadata, never in the common DTO type."""
    return CapabilityDescriptor(
        id="capability:local_storage",
        kind=DESCRIPTOR_KIND_CAPABILITY,
        provider=COMMUNITY_STORAGE_PROVIDER,
        edition=COMMUNITY_EDITION,
        capability="storage",
        metadata={
            "backend": "local-filesystem",
            "graph_files": "per-board-graph-store",
            "uploads": "local-uploads-dir",
            "data_dir": "local-data-dir",
        },
    )


def _operational_resource_descriptors() -> tuple[CapabilityDescriptor, ...]:
    """A capability descriptor per DISTINCT operational-overlay capability (R11-B
    contaminated URIs: kg/errors/decision), DERIVED from the Community overlay
    table — so the classification covers EVERY capability-tagged resource in the
    effective catalog (no relevant resource left silently unclassified). ``kg`` is
    already covered by ``capability:kg_backend``; this fills the rest."""
    from okto_pulse.community.adapters.resources import _COMMUNITY_OVERLAY_TABLE

    caps = sorted({cap for _, _, cap in _COMMUNITY_OVERLAY_TABLE} - {"kg"})
    return tuple(
        CapabilityDescriptor(
            id=f"capability:operational_{cap}",
            kind=DESCRIPTOR_KIND_CAPABILITY,
            provider=COMMUNITY_KG_PROVIDER,
            edition=COMMUNITY_EDITION,
            capability=cap,
            metadata={"source": "operational_overlay"},
        )
        for cap in caps
    )


def _legacy_descriptors() -> tuple[CapabilityDescriptor, ...]:
    """register-before-remove: the LEGACY kg capability descriptor that coexists
    with the new composition-derived ``provider:kg_registry`` during transition.
    It is NOT removed here (R11-D is additive)."""
    return (
        CapabilityDescriptor(
            id="capability:kg_legacy",
            kind=DESCRIPTOR_KIND_CAPABILITY,
            provider=COMMUNITY_PROVIDER,
            edition=COMMUNITY_EDITION,
            capability="kg",
            metadata={"lifecycle": "legacy", "superseded_by": "provider:kg_registry"},
        ),
    )


class CommunityCapabilityDescriptorSource:
    """Satisfies the core ``CapabilityDescriptorSource`` Protocol. DERIVES its
    descriptors from the bound active composition + the Community provider-specific
    + legacy declarations (stable, sorted-by-id order)."""

    def __init__(self, composition: Any) -> None:
        self._composition = composition

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        derived = _derived_provider_descriptors(self._composition)
        out = [
            *derived,
            *_provider_specific_descriptors(),
            *_operational_resource_descriptors(),
            *_legacy_descriptors(),
        ]
        # provider-specific LOCAL STORAGE descriptor — derived: only when the
        # active composition actually wires a storage_provider.
        if "storage_provider" in set(self._composition.provider_keys()):
            out.append(_local_storage_descriptor())
        return tuple(sorted(out, key=lambda d: d.id))


def derive_capability_descriptors(composition: Any) -> tuple[CapabilityDescriptor, ...]:
    """Build the Community descriptor set from the ACTIVE composition (IMP2)."""
    return CommunityCapabilityDescriptorSource(composition).descriptors()


def classify_effective_catalog(
    composition: Any, *, catalog: Any | None = None
) -> tuple[dict, ...]:
    """Classify the EFFECTIVE MCP resource catalog's specs by the derived
    descriptors (VIA the descriptors, not a hard-coded table). When ``catalog`` is
    omitted, the live effective catalog from the core MCP server is used."""
    if catalog is None:
        from okto_pulse.core.mcp import server as core_mcp_server

        catalog = core_mcp_server.effective_resource_catalog()
    descriptors = derive_capability_descriptors(composition)
    return classify_resources(tuple(catalog.specs()), descriptors)


__all__ = [
    "COMMUNITY_EDITION",
    "COMMUNITY_PROVIDER",
    "COMMUNITY_KG_PROVIDER",
    "CommunityCapabilityDescriptorSource",
    "derive_capability_descriptors",
    "classify_effective_catalog",
]
