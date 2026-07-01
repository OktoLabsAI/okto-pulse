"""Community KG graph adapters.

The Community edition owns the concrete Kuzu/Ladybug implementations for the
graph ports. Core code consumes only the port protocols and registry slots.
"""

from __future__ import annotations

from typing import Any

from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.kuzu_cypher_executor import (
    CommunityKuzuCypherExecutor,
)
from okto_pulse.community.adapters.kuzu_graph_lifecycle import (
    CommunityKuzuGraphLifecycle,
)
from okto_pulse.community.adapters.kuzu_graph_path_resolver import (
    CommunityKuzuGraphPathResolver,
)
from okto_pulse.community.adapters.kuzu_graph_schema_manager import (
    CommunityKuzuGraphSchemaManager,
)
from okto_pulse.community.adapters.kuzu_graph_store import (
    CommunityKuzuGraphStore,
)
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    CommunityKuzuGraphTransaction,
)


def build_community_graph_providers() -> dict[str, Any]:
    """Build the six Community graph providers as a registry-slot dict."""
    return {
        "graph_store": CommunityKuzuGraphStore(),
        "cypher_executor": CommunityKuzuCypherExecutor(),
        "graph_transaction": CommunityKuzuGraphTransaction(),
        "graph_schema_manager": CommunityKuzuGraphSchemaManager(),
        "graph_lifecycle": CommunityKuzuGraphLifecycle(),
        "graph_path_resolver": CommunityKuzuGraphPathResolver(),
        "global_discovery_runtime": CommunityGlobalDiscoveryRuntime(),
    }


__all__ = [
    "CommunityKuzuGraphStore",
    "CommunityKuzuCypherExecutor",
    "CommunityKuzuGraphTransaction",
    "CommunityKuzuGraphSchemaManager",
    "CommunityKuzuGraphLifecycle",
    "CommunityKuzuGraphPathResolver",
    "CommunityGlobalDiscoveryRuntime",
    "build_community_graph_providers",
]
