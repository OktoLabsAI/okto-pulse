"""Community KG graph adapters (spec R05-C, Onda C / IMP1).

The Community-edition adapters for the six spec-#06 KG ports
(``SemanticGraphStore`` / ``CypherExecutor`` / ``GraphTransaction`` /
``GraphSchemaManager`` / ``GraphLifecycle`` / ``GraphPathResolver``).

R05-C constraint (LEDGERED EXCEPTION): the embedded Kùzu/Ladybug runtime STAYS
in the core as a temporary ledgered exception — the ~4974 core tests resolve
``get_kg_registry()`` -> ``_build_defaults`` -> ``_build_graph_defaults`` ->
``KuzuGraphStore`` etc., so the core embedded MUST NOT be removed here. The
physical code move + Ladybug/asyncpg dependency cleanup is R05-E.

Therefore these adapters MIRROR the embedded BIT-FOR-BIT by SUBCLASSING them:
the sync-under-async-port contract, any internal Kùzu locking, the lazy Ladybug
import (the embedded import ``kg.schema`` which lazy-loads ``ladybug``), and the
structured health/recovery/rebuild errors are all preserved exactly (a subclass
with no overrides IS the same behaviour, with zero transcription risk on the
highest-risk graph-DB layer). The Community edition REGISTERS these via the
composition root; ``graph.lbug`` / ``discovery.lbug`` path ownership is
Community-local (the path resolver resolves to the same ``board_kuzu_path`` so
existing data is untouched).

Import note: this module is imported LAZILY by the composition helper (never at
the adapters package top), so importing ``okto_pulse.community`` does not eager
-load Ladybug — it loads only when the KG registry is actually configured, the
same point the core already loaded it.
"""

from __future__ import annotations

from typing import Any

from okto_pulse.core.kg.providers.embedded.kuzu_cypher_executor import (
    KuzuCypherExecutor,
)
from okto_pulse.core.kg.providers.embedded.kuzu_graph_lifecycle import (
    KuzuGraphLifecycle,
)
from okto_pulse.core.kg.providers.embedded.kuzu_graph_path_resolver import (
    KuzuGraphPathResolver,
)
from okto_pulse.core.kg.providers.embedded.kuzu_graph_schema_manager import (
    KuzuGraphSchemaManager,
)
from okto_pulse.core.kg.providers.embedded.kuzu_graph_store import KuzuGraphStore
from okto_pulse.core.kg.providers.embedded.kuzu_graph_transaction import (
    KuzuGraphTransaction,
)


class CommunityKuzuGraphStore(KuzuGraphStore):
    """SemanticGraphStore (Community) — mirrors the embedded bit-for-bit."""


class CommunityKuzuCypherExecutor(KuzuCypherExecutor):
    """CypherExecutor (Community) — mirrors the embedded bit-for-bit."""


class CommunityKuzuGraphTransaction(KuzuGraphTransaction):
    """GraphTransaction (Community) — mirrors the embedded bit-for-bit."""


class CommunityKuzuGraphSchemaManager(KuzuGraphSchemaManager):
    """GraphSchemaManager (Community) — mirrors the embedded bit-for-bit."""


class CommunityKuzuGraphLifecycle(KuzuGraphLifecycle):
    """GraphLifecycle (Community) — mirrors the embedded bit-for-bit."""


class CommunityKuzuGraphPathResolver(KuzuGraphPathResolver):
    """GraphPathResolver (Community) — graph.lbug/discovery.lbug ownership is
    Community-local; resolves the SAME path as the embedded (data untouched)."""


def build_community_graph_providers() -> dict[str, Any]:
    """Build the six Community graph providers as a registry-slot dict."""
    return {
        "graph_store": CommunityKuzuGraphStore(),
        "cypher_executor": CommunityKuzuCypherExecutor(),
        "graph_transaction": CommunityKuzuGraphTransaction(),
        "graph_schema_manager": CommunityKuzuGraphSchemaManager(),
        "graph_lifecycle": CommunityKuzuGraphLifecycle(),
        "graph_path_resolver": CommunityKuzuGraphPathResolver(),
    }


__all__ = [
    "CommunityKuzuGraphStore",
    "CommunityKuzuCypherExecutor",
    "CommunityKuzuGraphTransaction",
    "CommunityKuzuGraphSchemaManager",
    "CommunityKuzuGraphLifecycle",
    "CommunityKuzuGraphPathResolver",
    "build_community_graph_providers",
]
