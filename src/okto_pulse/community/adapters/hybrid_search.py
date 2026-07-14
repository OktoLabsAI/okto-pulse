"""Community local-graph adapters for the hybrid search pipeline.

These provide the live implementations of `VectorSeedProvider` and
`GraphExpander`. They reuse the existing `kg.search.find_similar_nodes_by_type`
HNSW wrapper for the seed step and run parametrised Cypher path queries
for the expand step.

The adapters are NOT imported at module load — callers wire them up in
the MCP layer so unit tests can swap in stubs without touching Kùzu.
"""

from __future__ import annotations

import logging
from typing import Iterable

from okto_pulse.core.kg.embedding import get_embedding_provider
from okto_pulse.core.kg.search import find_similar_nodes_by_type

from okto_pulse.core.kg.hybrid_search.hybrid import GraphNeighbor, VectorSeed

logger = logging.getLogger("okto_pulse.community.hybrid_search")


class KuzuVectorSeedProvider:
    """HNSW-backed vector seed. Fans out across each requested node type,
    then merges by similarity DESC and truncates to top_k."""

    def __init__(self, *, min_similarity: float = 0.0) -> None:
        self.min_similarity = min_similarity

    def seed(
        self,
        *,
        board_id: str,
        query: str,
        node_types: tuple[str, ...],
        top_k: int,
    ) -> list[VectorSeed]:
        embedder = get_embedding_provider()
        vec = embedder.encode(query)
        combined: list[VectorSeed] = []
        per_type_cap = max(1, top_k)
        for ntype in node_types:
            try:
                rows = find_similar_nodes_by_type(
                    board_id=board_id,
                    node_type=ntype,
                    query_vector=vec,
                    top_k=per_type_cap,
                    min_similarity=self.min_similarity,
                )
            except Exception as exc:  # noqa: BLE001 — log and skip this type
                logger.warning(
                    "hybrid_search.seed_failed board=%s type=%s err=%s",
                    board_id, ntype, exc,
                )
                continue
            for r in rows:
                combined.append(VectorSeed(
                    node_id=r.graph_node_id,
                    node_type=r.node_type,
                    title=r.title,
                    similarity=float(r.similarity),
                ))
        combined.sort(key=lambda s: s.similarity, reverse=True)
        return combined[:top_k]


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
    ) -> list[GraphNeighbor]:
        if not seed_ids or not edges:
            return []
        out: list[GraphNeighbor] = []
        seen: set[tuple[str, str]] = set()  # (node_id, edge_type)
        for edge in edges:
            rows = self._expand_one_edge(
                self._executor, board_id, seed_ids, edge, max_hops,
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
    ) -> list[GraphNeighbor]:
        """Run one parametrised path query. Returns one row per distinct
        reachable node, carrying `min` path length as hop_distance."""
        if max_hops <= 0:
            return []
        seed_list = list(seed_ids)
        # Kùzu bounded variable-length path. We filter by seed and return
        # distinct destination nodes with the shortest path length.
        stmt = (
            "MATCH p=(src {id: $seed})-[r:" + edge + f"*1..{max_hops}]->(dst) "
            "WHERE src.id IN $seed_ids "
            "RETURN DISTINCT dst.id AS node_id, "
            "       label(dst) AS node_type, "
            "       dst.title AS title, "
            "       min(length(p)) AS hop_distance, "
            "       max(r[size(r)-1].confidence) AS edge_confidence"
        )
        params = {"seed": None, "seed_ids": seed_list}
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
