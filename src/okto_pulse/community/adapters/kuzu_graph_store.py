"""KuzuGraphStore — satisfies SemanticGraphStore Protocol for embedded Kuzu.

Delegates all graph operations to per-board Kuzu databases via
`open_board_connection()` and parametrized Cypher templates. This is the
production default for single-node / community deployments.
"""

from __future__ import annotations

import logging
from typing import Any

from okto_pulse.core.kg.interfaces.graph_store import QueryFilters
from okto_pulse.community.adapters.kg_runtime import (
    MULTI_REL_TYPES,
    NODE_TYPES,
    REL_TYPES,
    SCHEMA_VERSION,
    VECTOR_INDEX_TYPES,
    bootstrap_board_graph,
    open_board_connection,
    resolve_relationship_endpoint_pair,
    stable_rel_type_entries,
    vector_index_name,
)
from okto_pulse.core.kg import cypher_templates as tpl

logger = logging.getLogger("okto_pulse.kg.kuzu_graph_store")


class CommunityKuzuGraphStore:
    """Embedded Kuzu implementation of SemanticGraphStore."""

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def bootstrap(self, board_id: str) -> None:
        bootstrap_board_graph(board_id)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create_node(
        self, board_id: str, node_type: str, node_id: str, attrs: dict[str, Any]
    ) -> None:
        with open_board_connection(board_id) as (_db, conn):
            params = dict(attrs)
            params["id"] = node_id
            columns = ", ".join(f"{k}: ${k}" for k in params)
            conn.execute(f"CREATE (n:{node_type} {{{columns}}})", params)

    def create_edge(
        self, board_id: str, edge_type: str, from_id: str, to_id: str,
        attrs: dict[str, Any] | None = None,
        *,
        from_type: str | None = None,
        to_type: str | None = None,
    ) -> None:
        from_type, to_type = resolve_relationship_endpoint_pair(
            edge_type,
            from_type=from_type,
            to_type=to_type,
        )

        edge_attrs = dict(attrs or {})
        edge_attrs.setdefault("confidence", 0.7)
        # v0.2.0 provenance defaults. The store can't tell cognitive vs
        # deterministic writes; callers MUST override `layer` explicitly for
        # Layer 1 emissions. If missing, we tag as "cognitive" so the
        # layer_isolation gate still lights up on orphan writes.
        edge_attrs.setdefault("layer", "cognitive")
        edge_attrs.setdefault("rule_id", "")
        edge_attrs.setdefault("created_by", edge_attrs.get("created_by_session_id", ""))
        edge_attrs.setdefault("fallback_reason", "")

        attr_cols = ", ".join(f"{k}: ${k}" for k in edge_attrs)
        stmt = (
            f"MATCH (a:{from_type} {{id: $from_id}}), "
            f"(b:{to_type} {{id: $to_id}}) "
            f"CREATE (a)-[r:{edge_type} {{{attr_cols}}}]->(b)"
        )
        params = dict(edge_attrs)
        params["from_id"] = from_id
        params["to_id"] = to_id

        with open_board_connection(board_id) as (_db, conn):
            conn.execute(stmt, params)

    def delete_nodes_by_session(self, board_id: str, session_id: str) -> int:
        count = 0
        with open_board_connection(board_id) as (_db, conn):
            for node_type in NODE_TYPES:
                try:
                    result = conn.execute(
                        f"MATCH (n:{node_type}) "
                        f"WHERE n.source_session_id = $sid "
                        f"RETURN count(n)",
                        {"sid": session_id},
                    )
                    if result.has_next():
                        count += result.get_next()[0]
                    conn.execute(
                        f"MATCH (n:{node_type}) "
                        f"WHERE n.source_session_id = $sid DETACH DELETE n",
                        {"sid": session_id},
                    )
                except Exception as exc:
                    logger.warning(
                        "delete_nodes_by_session failed type=%s err=%s",
                        node_type, exc,
                    )
        return count

    def delete_edges_by_session(self, board_id: str, session_id: str) -> int:
        count = 0
        with open_board_connection(board_id) as (_db, conn):
            rel_pairs = list(REL_TYPES)
            for rel_name, endpoint_pairs in MULTI_REL_TYPES:
                rel_pairs.extend(
                    (rel_name, from_type, to_type)
                    for from_type, to_type in endpoint_pairs
                )
            for rel_name, from_type, to_type in rel_pairs:
                try:
                    result = conn.execute(
                        f"MATCH (a:{from_type})-[r:{rel_name}]->(b:{to_type}) "
                        f"WHERE r.created_by_session_id = $sid "
                        f"RETURN count(r)",
                        {"sid": session_id},
                    )
                    if result.has_next():
                        count += result.get_next()[0]
                    conn.execute(
                        f"MATCH (a:{from_type})-[r:{rel_name}]->(b:{to_type}) "
                        f"WHERE r.created_by_session_id = $sid DELETE r",
                        {"sid": session_id},
                    )
                except Exception as exc:
                    logger.warning(
                        "delete_edges_by_session failed rel=%s err=%s",
                        rel_name, exc,
                    )
        return count

    # ------------------------------------------------------------------
    # Read operations (tier primario)
    # ------------------------------------------------------------------

    def _exec(self, board_id: str, cypher: str, params: dict[str, Any]) -> list[list]:
        with open_board_connection(board_id) as (_db, conn):
            result = conn.execute(cypher, params)
            rows = []
            while result.has_next():
                rows.append(result.get_next())
            return rows

    def find_by_topic(
        self, board_id: str, node_type: str, topic: str, filters: QueryFilters
    ) -> list[list]:
        # v0.3.0 R3: replaced the dropped validation_status filter with
        # the relevance_score threshold (default 0.3 — below the neutral
        # 0.5 so freshly created nodes still pass). Ranking order uses
        # relevance_score DESC so hot nodes bubble to the top.
        #
        # v0.3.2 (spec 20f67c2a — Ideação #5): the Cypher ORDER BY clause is
        # preserved (BR4) — but we now over-fetch a pool of
        # max_rows * DECAY_REORDER_POOL_MULTIPLIER and run
        # _apply_decay_reorder in Python so stale-but-high-scoring nodes
        # don't outrank fresh-and-active ones. We additionally select
        # query_hits and last_queried_at to feed the reorder helper.
        from okto_pulse.core.kg.scoring import (
            DECAY_REORDER_POOL_MULTIPLIER,
            _apply_decay_reorder,
        )

        pool_size = max(filters.max_rows, filters.max_rows * DECAY_REORDER_POOL_MULTIPLIER)
        cypher = (
            f"MATCH (n:{node_type}) "
            f"WHERE n.title CONTAINS $topic "
            f"AND n.source_confidence >= $min_confidence "
            f"AND n.relevance_score >= $min_relevance "
            f"RETURN n.id, n.title, n.content, n.created_at, n.source_confidence, "
            f"n.relevance_score, n.superseded_by, n.query_hits, n.last_queried_at "
            f"ORDER BY n.relevance_score DESC, n.created_at DESC "
            f"LIMIT $max_rows"
        )
        rows = self._exec(board_id, cypher, {
            "topic": topic,
            "min_confidence": filters.min_confidence,
            "min_relevance": filters.min_relevance,
            "max_rows": pool_size,
        })
        if not rows:
            return rows

        # Map Kùzu rows to dicts for the reorder helper, then back to the
        # 7-column legacy row shape that downstream callers expect.
        enriched = [
            {
                "node_id": r[0],
                "title": r[1],
                "content": r[2],
                "created_at": r[3],
                "source_confidence": r[4],
                "relevance_score": r[5],
                "superseded_by": r[6],
                "query_hits": r[7] or 0,
                "last_queried_at": r[8],
            }
            for r in rows
        ]
        reordered = _apply_decay_reorder(enriched, filters.max_rows)
        return [
            [
                row["node_id"],
                row["title"],
                row["content"],
                row["created_at"],
                row["source_confidence"],
                row["relevance_score"],
                row["superseded_by"],
            ]
            for row in reordered
        ]

    def find_by_topic_semantic(
        self,
        board_id: str,
        node_type: str,
        query_vec: list[float],
        filters: QueryFilters,
        min_similarity: float = 0.3,
    ) -> list[list]:
        """Vector-search over a node type, reshaped into the find_by_topic row
        shape so ``get_decision_history`` can merge semantic + title hits.

        Uses the existing HNSW index via ``find_similar_nodes_by_type``; pulls
        full node attributes (content, created_at, source_confidence,
        relevance_score, superseded_by) for each hit. Returns empty list when
        the index is absent or the query fails — caller falls back to
        title-CONTAINS.
        """
        from okto_pulse.core.kg.search import find_similar_nodes_by_type

        with open_board_connection(board_id) as (_db, conn):
            raw = find_similar_nodes_by_type(
                board_id=board_id,
                node_type=node_type,
                query_vector=query_vec,
                top_k=filters.max_rows,
                min_similarity=min_similarity,
                conn=conn,
            )
        if not raw:
            return []

        ids_in_order = [r.kuzu_node_id for r in raw]
        attrs = self._exec(
            board_id,
            f"MATCH (n:{node_type}) WHERE n.id IN $ids "
            f"AND n.source_confidence >= $min_confidence "
            f"AND n.relevance_score >= $min_relevance "
            f"RETURN n.id, n.title, n.content, n.created_at, "
            f"n.source_confidence, n.relevance_score, n.superseded_by",
            {
                "ids": ids_in_order,
                "min_confidence": filters.min_confidence,
                "min_relevance": filters.min_relevance,
            },
        )
        # Normalize created_at → ISO string so the row shape matches
        # find_by_topic (which gets its value through a Cypher template
        # that Kùzu already serializes via default=str in the MCP wrapper).
        # Without this, Pydantic response models that declare created_at as
        # `str` reject the raw datetime.
        normalized = []
        for row in attrs:
            r = list(row)
            if len(r) > 3 and hasattr(r[3], "isoformat"):
                r[3] = r[3].isoformat()
            normalized.append(r)
        by_id = {row[0]: row for row in normalized}
        return [by_id[nid] for nid in ids_in_order if nid in by_id]

    def find_by_artifact(
        self, board_id: str, artifact_id: str, filters: QueryFilters,
        *, graph_layer: str = "all",
    ) -> list[list]:
        return self._exec(board_id, tpl.GET_RELATED_CONTEXT, {
            "artifact_id": artifact_id,
            "min_confidence": filters.min_confidence,
            "max_rows": filters.max_rows,
            "graph_layer": graph_layer,
        })

    def find_by_artifact_filtered(
        self,
        board_id: str,
        artifact_id: str,
        filters: QueryFilters,
        *,
        rel_types: list[str] | None = None,
        direction: str = "both",
        max_depth: int = 2,
        graph_layer: str = "all",
    ) -> list[list]:
        """Impact-analysis variant of ``find_by_artifact`` with rel-type and
        direction filters. Builds the Cypher pattern dynamically so the
        caller can scope traversal (e.g. only ``supersedes``/``contradicts``
        edges, only outgoing from the artifact).

        ``graph_layer`` (spec 849d6292, TR4) scopes every returned node to a
        layer — ``canonical`` (default at the service boundary) must never leak
        ``working`` nodes; ``working`` returns only working; ``all`` returns
        both. The filter is applied to center, hop1 and hop2 in Cypher so the
        non-leakage guarantee holds at the store, not just the API surface.
        """
        if direction == "outgoing":
            hop1_pat = "(center)-[r1]->(hop1)"
        elif direction == "incoming":
            hop1_pat = "(center)<-[r1]-(hop1)"
        else:
            hop1_pat = "(center)-[r1]-(hop1)"

        params: dict[str, Any] = {
            "artifact_id": artifact_id,
            "min_confidence": filters.min_confidence,
            "max_rows": filters.max_rows,
            "graph_layer": graph_layer,
        }
        # Layer scoping clauses (spec 849d6292). The center is the explicitly
        # requested anchor and is always returned; only the EXPANDED neighbors
        # (hop1, hop2) are layer-scoped so a canonical view never pulls in
        # working nodes. hop2 is optional so a null hop2 (no second hop) is kept.
        # Fail-closed layer scoping (bug 07bdf670) via the single-source helper:
        # a hop with NULL/absent graph_layer is NOT treated as canonical.
        hop1_layer = tpl.layer_filter_clause("hop1")
        hop2_layer = f"(hop2 IS NULL OR {tpl.layer_filter_clause('hop2')})"
        if rel_types:
            # Kùzu doesn't expose `label(r)` as a parameter-safe filter, so we
            # inline a whitelist check via :<type1>|:<type2> pattern syntax.
            # Normalize + guard against injection by keeping only identifier
            # characters.
            safe = [t for t in rel_types if t.replace("_", "").isalnum()]
            if safe:
                type_list = "|".join(safe)
                # Rewrite the hop1 pattern to carry the type filter inline.
                if direction == "outgoing":
                    hop1_pat = f"(center)-[r1:{type_list}]->(hop1)"
                elif direction == "incoming":
                    hop1_pat = f"(center)<-[r1:{type_list}]-(hop1)"
                else:
                    hop1_pat = f"(center)-[r1:{type_list}]-(hop1)"

        if max_depth == 1:
            cypher = (
                f"MATCH {hop1_pat} "
                "WHERE center.source_artifact_ref = $artifact_id "
                "  AND center.source_confidence >= $min_confidence "
                f"  AND {hop1_layer} "
                "RETURN center.id AS center_id, center.title AS center_title, "
                "       hop1.id AS hop1_id, hop1.title AS hop1_title, "
                "       NULL AS hop2_id, NULL AS hop2_title, "
                "       label(r1) AS rel1_type, NULL AS rel2_type "
                "LIMIT $max_rows"
            )
        else:
            cypher = (
                f"MATCH {hop1_pat} "
                "WHERE center.source_artifact_ref = $artifact_id "
                "  AND center.source_confidence >= $min_confidence "
                f"  AND {hop1_layer} "
                "OPTIONAL MATCH (hop1)-[r2]-(hop2) "
                f"WHERE {hop2_layer} "
                "RETURN center.id AS center_id, center.title AS center_title, "
                "       hop1.id AS hop1_id, hop1.title AS hop1_title, "
                "       hop2.id AS hop2_id, hop2.title AS hop2_title, "
                "       label(r1) AS rel1_type, label(r2) AS rel2_type "
                "LIMIT $max_rows"
            )
        return self._exec(board_id, cypher, params)

    def traverse_supersedence(
        self, board_id: str, decision_id: str, max_depth: int = 10
    ) -> list[list]:
        return self._exec(board_id, tpl.GET_SUPERSEDENCE_CHAIN, {
            "decision_id": decision_id,
        })

    def find_contradictions(
        self, board_id: str, node_id: str | None, limit: int
    ) -> list[list]:
        if node_id:
            return self._exec(board_id, tpl.FIND_CONTRADICTIONS_BY_NODE, {
                "node_id": node_id,
                "max_rows": limit,
            })
        return self._exec(board_id, tpl.FIND_CONTRADICTIONS_ALL, {
            "max_rows": limit,
        })

    def vector_search(
        self, board_id: str, node_type: str, query_vec: list[float],
        top_k: int, min_similarity: float,
    ) -> list[dict]:
        from okto_pulse.core.kg.graph_availability import (
            graph_unavailable_error,
            is_graph_unavailable_error,
        )
        from okto_pulse.core.kg.search import find_similar_nodes_by_type

        try:
            with open_board_connection(board_id) as (_db, conn):
                raw = find_similar_nodes_by_type(
                    board_id=board_id,
                    node_type=node_type,
                    query_vector=query_vec,
                    top_k=top_k,
                    min_similarity=min_similarity,
                    conn=conn,
                )
        except Exception as exc:
            if is_graph_unavailable_error(exc):
                raise graph_unavailable_error(board_id) from exc
            raise
        return [
            {
                "node_id": r.kuzu_node_id,
                "node_type": r.node_type,
                "title": r.title,
                "source_artifact_ref": r.source_artifact_ref,
                "similarity": r.similarity,
            }
            for r in raw
        ]

    def get_constraint_detail(
        self, board_id: str, constraint_id: str
    ) -> tuple[list[list], list[list], list[list]]:
        params = {"constraint_id": constraint_id}
        main = self._exec(board_id, tpl.EXPLAIN_CONSTRAINT, params)
        origins = self._exec(board_id, tpl.EXPLAIN_CONSTRAINT_ORIGINS, params)
        violations = self._exec(board_id, tpl.EXPLAIN_CONSTRAINT_VIOLATIONS, params)
        return main, origins, violations

    def get_alternatives(
        self, board_id: str, decision_id: str, limit: int
    ) -> list[list]:
        return self._exec(board_id, tpl.LIST_ALTERNATIVES, {
            "decision_id": decision_id,
            "max_rows": limit,
        })

    def get_learnings_for_area(
        self, board_id: str, area: str, filters: QueryFilters
    ) -> list[list]:
        return self._exec(board_id, tpl.GET_LEARNING_FROM_BUGS, {
            "area": area,
            "min_confidence": filters.min_confidence,
            "min_relevance": filters.min_relevance,
            "max_rows": filters.max_rows,
        })

    def get_schema_version(self, board_id: str) -> str | None:
        rows = self._exec(
            board_id,
            "MATCH (m:BoardMeta {board_id: $b}) RETURN m.schema_version",
            {"b": board_id},
        )
        if rows:
            return rows[0][0]
        return None

    def get_schema_info(self, board_id: str, *, include_internal: bool = False) -> dict:
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "stable_node_types": [{"name": nt, "stable": True} for nt in NODE_TYPES],
            "stable_rel_types": stable_rel_type_entries(),
            "vector_indexes": [
                {
                    "node_type": nt,
                    "attribute": "embedding",
                    "dimension": 384,
                    "similarity_metric": "cosine",
                    "index_name": vector_index_name(nt),
                }
                for nt in VECTOR_INDEX_TYPES
            ],
        }
        if include_internal:
            result["internal_node_types"] = [{"name": "BoardMeta", "stable": False}]
            result["internal_rel_types"] = []
        return result
