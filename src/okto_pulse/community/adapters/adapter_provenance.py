"""Executable F13 provenance registry for Community-owned adapters."""

from __future__ import annotations

from pathlib import Path

from okto_pulse.core.application.boundary.adapter_provenance import (
    audit_adapter_provenance,
)
from okto_pulse.core.application.boundary.public_contract_manifest import (
    PUBLIC_CORE_CONTRACT_MANIFEST_DIGEST,
    PUBLIC_CORE_CONTRACT_MANIFEST_VERSION,
    PUBLIC_CORE_CONTRACT_SURFACES as CORE_PUBLIC_CORE_CONTRACT_SURFACES,
)
from okto_pulse.core.ports.f13 import AdapterProvenanceRegistration


_CROSS_EDITION_CONTRACT_EXPECTATION: tuple[str, ...] = (
    "okto_pulse.community.api.auth_deps",
    "okto_pulse.community.app",
    "okto_pulse.core.AuthProvider",
    "okto_pulse.core.AuthenticationError",
    "okto_pulse.core.AuthenticationPort",
    "okto_pulse.core.AuthorizationDenied",
    "okto_pulse.core.CoreSettings",
    "okto_pulse.core.Credential",
    "okto_pulse.core.DEFAULT_STREAM_CHUNK_SIZE",
    "okto_pulse.core.InvalidCredential",
    "okto_pulse.core.MissingCredential",
    "okto_pulse.core.Principal",
    "okto_pulse.core.RelationalSchemaLifecycleOrchestrator",
    "okto_pulse.core.StorageObjectStat",
    "okto_pulse.core.StorageProvider",
    "okto_pulse.core.configure_auth",
    "okto_pulse.core.configure_settings",
    "okto_pulse.core.configure_storage",
    "okto_pulse.core.get_auth_provider",
    "okto_pulse.core.get_settings",
    "okto_pulse.core.get_storage_provider",
    "okto_pulse.core.register_package_version_provider",
    "okto_pulse.core.register_relational_schema_lifecycle_orchestrator",
    "okto_pulse.core.reset_auth_for_tests",
    "okto_pulse.core.reset_package_version_provider_for_tests",
    "okto_pulse.core.reset_relational_schema_lifecycle_orchestrator",
    "okto_pulse.core.resolve_relational_schema_lifecycle_orchestrator",
    "okto_pulse.core.application",
    "okto_pulse.core.composition",
    "okto_pulse.core.composition.isolated_runtime_provider_scope",
    "okto_pulse.core.discovery_intent_catalog",
    "okto_pulse.core.domain",
    "okto_pulse.core.events",
    "okto_pulse.core.events.types.QualityAssessmentRecorded",
    "okto_pulse.core.events.types.QualityClarificationChanged",
    "okto_pulse.core.inbound.enum_error_envelope",
    "okto_pulse.core.inbound.guideline_policy_cursor.policy_cursor_codec_from_settings",
    "okto_pulse.core.inbound.guideline_policy_error.guideline_policy_http_status",
    "okto_pulse.core.inbound.guideline_policy_error.project_guideline_policy_error",
    "okto_pulse.core.inbound.policy_transition_error.project_policy_transition_rejection",
    "okto_pulse.core.inbound.quality_assessment_error",
    "okto_pulse.core.inbound.ska_contract_error",
    "okto_pulse.core.kg.async_bridge",
    "okto_pulse.core.kg.board_rebuild_adapter",
    "okto_pulse.core.kg.board_source_store",
    "okto_pulse.core.kg.canonical_cognitive_preservation",
    "okto_pulse.core.kg.candidate_decision_store",
    "okto_pulse.core.kg.cognitive_action_center",
    "okto_pulse.core.kg.cognitive_badge_resolver",
    "okto_pulse.core.kg.cognitive_readiness",
    "okto_pulse.core.kg.config_guard",
    "okto_pulse.core.kg.cypher_templates",
    "okto_pulse.core.kg.cursor_codec",
    "okto_pulse.core.kg.data_provider_ownership_gate",
    "okto_pulse.core.kg.dedup_migration",
    "okto_pulse.core.kg.embedding",
    "okto_pulse.core.kg.global_discovery.schema",
    "okto_pulse.core.kg.graph_availability",
    "okto_pulse.core.kg.health",
    "okto_pulse.core.kg.health_state",
    "okto_pulse.core.kg.hybrid_search.hybrid",
    "okto_pulse.core.kg.interfaces",
    "okto_pulse.core.kg.kg_service",
    "okto_pulse.core.kg.orphan_integrity",
    "okto_pulse.core.kg.primitives",
    "okto_pulse.core.kg.quarantine",
    "okto_pulse.core.kg.rebuild_audit",
    "okto_pulse.core.kg.rebuild_confirmation",
    "okto_pulse.core.kg.rebuild_deterministic",
    "okto_pulse.core.kg.rebuild_generation",
    "okto_pulse.core.kg.rebuild_preflight",
    "okto_pulse.core.kg.rebuild_report",
    "okto_pulse.core.kg.rebuild_service",
    "okto_pulse.core.kg.rebuild_sources",
    "okto_pulse.core.kg.relational_projection",
    "okto_pulse.core.kg.rerank.factory",
    "okto_pulse.core.kg.rerank.token_overlap",
    "okto_pulse.core.kg.safe_write_lifecycle",
    "okto_pulse.core.kg.schema_contract",
    "okto_pulse.core.kg.schemas",
    "okto_pulse.core.kg.scoring",
    "okto_pulse.core.kg.search",
    "okto_pulse.core.kg.session_manager",
    "okto_pulse.core.kg.single_writer_lock",
    "okto_pulse.core.kg.stress_runner",
    "okto_pulse.core.kg.tier_power",
    "okto_pulse.core.kg.write_barrier",
    "okto_pulse.core.mcp",
    "okto_pulse.core.models",
    "okto_pulse.core.ports",
    "okto_pulse.core.repositories",
    "okto_pulse.core.repositories.interfaces",
    "okto_pulse.core.runtime_registry",
    "okto_pulse.core.services.amendment_revision",
    "okto_pulse.core.services.amendment_revision_api",
    "okto_pulse.core.services.analytics_contract",
    "okto_pulse.core.services.analytics_service",
    "okto_pulse.core.services.application_agents",
    "okto_pulse.core.services.application_kg",
    "okto_pulse.core.services.application_startup",
    "okto_pulse.core.services.ambiguity_assessment",
    "okto_pulse.core.services.architecture",
    "okto_pulse.core.services.architecture_observability",
    "okto_pulse.core.services.bug_regression_preview",
    "okto_pulse.core.services.cancellation",
    "okto_pulse.core.services.checklist",
    "okto_pulse.core.services.code_evidence",
    "okto_pulse.core.services.code_evidence_rebase",
    "okto_pulse.core.services.code_investigation",
    "okto_pulse.core.services.code_traceability_gate",
    "okto_pulse.core.services.cognitive_effectiveness_service",
    "okto_pulse.core.services.default_board_config_api",
    "okto_pulse.core.services.default_board_configuration",
    "okto_pulse.core.services.design_system",
    "okto_pulse.core.services.discovery_executor",
    "okto_pulse.core.services.discovery_selector_catalog",
    "okto_pulse.core.services.effective_resource_propagation",
    "okto_pulse.core.services.gate_contracts",
    "okto_pulse.core.services.governance_observability",
    "okto_pulse.core.services.kg_health_readiness_service",
    "okto_pulse.core.services.knowledge_propagation",
    "okto_pulse.core.services.implementation_targets",
    "okto_pulse.core.services.quality_assessment",
    "okto_pulse.core.services.quality_assessment_legacy_import",
    "okto_pulse.core.services.quality_assessment_lifecycle",
    "okto_pulse.core.services.quality_projection_currentness",
    "okto_pulse.core.services.reference_resolution",
    "okto_pulse.core.services.requirement_lint_assessment",
    "okto_pulse.core.services.requirement_lint_writer",
    "okto_pulse.core.services.research_decision_ledger",
    "okto_pulse.core.services.resource_gate_contracts",
    "okto_pulse.core.services.resource_gate",
    "okto_pulse.core.services.resource_lineage",
    "okto_pulse.core.services.ska_observability",
    "okto_pulse.core.services.spec_entity_canonicalization",
    "okto_pulse.core.services.spec_structured_entities",
    "okto_pulse.core.services.test_scenario_lifecycle",
    "okto_pulse.core.services.traceability",
    "okto_pulse.core.telemetry",
)

# Core owns the shipping manifest.  This expectation makes a Core/Community branch
# mismatch fail at import/CI time instead of silently widening or narrowing the
# allowlist; the effective Community allowlist is always built from the installed
# Core wheel plus the two edition-local surfaces below.
_COMMUNITY_LOCAL_CONTRACT_SURFACES = tuple(
    item
    for item in _CROSS_EDITION_CONTRACT_EXPECTATION
    if item.startswith("okto_pulse.community.")
)
_EXPECTED_CORE_CONTRACT_SURFACES = tuple(
    item
    for item in _CROSS_EDITION_CONTRACT_EXPECTATION
    if item.startswith("okto_pulse.core.")
)
if (
    len(_EXPECTED_CORE_CONTRACT_SURFACES)
    != len(CORE_PUBLIC_CORE_CONTRACT_SURFACES)
    or set(_EXPECTED_CORE_CONTRACT_SURFACES)
    != set(CORE_PUBLIC_CORE_CONTRACT_SURFACES)
):
    raise RuntimeError(
        "public_core_contract_manifest_mismatch: Community was built against a "
        "different Core public-contract manifest "
        f"(core_version={PUBLIC_CORE_CONTRACT_MANIFEST_VERSION}, "
        f"core_digest={PUBLIC_CORE_CONTRACT_MANIFEST_DIGEST})"
    )

PUBLIC_CORE_CONTRACT_SURFACES: tuple[str, ...] = (
    *_COMMUNITY_LOCAL_CONTRACT_SURFACES,
    *CORE_PUBLIC_CORE_CONTRACT_SURFACES,
)

PRIVATE_CORE_IMPLEMENTATION_SURFACES: tuple[str, ...] = (
    "okto_pulse.core.infra.database",
    "okto_pulse.core.kg.governance",
    "okto_pulse.core.kg.interfaces.get_kg_registry",
    "okto_pulse.core.kg.interfaces.registry",
    "okto_pulse.core.kg.workers",
    "okto_pulse.core.mcp.server",
    "okto_pulse.core.models.db",
    "okto_pulse.core.repositories.sqlalchemy",
    "okto_pulse.core.services.main",
    "okto_pulse.core.services.settings_service",
)


COMMUNITY_ADAPTER_PROVENANCE_REGISTRY: tuple[
    AdapterProvenanceRegistration, ...
] = (
    AdapterProvenanceRegistration(
        adapter_key="community_sqlalchemy_uow",
        owner="okto-pulse-community/relational",
        implementation_module=(
            "okto_pulse.community.adapters.sqlalchemy_unit_of_work"
        ),
        implementation_symbol="CommunityUnitOfWork",
        port_module="okto_pulse.core.repositories.interfaces.unit_of_work",
        port_symbol="PulseUnitOfWork",
        dependencies=("sqlalchemy", "aiosqlite"),
        contract_test="tests/test_r01b_unit_of_work_parity.py::test_ac2_unit_of_work_satisfies_ports",
    ),
    AdapterProvenanceRegistration(
        adapter_key="community_application_persistence",
        owner="okto-pulse-community/relational",
        implementation_module=(
            "okto_pulse.community.adapters.sqlalchemy_application_persistence"
        ),
        implementation_symbol="CommunitySqlAlchemyApplicationPersistence",
        port_module="okto_pulse.core.ports.application_persistence",
        port_symbol="ApplicationPersistencePort",
        dependencies=("sqlalchemy", "aiosqlite"),
        contract_test="tests/test_application_persistence_adapter.py::test_application_persistence_round_trip_includes_and_rollback",
    ),
    AdapterProvenanceRegistration(
        adapter_key="community_realm_access",
        owner="okto-pulse-community/relational",
        implementation_module="okto_pulse.community.adapters.sqlalchemy_realm_access",
        implementation_symbol="CommunitySqlAlchemyRealmAccess",
        port_module="okto_pulse.core.ports.realm_access",
        port_symbol="RealmAccessPort",
        dependencies=("sqlalchemy", "aiosqlite"),
        contract_test="tests/test_f03_realm_isolation.py::test_f03_two_realms_hide_known_ids_and_reject_cross_realm_writes",
    ),
    AdapterProvenanceRegistration(
        adapter_key="community_graph_transaction",
        owner="okto-pulse-community/graph",
        implementation_module="okto_pulse.community.adapters.kuzu_graph_transaction",
        implementation_symbol="CommunityKuzuGraphTransaction",
        port_module="okto_pulse.core.kg.interfaces.graph_transaction",
        port_symbol="GraphTransaction",
        dependencies=("ladybug",),
        contract_test="tests/test_f09_graph_adapters.py::test_f09_native_cursor_is_materialized_and_closed_in_community",
    ),
)


def audit_community_adapter_provenance(
    source_root: str | Path | None = None,
) -> dict[str, object]:
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[4]
    return audit_adapter_provenance(
        root,
        public_core_surfaces=PUBLIC_CORE_CONTRACT_SURFACES,
        private_core_surfaces=PRIVATE_CORE_IMPLEMENTATION_SURFACES,
        registrations=COMMUNITY_ADAPTER_PROVENANCE_REGISTRY,
        bridge_ledger=(),
        bridge_budget=0,
    )


__all__ = [
    "COMMUNITY_ADAPTER_PROVENANCE_REGISTRY",
    "PRIVATE_CORE_IMPLEMENTATION_SURFACES",
    "PUBLIC_CORE_CONTRACT_SURFACES",
    "audit_community_adapter_provenance",
]
