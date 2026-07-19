"""Community local-graph adapters for the hybrid search pipeline.

These provide the live implementations of `VectorSeedProvider` and
`GraphExpander`. They consume only the public graph/embedding ports and run
parametrised Cypher path queries for the expand step.

The adapters are NOT imported at module load — callers wire them up in
the MCP layer so unit tests can swap in stubs without touching Kùzu.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Iterable

from okto_pulse.core.kg.hybrid_search.hybrid import GraphNeighbor, VectorSeed
from okto_pulse.core.kg.interfaces.reflective_query import REFLECTIVE_DEFAULT_EDGES

logger = logging.getLogger("okto_pulse.community.hybrid_search")


class KuzuVectorSeedProvider:
    """HNSW-backed vector seed. Fans out across each requested node type,
    then merges by similarity DESC and truncates to top_k."""

    def __init__(
        self,
        *,
        graph_store: Any | None = None,
        embedding_provider: Any | None = None,
        min_similarity: float = 0.0,
    ) -> None:
        self._graph_store = graph_store
        self._embedding_provider = embedding_provider
        self.min_similarity = min_similarity

    def _providers(self) -> tuple[Any, Any]:
        graph_store = self._graph_store
        embedding = self._embedding_provider
        if graph_store is None or not callable(getattr(graph_store, "vector_search", None)):
            raise RuntimeError("reflective_graph_store_missing")
        if embedding is None or not callable(getattr(embedding, "encode", None)):
            raise RuntimeError("reflective_embedding_provider_missing")
        return graph_store, embedding

    def seed(
        self,
        *,
        board_id: str,
        query: str,
        node_types: tuple[str, ...],
        top_k: int,
        graph_layer: str = "all",
    ) -> list[VectorSeed]:
        graph_store, embedder = self._providers()
        vec = embedder.encode(query)
        combined: list[VectorSeed] = []
        per_type_cap = max(1, top_k)
        for ntype in node_types:
            try:
                rows = graph_store.vector_search(
                    board_id,
                    ntype,
                    vec,
                    per_type_cap,
                    self.min_similarity,
                    graph_layer=graph_layer,
                )
            except Exception as exc:  # noqa: BLE001 — log and skip this type
                logger.warning(
                    "hybrid_search.seed_failed board=%s type=%s err=%s",
                    board_id, ntype, exc,
                )
                continue
            for r in rows:
                if isinstance(r, Mapping):
                    node_id = r.get("node_id") or r.get("graph_node_id")
                    node_type = r.get("node_type") or ntype
                    title = r.get("title") or ""
                    similarity = r.get("similarity", 0.0)
                else:
                    node_id = getattr(r, "node_id", None) or getattr(
                        r, "graph_node_id", None
                    )
                    node_type = getattr(r, "node_type", ntype)
                    title = getattr(r, "title", "")
                    similarity = getattr(r, "similarity", 0.0)
                if not node_id:
                    continue
                combined.append(VectorSeed(
                    node_id=str(node_id),
                    node_type=str(node_type),
                    title=str(title),
                    similarity=float(similarity),
                ))
        best: dict[str, VectorSeed] = {}
        for item in combined:
            current = best.get(item.node_id)
            if current is None or item.similarity > current.similarity:
                best[item.node_id] = item
        return sorted(
            best.values(),
            key=lambda s: (-s.similarity, s.node_type, s.node_id),
        )[:top_k]


class KuzuGraphExpander:
    """Runs bounded variable-length Cypher queries for an intent's edge
    set. One query per edge type keeps the plan simple and lets Kùzu use
    the right relationship index."""

    def __init__(self, executor) -> None:
        if executor is None:
            raise ValueError("cypher_executor_required")
        self._executor = executor

    def expand(
        self,
        *,
        board_id: str,
        seed_ids: tuple[str, ...],
        edges: tuple[str, ...],
        max_hops: int,
        graph_layer: str = "all",
    ) -> list[GraphNeighbor]:
        if not seed_ids or not edges:
            return []
        invalid = sorted(set(edges) - set(REFLECTIVE_DEFAULT_EDGES))
        if invalid:
            raise ValueError(f"reflective_edge_not_allowed:{','.join(invalid)}")
        if not 1 <= int(max_hops) <= 3:
            raise ValueError("reflective_max_hops_out_of_range")
        if graph_layer not in {"canonical", "working", "all"}:
            raise ValueError("invalid_graph_layer")
        out: list[GraphNeighbor] = []
        seen: set[tuple[str, str]] = set()  # (node_id, edge_type)
        for edge in edges:
            rows = self._expand_one_edge(
                self._executor, board_id, seed_ids, edge, max_hops, graph_layer,
            )
            for row in rows:
                key = (row.node_id, edge)
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
        return out

    @staticmethod
    def _expand_one_edge(
        executor,
        board_id: str,
        seed_ids: Iterable[str],
        edge: str,
        max_hops: int,
        graph_layer: str = "all",
    ) -> list[GraphNeighbor]:
        """Run one parametrised path query. Returns one row per distinct
        reachable node, carrying `min` path length as hop_distance."""
        if not 1 <= int(max_hops) <= 3:
            return []
        seed_list = list(seed_ids)
        # Kùzu bounded variable-length path. We filter by seed and return
        # distinct destination nodes with the shortest path length.
        stmt = (
            "MATCH p=(src)-[r:" + edge + f"*1..{max_hops}]->(dst) "
            "WHERE src.id IN $seed_ids "
            "AND ($graph_layer = 'all' OR dst.graph_layer = $graph_layer) "
            "RETURN dst.id AS node_id, "
            "       label(dst) AS node_type, "
            "       dst.title AS title, "
            "       length(p) AS hop_distance, "
            "       0.7 AS edge_confidence "
            "ORDER BY hop_distance ASC, edge_confidence DESC"
        )
        params = {"seed_ids": seed_list, "graph_layer": graph_layer}
        results: list[GraphNeighbor] = []
        try:
            query_result = executor.execute_read_only(
                board_id, stmt, params, max_rows=1000,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hybrid_search.expand_failed edge=%s err=%s", edge, exc,
            )
            return []
        for row in query_result.get("rows", []):
            node_id, node_type, title, hop_distance, edge_conf = row
            results.append(GraphNeighbor(
                node_id=str(node_id),
                node_type=str(node_type),
                title=str(title or ""),
                edge_type=edge,
                edge_confidence=float(edge_conf or 0.7),
                hop_distance=int(hop_distance or 1),
            ))
        return results
