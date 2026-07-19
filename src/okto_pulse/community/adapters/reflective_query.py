"""Community implementations for the Core-owned reflective KG state machine.

The edition owns graph, embedding and telemetry concretes.  Core sees only the
ports in ``kg.interfaces.reflective_query`` and therefore never imports Kuzu,
SQLite or an edition package.  The deterministic critic is intentionally the
baseline: local Community deployments get a real bounded critic loop without
requiring an LLM.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from okto_pulse.community.adapters.hybrid_search import KuzuGraphExpander
from okto_pulse.core.kg.interfaces.reflective_query import (
    REFLECTIVE_DEFAULT_EDGES,
    Adequacy,
    CriticAction,
    CriticDecision,
    ReflectiveCriticRequest,
    ReflectiveRetrievalBatch,
    ReflectiveRetrievalRequest,
)

logger = logging.getLogger("okto_pulse.community.reflective_query")

_WORD = re.compile(r"[\w-]+", re.UNICODE)


def _stable_rows(rows: list[Mapping[str, Any]], limit: int) -> tuple[Mapping[str, Any], ...]:
    best: dict[str, dict[str, Any]] = {}
    for raw in rows:
        node_id = str(raw.get("node_id") or "")
        if not node_id:
            continue
        row = dict(raw)
        row["node_id"] = node_id
        row["node_type"] = str(row.get("node_type") or "unknown")
        row["title"] = str(row.get("title") or "")
        row["similarity"] = max(
            0.0, min(1.0, float(row.get("similarity") or 0.0))
        )
        current = best.get(node_id)
        if current is None or row["similarity"] > current["similarity"]:
            best[node_id] = row
    ordered = sorted(
        best.values(),
        key=lambda item: (
            -float(item["similarity"]),
            str(item["node_type"]),
            str(item["node_id"]),
        ),
    )
    return tuple(ordered[:limit])


def _rewrite_query(query: str) -> str:
    """Deterministic, bounded rewrite used by the no-LLM critic."""

    tokens = _WORD.findall(query.casefold())
    # Keep order while removing duplicate noise tokens.
    return " ".join(dict.fromkeys(tokens))[:500]


class CommunityReflectiveRetrieval:
    """Vector retrieval plus deterministic graph expansion/fallback actions."""

    identity = "community-kuzu-reflective-retrieval"
    version = "2"

    def __init__(
        self,
        *,
        graph_store: Any,
        embedding_provider: Any,
        cypher_executor: Any,
    ) -> None:
        if not callable(getattr(graph_store, "vector_search", None)):
            raise ValueError("reflective_graph_store_required")
        if not callable(getattr(embedding_provider, "encode", None)):
            raise ValueError("reflective_embedding_provider_required")
        self._graph_store = graph_store
        self._embedding = embedding_provider
        self._expander = KuzuGraphExpander(cypher_executor)

    def _node_types(self, board_id: str, target_intent: str | None) -> tuple[str, ...]:
        info = self._graph_store.get_schema_info(board_id)
        available = tuple(
            sorted(
                {
                    str(item.get("node_type"))
                    for item in (info.get("vector_indexes") or [])
                    if isinstance(item, Mapping) and item.get("node_type")
                }
            )
        )
        if target_intent:
            wanted = target_intent.casefold().replace("_", "")
            narrowed = tuple(
                item
                for item in available
                if item.casefold().replace("_", "") == wanted
            )
            if narrowed:
                return narrowed
        return available

    def _vector_rows(
        self,
        request: ReflectiveRetrievalRequest,
        *,
        query: str,
        fallback: bool,
    ) -> list[Mapping[str, Any]]:
        vector = self._embedding.encode(query)
        node_types = self._node_types(request.board_id, request.target_intent)
        threshold = 0.0 if fallback else request.min_confidence
        rows: list[Mapping[str, Any]] = []
        for node_type in node_types:
            hits = self._graph_store.vector_search(
                request.board_id,
                node_type,
                vector,
                request.limit,
                threshold,
                graph_layer=request.graph_layer,
            )
            for hit in hits:
                if isinstance(hit, Mapping):
                    rows.append(hit)
        return rows

    def _graph_version(self, board_id: str) -> str:
        version = self._graph_store.get_schema_version(board_id)
        if version:
            return str(version)
        info = self._graph_store.get_schema_info(board_id)
        return str(info.get("schema_version") or "unknown")

    def retrieve(
        self, request: ReflectiveRetrievalRequest
    ) -> ReflectiveRetrievalBatch:
        action = request.action
        mode = "vector_seed"
        cost = 1

        if action == CriticAction.EXPAND_HOPS:
            mode = "graph_expand"
            cost = 2
            previous = [dict(row) for row in request.previous_rows]
            seeds = tuple(
                str(row.get("node_id"))
                for row in previous
                if row.get("node_id")
            )
            neighbors = self._expander.expand(
                board_id=request.board_id,
                seed_ids=seeds,
                edges=REFLECTIVE_DEFAULT_EDGES,
                max_hops=request.fixed_hops_hint,
                graph_layer=request.graph_layer,
            )
            base_similarity = max(
                (float(row.get("similarity") or 0.0) for row in previous),
                default=0.0,
            )
            rows: list[Mapping[str, Any]] = previous
            rows.extend(
                {
                    "node_id": item.node_id,
                    "node_type": item.node_type,
                    "title": item.title,
                    "similarity": max(
                        0.0, base_similarity - (0.05 * item.hop_distance)
                    ),
                    "edge_type": item.edge_type,
                    "hop_distance": item.hop_distance,
                }
                for item in neighbors
            )
        else:
            fallback = action == CriticAction.FALLBACK_SEMANTIC
            if fallback:
                mode = "semantic_fallback"
            elif action == CriticAction.RETRY_WITH_REWRITE:
                mode = "query_rewrite"
            elif action == CriticAction.CHANGE_INTENT:
                mode = "intent_change"
            effective_query = (
                request.rewritten_query
                if action == CriticAction.RETRY_WITH_REWRITE
                and request.rewritten_query
                else request.query
            )
            rows = self._vector_rows(
                request,
                query=effective_query,
                fallback=fallback,
            )

        stable = _stable_rows(rows, request.limit)
        return ReflectiveRetrievalBatch(
            rows=stable,
            graph_version=self._graph_version(request.board_id),
            retrieval_mode=mode,
            cost_units=cost,
            metadata={"node_count": len(stable)},
        )


class CommunityDeterministicReflectiveCritic:
    """Fail-closed local critic with no model/network dependency."""

    identity = "community-deterministic-reflective-critic"
    version = "2"

    def evaluate(self, request: ReflectiveCriticRequest) -> CriticDecision:
        rows = request.rows
        if not rows:
            if request.iteration == 0:
                rewritten = _rewrite_query(request.query)
                if rewritten:
                    return CriticDecision(
                        adequacy=Adequacy.IRRELEVANT,
                        reason="no_rows_rewrite",
                        suggested_action=CriticAction.RETRY_WITH_REWRITE,
                        confidence=0.0,
                        rewritten_query=rewritten,
                    )
            if request.previous_action != CriticAction.FALLBACK_SEMANTIC:
                return CriticDecision(
                    adequacy=Adequacy.IRRELEVANT,
                    reason="no_rows_semantic_fallback",
                    suggested_action=CriticAction.FALLBACK_SEMANTIC,
                    confidence=0.0,
                )
            return CriticDecision(
                adequacy=Adequacy.IRRELEVANT,
                reason="no_rows_rejected",
                suggested_action=CriticAction.REJECT,
                confidence=0.0,
            )

        top = max(float(row.get("similarity") or 0.0) for row in rows)
        if top >= 0.5 or len(rows) >= 3:
            return CriticDecision(
                adequacy=Adequacy.SUFFICIENT,
                reason="evidence_threshold_met",
                suggested_action=CriticAction.ACCEPT,
                confidence=max(0.5, min(1.0, top)),
            )
        if request.previous_action == CriticAction.EXPAND_HOPS:
            return CriticDecision(
                adequacy=Adequacy.PARTIAL,
                reason="weak_evidence_rejected",
                suggested_action=CriticAction.REJECT,
                confidence=max(0.0, min(1.0, top)),
            )
        return CriticDecision(
            adequacy=Adequacy.PARTIAL,
            reason="weak_evidence_expand",
            suggested_action=CriticAction.EXPAND_HOPS,
            confidence=max(0.0, min(1.0, top)),
        )


class CommunityReflectiveTelemetry:
    """Safe structured logging sink; Core emits hashes/counters only."""

    def emit(self, event: Mapping[str, Any]) -> None:
        logger.info("kg.reflective event=%s", dict(event))


def build_community_reflective_providers(
    *,
    graph_store: Any,
    embedding_provider: Any,
    cypher_executor: Any,
) -> dict[str, Any]:
    return {
        "reflective_retrieval": CommunityReflectiveRetrieval(
            graph_store=graph_store,
            embedding_provider=embedding_provider,
            cypher_executor=cypher_executor,
        ),
        "reflective_critic": CommunityDeterministicReflectiveCritic(),
        "reflective_telemetry": CommunityReflectiveTelemetry(),
    }


__all__ = [
    "CommunityDeterministicReflectiveCritic",
    "CommunityReflectiveRetrieval",
    "CommunityReflectiveTelemetry",
    "build_community_reflective_providers",
]
