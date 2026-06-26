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
    CommunityMcpAuthenticator,
    make_community_mcp_authenticator,
)
from .memory import (
    CommunityInMemoryCache,
    CommunityInMemoryRateLimiter,
    CommunityInMemorySessionStore,
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
from .storage import CommunityFileSystemStorage

__all__ = [
    "CREATE_ALL_BOUNDARY_STEP_ID",
    "CommunityCrossEncoderReranker",
    "CommunityDataBootstrapper",
    "CommunityFileSystemStorage",
    "CommunityInMemoryCache",
    "CommunityInMemoryRateLimiter",
    "CommunityInMemorySessionStore",
    "CommunityKgComposition",
    "CommunityMcpAuthenticator",
    "CommunityRelationalSchemaMigrator",
    "CommunitySentenceTransformerProvider",
    "CommunityStubEmbeddingProvider",
    "build_community_data_bootstrap_ledger",
    "build_community_kg_composition",
    "build_community_migration_ledger",
    "community_storage_provider",
    "configure_community_kg_registry",
    "make_community_data_bootstrapper",
    "make_community_mcp_authenticator",
    "make_community_relational_schema_migrator",
    "register_community_reranker",
]
