"""Community-edition adapters for the core runtime ports (spec #15 / #16).

Concrete implementations of the pure ``okto_pulse.core.ports`` Protocols live
here. Importing this package is import-light: the adapter modules pull only the
``core.ports`` contract at module top and lazy-import their concrete
dependencies (engine, ``infra.database``) inside their composition factories,
so ``core`` never imports ``community``.
"""

from __future__ import annotations

from .composition import (
    CommunityKgComposition,
    build_community_kg_composition,
    community_storage_provider,
    configure_community_kg_registry,
)
from .board_rebuild_ingestion import CommunityBoardRebuildIngestionAdapter
from .board_source_reader import CommunityBoardSourceReader, resolve_pulse_db_path
from .boundary_evidence import (
    CommunityBoundaryCheckResult,
    build_community_boundary_evidence,
)
from .smoke_evidence import (
    build_community_runtime_smoke_evidence,
)
from .data_bootstrapper import (
    CommunityDataBootstrapper,
    build_community_data_bootstrap_ledger,
    make_community_data_bootstrapper,
)
from .embedding import (
    CommunitySentenceTransformerProvider,
    CommunityStubEmbeddingProvider,
)
from .mcp_auth import (
    CommunityMCPAuthContext,
    CommunityMcpAuthenticator,
    MCPAuthContext,
    auth_context_from_session,
    create_mcp_auth_factory,
    make_community_mcp_authenticator,
)
from .memory import (
    CommunityInMemoryCache,
    CommunityInMemoryRateLimiter,
    CommunityInMemorySessionStore,
)
from .relational_schema_lifecycle import (
    CommunityRelationalSchemaLifecycleOrchestrator,
    make_community_relational_schema_lifecycle_orchestrator,
    register_community_relational_schema_lifecycle,
)
from .relational_schema_migrator import (
    CREATE_ALL_BOUNDARY_STEP_ID,
    CommunityRelationalSchemaMigrator,
    build_community_migration_ledger,
    make_community_relational_schema_migrator,
)
from .rerank import (
    CommunityCrossEncoderReranker,
    register_community_reranker,
)
from .scheduler import SingletonSchedulerControl
from .storage import CommunityFileSystemStorage
from .telemetry_state import (
    CommunityTelemetryStateCarrier,
    build_community_telemetry_state_carrier,
    register_community_telemetry_state_carrier,
)

__all__ = [
    "CREATE_ALL_BOUNDARY_STEP_ID",
    "CommunityCrossEncoderReranker",
    "CommunityBoardSourceReader",
    "CommunityBoardRebuildIngestionAdapter",
    "CommunityDataBootstrapper",
    "CommunityBoundaryCheckResult",
    "CommunityFileSystemStorage",
    "CommunityInMemoryCache",
    "CommunityInMemoryRateLimiter",
    "CommunityInMemorySessionStore",
    "CommunityKgComposition",
    "CommunityMCPAuthContext",
    "CommunityMcpAuthenticator",
    "CommunityRelationalSchemaLifecycleOrchestrator",
    "CommunityRelationalSchemaMigrator",
    "CommunitySentenceTransformerProvider",
    "CommunityStubEmbeddingProvider",
    "CommunityTelemetryStateCarrier",
    "MCPAuthContext",
    "SingletonSchedulerControl",
    "auth_context_from_session",
    "build_community_data_bootstrap_ledger",
    "build_community_boundary_evidence",
    "build_community_runtime_smoke_evidence",
    "build_community_kg_composition",
    "build_community_migration_ledger",
    "build_community_telemetry_state_carrier",
    "community_storage_provider",
    "configure_community_kg_registry",
    "create_mcp_auth_factory",
    "make_community_data_bootstrapper",
    "make_community_mcp_authenticator",
    "make_community_relational_schema_lifecycle_orchestrator",
    "make_community_relational_schema_migrator",
    "register_community_relational_schema_lifecycle",
    "register_community_reranker",
    "register_community_telemetry_state_carrier",
    "resolve_pulse_db_path",
]
