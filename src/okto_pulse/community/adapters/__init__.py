"""Lazy public adapter exports; importing the package does not compose a runtime."""

from importlib import import_module

_EXPORTS = {
    'CREATE_ALL_BOUNDARY_STEP_ID': 'okto_pulse.community.adapters.relational_schema_migrator',
    'CommunityCrossEncoderReranker': 'okto_pulse.community.adapters.rerank',
    'CommunityBoardSourceReader': 'okto_pulse.community.adapters.board_source_reader',
    'CommunityBoardRebuildIngestionAdapter': 'okto_pulse.community.adapters.board_rebuild_ingestion',
    'CommunityDataBootstrapper': 'okto_pulse.community.adapters.data_bootstrapper',
    'CommunityAgentAuthenticationGateway': 'okto_pulse.community.adapters.relational_application',
    'CommunityAmendmentRevisionApiBackend': 'okto_pulse.community.adapters.relational_application',
    'CommunityBoundaryCheckResult': 'okto_pulse.community.adapters.boundary_evidence',
    'CommunityContentIngestionResolver': 'okto_pulse.community.adapters.content_ingestion',
    'CommunityLocalLeaseProvider': 'okto_pulse.community.adapters.coordination',
    'CommunityLocalWriteLockPort': 'okto_pulse.community.adapters.coordination',
    'CommunitySqlAlchemyClaimRepository': 'okto_pulse.community.adapters.coordination',
    'CommunityFileSystemRebuildAuditArtifactStore': 'okto_pulse.community.adapters.rebuild_audit_storage',
    'CommunityRebuildAuditArtifactStoreResolver': 'okto_pulse.community.adapters.rebuild_audit_storage',
    'CommunityFileSystemStorage': 'okto_pulse.community.adapters.storage',
    'CommunityInMemoryCache': 'okto_pulse.community.adapters.memory',
    'CommunityInMemoryRateLimiter': 'okto_pulse.community.adapters.memory',
    'CommunityInMemorySessionStore': 'okto_pulse.community.adapters.memory',
    'CommunityKGEventsReader': 'okto_pulse.community.adapters.kg_events',
    'CommunityKGOperationalPorts': 'okto_pulse.community.adapters.kg_operational',
    'CommunityKgComposition': 'okto_pulse.community.adapters.composition',
    'CommunityMCPAuthContext': 'okto_pulse.community.adapters.mcp_auth',
    'CommunityApiKeySessionMiddleware': 'okto_pulse.community.adapters.mcp_host',
    'CommunityMcpHostProvider': 'okto_pulse.community.adapters.mcp_host',
    'CommunityMcpAuthenticator': 'okto_pulse.community.adapters.mcp_auth',
    'CommunityPermissionPresetGateway': 'okto_pulse.community.adapters.relational_application',
    'CommunityRelationalApplicationAdapter': 'okto_pulse.community.adapters.relational_application',
    'CommunityRelationalSchemaLifecycleOrchestrator': 'okto_pulse.community.adapters.relational_schema_lifecycle',
    'CommunityRelationalSchemaMigrator': 'okto_pulse.community.adapters.relational_schema_migrator',
    'CommunitySentenceTransformerProvider': 'okto_pulse.community.adapters.embedding',
    'CommunitySqlAlchemyKGOperationalReadModel': 'okto_pulse.community.adapters.kg_operational',
    'CommunitySqlAlchemyKGWorkerAudit': 'okto_pulse.community.adapters.kg_operational',
    'CommunitySqlAlchemyKGWorkerQueue': 'okto_pulse.community.adapters.kg_operational',
    'CommunityStubEmbeddingProvider': 'okto_pulse.community.adapters.embedding',
    'CommunityTelemetryStateCarrier': 'okto_pulse.community.adapters.telemetry_state',
    'CommunityTestEvidenceError': 'okto_pulse.community.adapters.test_evidence',
    'EvidenceMigrationReport': 'okto_pulse.community.adapters.test_evidence',
    'MCPAuthContext': 'okto_pulse.community.adapters.mcp_auth',
    'PersistedEvidenceMigrationReport': 'okto_pulse.community.adapters.test_evidence',
    'ProductExecutionObservation': 'okto_pulse.community.adapters.test_evidence',
    'SingletonSchedulerControl': 'okto_pulse.community.adapters.scheduler',
    'auth_context_from_session': 'okto_pulse.community.adapters.mcp_auth',
    'build_community_data_bootstrap_ledger': 'okto_pulse.community.adapters.data_bootstrapper',
    'build_community_boundary_evidence': 'okto_pulse.community.adapters.boundary_evidence',
    'build_community_runtime_smoke_evidence': 'okto_pulse.community.adapters.smoke_evidence',
    'build_community_mcp_asgi_app': 'okto_pulse.community.adapters.mcp_host',
    'build_community_kg_composition': 'okto_pulse.community.adapters.composition',
    'build_community_migration_ledger': 'okto_pulse.community.adapters.relational_schema_migrator',
    'build_community_telemetry_state_carrier': 'okto_pulse.community.adapters.telemetry_state',
    'community_storage_provider': 'okto_pulse.community.adapters.composition',
    'configure_community_kg_registry': 'okto_pulse.community.adapters.composition',
    'create_mcp_auth_factory': 'okto_pulse.community.adapters.mcp_auth',
    'make_community_data_bootstrapper': 'okto_pulse.community.adapters.data_bootstrapper',
    'make_community_mcp_authenticator': 'okto_pulse.community.adapters.mcp_auth',
    'migrate_persisted_test_scenario_evidence': 'okto_pulse.community.adapters.test_evidence',
    'migrate_test_scenario_evidence': 'okto_pulse.community.adapters.test_evidence',
    'normalize_test_scenario_evidence': 'okto_pulse.community.adapters.test_evidence',
    'register_community_mcp_host': 'okto_pulse.community.adapters.mcp_host',
    'principal_from_auth_session': 'okto_pulse.community.adapters.mcp_auth',
    'make_community_relational_schema_lifecycle_orchestrator': 'okto_pulse.community.adapters.relational_schema_lifecycle',
    'make_community_relational_schema_migrator': 'okto_pulse.community.adapters.relational_schema_migrator',
    'register_community_relational_schema_lifecycle': 'okto_pulse.community.adapters.relational_schema_lifecycle',
    'register_community_content_ingestion_resolver': 'okto_pulse.community.adapters.content_ingestion',
    'register_community_coordination_providers': 'okto_pulse.community.adapters.coordination',
    'register_community_kg_events_reader': 'okto_pulse.community.adapters.kg_events',
    'register_community_kg_operational_ports': 'okto_pulse.community.adapters.kg_operational',
    'register_community_reranker': 'okto_pulse.community.adapters.rerank',
    'register_community_telemetry_state_carrier': 'okto_pulse.community.adapters.telemetry_state',
    'resolve_pulse_db_path': 'okto_pulse.community.adapters.board_source_reader',
    'run_manifest_and_build_evidence_v2': 'okto_pulse.community.adapters.test_evidence',
    'verify_community_evidence_v2': 'okto_pulse.community.adapters.test_evidence',
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name]), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
