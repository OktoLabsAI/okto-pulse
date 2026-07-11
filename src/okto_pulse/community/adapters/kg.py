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
from okto_pulse.community.adapters.kuzu_graph_runtime_store import (
    CommunityKuzuGraphRuntimeStore,
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
from okto_pulse.community.adapters.kg_wal_recovery import (
    CommunityGraphRecovery,
)


def build_community_graph_providers() -> dict[str, Any]:
    """Build the Community graph providers as a registry-slot dict."""
    return {
        "graph_store": CommunityKuzuGraphStore(),
        "cypher_executor": CommunityKuzuCypherExecutor(),
        "graph_transaction": CommunityKuzuGraphTransaction(),
        "graph_schema_manager": CommunityKuzuGraphSchemaManager(),
        "graph_lifecycle": CommunityKuzuGraphLifecycle(),
        "graph_runtime_store": CommunityKuzuGraphRuntimeStore(),
        "global_discovery_runtime": CommunityGlobalDiscoveryRuntime(),
        # KGD-01 FR3/BR2 — wal-only recovery port (degrau 2 da escada).
        # Slot opcional no registry (read-time fail-closed via
        # require_graph_recovery), mesmo contrato do quarantine_restore.
        "graph_recovery": CommunityGraphRecovery(),
    }


__all__ = [
    "CommunityKuzuGraphStore",
    "CommunityKuzuCypherExecutor",
    "CommunityKuzuGraphTransaction",
    "CommunityKuzuGraphSchemaManager",
    "CommunityKuzuGraphLifecycle",
    "CommunityKuzuGraphRuntimeStore",
    "CommunityGlobalDiscoveryRuntime",
    "CommunityGraphRecovery",
    "build_community_graph_providers",
]
