"""Inactive M-PULSE-4 board vector search over public Okto Grafx doors.

The adapter resolves exactly one board database for each call and keeps the
indexed page and any exact fallback in one read snapshot.  Provider selection,
board-path routing and activation in ``kg.py``/``composition.py`` belong to
M-PULSE-6 and are deliberately absent here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from types import MappingProxyType

from okto_grafx import Database, VectorValue
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.kg.schema_contract import VECTOR_INDEX_TYPES

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_schema_manifest import (
    EMBEDDING_DIMENSION,
    PULSE_GRAFX_SCHEMA_MANIFEST,
)

DatabaseResolver = Callable[[str], Database]

_OPERATION = "grafx_board_vector_search"
_LAYERS = frozenset({"canonical", "working", "all"})
_SCORE_ABS_TOLERANCE = 1e-9
_SCORE_REL_TOLERANCE = 1e-9

_SPACE_BY_NODE_TYPE = MappingProxyType(
    {
        space.node_type: space.name
        for space in PULSE_GRAFX_SCHEMA_MANIFEST.spaces
        if space.node_type in VECTOR_INDEX_TYPES
    }
)

_PAYLOAD_PROJECTION = (
    "n.id, n.title, n.source_artifact_ref, n.content, n.context, "
    "n.justification, n.kind_of"
)


def _candidate_predicate() -> str:
    """Return the single board-eligibility predicate used by both paths."""

    return (
        "n.embedding IS NOT NULL "
        f"AND {tpl.superseded_filter_clause('n')} "
        f"AND {tpl.layer_filter_clause('n')} "
        f"AND {tpl.active_read_filter_clause('n')}"
    )


def _indexed_statement(node_type: str, space: str) -> str:
    return (
        f"MATCH (n:{node_type}) WHERE {_candidate_predicate()} "
        f"AND similarity(n.embedding, $query, space => '{space}') "
        ">= $raw_threshold "
        f"RETURN {_PAYLOAD_PROJECTION}, similarity_score() AS score "
        "ORDER BY score DESC LIMIT $search_k"
    )


def _exact_statement(node_type: str) -> str:
    return (
        f"MATCH (n:{node_type}) WHERE {_candidate_predicate()} "
        f"RETURN {_PAYLOAD_PROJECTION}, n.embedding"
    )


def _validate_arguments(
    *,
    board_id: object,
    query_vec: object,
    top_k: object,
    min_similarity: object,
    include_superseded: object,
    graph_layer: object,
) -> tuple[str, tuple[float, ...], int, float, bool, str]:
    if type(board_id) is not str or not board_id:
        raise ValueError("board_id must be non-empty text")
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if type(include_superseded) is not bool:
        raise ValueError("include_superseded must be a boolean")
    if type(graph_layer) is not str or graph_layer not in _LAYERS:
        raise ValueError("invalid_graph_layer")
    if type(min_similarity) not in (int, float) or isinstance(min_similarity, bool):
        raise ValueError("min_similarity must be a finite number from 0 to 1")
    threshold = float(min_similarity)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("min_similarity must be a finite number from 0 to 1")
    if isinstance(query_vec, (str, bytes, bytearray)):
        raise ValueError(f"query_vec must contain {EMBEDDING_DIMENSION} numbers")
    try:
        components = tuple(float(component) for component in query_vec)  # type: ignore[union-attr]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"query_vec must contain {EMBEDDING_DIMENSION} numbers"
        ) from exc
    if len(components) != EMBEDDING_DIMENSION:
        raise ValueError(f"query_vec must contain {EMBEDDING_DIMENSION} numbers")
    if not all(math.isfinite(component) for component in components):
        raise ValueError("query_vec components must be finite")
    return (
        board_id,
        components,
        top_k,
        threshold,
        include_superseded,
        graph_layer,
    )


def _parameters(
    *,
    query: tuple[float, ...],
    top_k: int,
    min_similarity: float,
    include_superseded: bool,
    graph_layer: str,
) -> dict[str, object]:
    # A public threshold of zero admits negative raw cosine values after they
    # clamp to zero.  For positive thresholds, clamp(x, 0, 1) >= t is exactly
    # x >= t.
    raw_threshold = -1.0 if min_similarity == 0.0 else min_similarity
    return {
        "query": query,
        "raw_threshold": raw_threshold,
        "search_k": top_k + 1,
        "include_superseded": include_superseded,
        "graph_layer": graph_layer,
    }


def _score(raw_score: object) -> float:
    if type(raw_score) not in (int, float) or isinstance(raw_score, bool):
        raise ValueError("Grafx returned a non-numeric vector score")
    score = float(raw_score)
    if not math.isfinite(score):
        raise ValueError("Grafx returned a non-finite vector score")
    return max(0.0, min(1.0, score))


def _payload(row: Sequence[object], *, node_type: str, similarity: float) -> dict:
    if len(row) != 8:
        raise ValueError("Grafx returned an unexpected board vector row shape")
    node_id = row[0]
    if type(node_id) is not str:
        raise ValueError("Grafx returned a non-text Pulse node id")
    return {
        "node_id": node_id,
        "node_type": node_type,
        "title": row[1],
        "source_artifact_ref": row[2],
        "content": row[3],
        "context": row[4],
        "justification": row[5],
        "kind_of": row[6],
        "similarity": similarity,
    }


def _ranked_page(rows: Sequence[Sequence[object]], *, node_type: str) -> list[dict]:
    hits = [
        _payload(row, node_type=node_type, similarity=_score(row[7])) for row in rows
    ]
    hits.sort(key=lambda item: (-item["similarity"], item["node_id"]))
    return hits


def _embedding_components(value: object) -> tuple[float, ...]:
    source: object = value.values if isinstance(value, VectorValue) else value
    if isinstance(source, (str, bytes, bytearray)):
        raise ValueError("Grafx returned an invalid stored embedding")
    try:
        components = tuple(float(component) for component in source)  # type: ignore[union-attr]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Grafx returned an invalid stored embedding") from exc
    if len(components) != EMBEDDING_DIMENSION or not all(
        math.isfinite(component) for component in components
    ):
        raise ValueError("Grafx returned an invalid stored embedding")
    return components


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    left_norm = math.hypot(*left)
    right_norm = math.hypot(*right)
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    raw = math.fsum((a / left_norm) * (b / right_norm) for a, b in zip(left, right))
    return max(0.0, min(1.0, raw))


def _exact_hits(
    rows: Sequence[Sequence[object]],
    *,
    node_type: str,
    query: tuple[float, ...],
    top_k: int,
    min_similarity: float,
) -> list[dict]:
    hits: list[dict] = []
    for row in rows:
        if len(row) != 8:
            raise ValueError("Grafx returned an unexpected exact vector row shape")
        similarity = _cosine(query, _embedding_components(row[7]))
        if similarity >= min_similarity:
            hits.append(_payload(row, node_type=node_type, similarity=similarity))
    hits.sort(key=lambda item: (-item["similarity"], item["node_id"]))
    return hits[:top_k]


def _needs_exact(page: Sequence[dict], *, top_k: int) -> bool:
    # ``top_k + 1`` was requested.  A page with no witness beyond the public
    # cutoff cannot prove completeness, and an equal normalized score at the
    # cutoff cannot choose the logical-id winner safely.
    if len(page) <= top_k:
        return True
    return math.isclose(
        page[top_k - 1]["similarity"],
        page[top_k]["similarity"],
        abs_tol=_SCORE_ABS_TOLERANCE,
        rel_tol=_SCORE_REL_TOLERANCE,
    )


class CommunityGrafxBoardVectorSearch:
    """Board-scoped implementation of the existing vector-search operation."""

    def __init__(self, database_resolver: DatabaseResolver) -> None:
        if not callable(database_resolver):
            raise ValueError("database_resolver must be callable")
        self._database_resolver = database_resolver

    def vector_search(
        self,
        board_id: str,
        node_type: str,
        query_vec: list[float],
        top_k: int,
        min_similarity: float,
        *,
        include_superseded: bool = False,
        graph_layer: str = "all",
    ) -> list[dict]:
        """Return Pulse dictionaries without exposing any Grafx result type."""

        space = _SPACE_BY_NODE_TYPE.get(node_type) if type(node_type) is str else None
        if space is None:
            return []
        (
            wanted_board,
            query,
            wanted_k,
            threshold,
            wanted_superseded,
            wanted_layer,
        ) = _validate_arguments(
            board_id=board_id,
            query_vec=query_vec,
            top_k=top_k,
            min_similarity=min_similarity,
            include_superseded=include_superseded,
            graph_layer=graph_layer,
        )
        parameters = _parameters(
            query=query,
            top_k=wanted_k,
            min_similarity=threshold,
            include_superseded=wanted_superseded,
            graph_layer=wanted_layer,
        )

        try:
            # Resolve once.  Both reads use the same fixed snapshot so a
            # concurrent commit cannot split the page from its exact oracle.
            database = self._database_resolver(wanted_board)
            with database.begin("read") as reader:
                result = reader.execute(
                    _indexed_statement(node_type, space), parameters
                )
                page = _ranked_page(result.rows, node_type=node_type)
                page = [hit for hit in page if hit["similarity"] >= threshold]
                if not _needs_exact(page, top_k=wanted_k):
                    return page[:wanted_k]

                exact = reader.execute(
                    _exact_statement(node_type),
                    {
                        "include_superseded": wanted_superseded,
                        "graph_layer": wanted_layer,
                    },
                )
                return _exact_hits(
                    exact.rows,
                    node_type=node_type,
                    query=query,
                    top_k=wanted_k,
                    min_similarity=threshold,
                )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation=_OPERATION)
            if mapped is exc:
                raise
            raise mapped from exc


__all__ = ["CommunityGrafxBoardVectorSearch", "DatabaseResolver"]
