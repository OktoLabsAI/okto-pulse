"""Community local-graph adapters for the hybrid search pipeline.

These provide the live implementations of `VectorSeedProvider` and
`GraphExpander`. They consume only the public graph/embedding ports and run
parametrised Cypher path queries for the expand step.

The adapters are NOT imported at module load — callers wire them up in
the MCP layer so unit tests can swap in stubs without opening a database.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Iterable

from okto_pulse.core.kg.hybrid_search.hybrid import GraphNeighbor, VectorSeed
from okto_pulse.core.kg.interfaces.reflective_query import REFLECTIVE_DEFAULT_EDGES

logger = logging.getLogger("okto_pulse.community.hybrid_search")


class CommunityVectorSeedProvider:
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
        if graph_store is None or not callable(
            getattr(graph_store, "vector_search", None)
        ):
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
                    board_id,
                    ntype,
                    exc,
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
                combined.append(
                    VectorSeed(
                        node_id=str(node_id),
                        node_type=str(node_type),
                        title=str(title),
                        similarity=float(similarity),
                    )
                )
        best: dict[str, VectorSeed] = {}
        for item in combined:
            current = best.get(item.node_id)
            if current is None or item.similarity > current.similarity:
                best[item.node_id] = item
        return sorted(
            best.values(),
            key=lambda s: (-s.similarity, s.node_type, s.node_id),
        )[:top_k]


class CommunityGraphExpander:
    """Expand typed endpoint paths, preserving shortest-hop reachability.

    Community's schema supplies the finite endpoint alternatives. Batched
    readers evaluate these alternatives in one snapshot without untyped paths.
    """

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
                self._executor,
                board_id,
                seed_ids,
                edge,
                max_hops,
                graph_layer,
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
        from okto_pulse.community.adapters.grafx_relationship_layout import (
            PULSE_RELATIONSHIP_LAYOUT,
        )

        layouts = tuple(
            entry
            for entry in PULSE_RELATIONSHIP_LAYOUT.entries
            if entry.logical_type == edge
        )
        paths = [(entry,) for entry in layouts]
        params = {"seed_ids": seed_list, "graph_layer": graph_layer}
        statements = []
        for hops in range(1, max_hops + 1):
            for path in paths:
                pattern = f"(src:{path[0].from_type})"
                for position, entry in enumerate(path):
                    name = "dst" if position == hops - 1 else f"step{position}"
                    pattern += f"-[:{edge}]->({name}:{entry.to_type})"
                statement = (
                    f"MATCH {pattern} WHERE src.id IN $seed_ids "
                    "AND ($graph_layer = 'all' OR dst.graph_layer = $graph_layer) "
                    "RETURN DISTINCT dst.id AS node_id, "
                    f"'{path[-1].to_type}' AS node_type, dst.title AS title, "
                    f"{hops} AS hop_distance, 0.7 AS edge_confidence "
                    "ORDER BY node_type ASC, node_id ASC"
                )
                statements.append((statement, params, 1000))
            paths = [
                (*path, entry)
                for path in paths
                for entry in layouts
                if path[-1].to_type == entry.from_type
            ]
        results: list[GraphNeighbor] = []
        try:
            batch = getattr(executor, "execute_read_only_batch", None)
            if callable(batch):
                envelopes = batch(board_id, statements)
            else:
                envelopes = [
                    executor.execute_read_only(
                        board_id, statement, parameters, max_rows=limit
                    )
                    for statement, parameters, limit in statements
                ]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hybrid_search.expand_failed edge=%s err=%s",
                edge,
                exc,
            )
            return []
        rows = [row for envelope in envelopes for row in envelope.get("rows", [])]
        rows.sort(key=lambda row: (int(row[3] or 1), str(row[1]), str(row[0])))
        seen = set()
        for row in rows:
            node_id, node_type, title, hop_distance, edge_conf = row
            if str(node_id) in seen:
                continue
            seen.add(str(node_id))
            results.append(
                GraphNeighbor(
                    node_id=str(node_id),
                    node_type=str(node_type),
                    title=str(title or ""),
                    edge_type=edge,
                    edge_confidence=float(edge_conf or 0.7),
                    hop_distance=int(hop_distance or 1),
                )
            )
            if len(results) == 1000:
                break
        return results
