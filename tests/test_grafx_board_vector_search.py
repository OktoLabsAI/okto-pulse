"""Finite M-PULSE-4 tests for the inactive Grafx board search helper."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import okto_grafx
import pytest
from okto_grafx.errors import GrafxIndexError
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.kg.interfaces.graph_errors import GraphIndexUnavailable

from okto_pulse.community.adapters.grafx_board_vector_search import (
    CommunityGrafxBoardVectorSearch,
)

DIMENSION = 384
PUBLIC_SPACES = {
    "Decision": "decision_embedding_idx",
    "Criterion": "criterion_embedding_idx",
    "Constraint": "constraint_embedding_idx",
    "Requirement": "requirement_embedding_idx",
    "Entity": "entity_embedding_idx",
    "APIContract": "apicontract_embedding_idx",
    "TestScenario": "testscenario_embedding_idx",
    "Bug": "bug_embedding_idx",
    "Learning": "learning_embedding_idx",
}
RESULT_KEYS = {
    "node_id",
    "node_type",
    "title",
    "source_artifact_ref",
    "content",
    "context",
    "justification",
    "kind_of",
    "similarity",
}


def _vector(first: float, second: float = 0.0) -> list[float]:
    return [first, second, *([0.0] * (DIMENSION - 2))]


@dataclass(frozen=True)
class _Result:
    rows: tuple[tuple[object, ...], ...]


class _Reader:
    def __init__(
        self,
        indexed_rows: tuple[tuple[object, ...], ...] = (),
        exact_rows: tuple[tuple[object, ...], ...] = (),
    ) -> None:
        self.indexed_rows = indexed_rows
        self.exact_rows = exact_rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: str, parameters: dict[str, object]) -> _Result:
        self.calls.append((statement, dict(parameters)))
        rows = self.indexed_rows if "similarity(" in statement else self.exact_rows
        return _Result(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _Database:
    def __init__(self, reader: _Reader) -> None:
        self.reader = reader
        self.begin_modes: list[str] = []

    def begin(self, mode: str) -> _Reader:
        self.begin_modes.append(mode)
        return self.reader


@pytest.mark.parametrize(("node_type", "space"), tuple(PUBLIC_SPACES.items()))
def test_all_nine_public_types_map_to_their_board_space_once(
    node_type: str,
    space: str,
) -> None:
    reader = _Reader()
    database = _Database(reader)
    resolved: list[str] = []

    adapter = CommunityGrafxBoardVectorSearch(
        lambda board_id: resolved.append(board_id) or database  # type: ignore[arg-type]
    )
    assert adapter.vector_search("board-1", node_type, _vector(1.0), 2, 0.0) == []

    assert resolved == ["board-1"]
    assert database.begin_modes == ["read"]
    assert len(reader.calls) == 2
    assert f"MATCH (n:{node_type})" in reader.calls[0][0]
    assert f"space => '{space}'" in reader.calls[0][0]
    assert reader.calls[0][1]["search_k"] == 3
    assert reader.calls[0][1]["raw_threshold"] == -1.0


@pytest.mark.parametrize("node_type", ("Alternative", "Assumption", "Custom"))
def test_non_public_types_return_empty_without_resolving(node_type: str) -> None:
    def must_not_resolve(_board_id: str):
        raise AssertionError("a non-public type must not resolve a database")

    adapter = CommunityGrafxBoardVectorSearch(must_not_resolve)

    assert adapter.vector_search("board-1", node_type, [], 0, -1.0) == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"board_id": ""}, "board_id must be non-empty text"),
        ({"top_k": 0}, "top_k must be a positive integer"),
        ({"top_k": True}, "top_k must be a positive integer"),
        ({"min_similarity": -0.1}, "min_similarity must be"),
        ({"min_similarity": float("nan")}, "min_similarity must be"),
        ({"graph_layer": "legacy"}, "invalid_graph_layer"),
        ({"include_superseded": 1}, "include_superseded must be a boolean"),
        ({"query_vec": [1.0]}, "query_vec must contain 384 numbers"),
        ({"query_vec": _vector(float("inf"))}, "components must be finite"),
    ),
)
def test_arguments_are_refused_before_database_resolution(
    overrides: dict[str, object],
    message: str,
) -> None:
    arguments: dict[str, object] = {
        "board_id": "board-1",
        "node_type": "Decision",
        "query_vec": _vector(1.0),
        "top_k": 2,
        "min_similarity": 0.0,
        "include_superseded": False,
        "graph_layer": "all",
    }
    arguments.update(overrides)

    def must_not_resolve(_board_id: str):
        raise AssertionError("invalid input must not resolve a database")

    adapter = CommunityGrafxBoardVectorSearch(must_not_resolve)
    with pytest.raises(ValueError, match=message):
        adapter.vector_search(**arguments)  # type: ignore[arg-type]


def _indexed_row(node_id: str, score: float) -> tuple[object, ...]:
    return (
        node_id,
        f"title {node_id}",
        f"spec:{node_id}",
        f"content {node_id}",
        f"context {node_id}",
        f"justification {node_id}",
        "decision",
        score,
    )


def _exact_row(node_id: str, embedding: list[float]) -> tuple[object, ...]:
    return (*_indexed_row(node_id, 0.0)[:7], embedding)


def test_normalized_tie_at_cutoff_uses_complete_exact_logical_id_order() -> None:
    half = _vector(0.5, math.sqrt(0.75))
    reader = _Reader(
        indexed_rows=(
            _indexed_row("z-best", 0.8),
            _indexed_row("c-tied", 0.5),
            _indexed_row("b-tied", 0.5 + 5e-10),
        ),
        exact_rows=(
            _exact_row("c-tied", half),
            _exact_row("z-best", _vector(0.8, 0.6)),
            _exact_row("a-tied", half),
            _exact_row("b-tied", half),
        ),
    )
    adapter = CommunityGrafxBoardVectorSearch(
        lambda _board_id: _Database(reader)  # type: ignore[arg-type]
    )

    hits = adapter.vector_search("board-1", "Decision", _vector(1.0), 2, 0.0)

    assert [hit["node_id"] for hit in hits] == ["z-best", "a-tied"]
    assert len(reader.calls) == 2
    assert "n.embedding" in reader.calls[1][0]


def test_underfilled_index_page_uses_complete_exact_path() -> None:
    reader = _Reader(
        indexed_rows=(_indexed_row("best", 1.0),),
        exact_rows=(
            _exact_row("best", _vector(1.0)),
            _exact_row("second", _vector(0.8, 0.6)),
            _exact_row("third", _vector(0.0, 1.0)),
        ),
    )
    database = _Database(reader)
    adapter = CommunityGrafxBoardVectorSearch(
        lambda _board_id: database  # type: ignore[arg-type]
    )

    hits = adapter.vector_search("board-1", "Decision", _vector(1.0), 2, 0.0)

    assert [hit["node_id"] for hit in hits] == ["best", "second"]
    assert len(reader.calls) == 2


def test_zero_norm_cosine_is_zero_and_obeys_the_inclusive_threshold() -> None:
    reader = _Reader(
        indexed_rows=(
            _indexed_row("z-zero", 0.0),
            _indexed_row("a-nonzero", 0.0),
        ),
        exact_rows=(
            _exact_row("z-zero", _vector(0.0)),
            _exact_row("a-nonzero", _vector(1.0)),
        ),
    )
    database = _Database(reader)
    adapter = CommunityGrafxBoardVectorSearch(
        lambda _board_id: database  # type: ignore[arg-type]
    )

    included = adapter.vector_search("board-1", "Decision", _vector(0.0), 2, 0.0)
    excluded = adapter.vector_search("board-1", "Decision", _vector(0.0), 2, 0.1)

    assert [(hit["node_id"], hit["similarity"]) for hit in included] == [
        ("a-nonzero", 0.0),
        ("z-zero", 0.0),
    ]
    assert excluded == []


def test_complete_page_without_cutoff_tie_stays_on_the_bounded_path() -> None:
    reader = _Reader(
        indexed_rows=(
            _indexed_row("second", 0.8),
            _indexed_row("best", 1.0),
            _indexed_row("witness", 0.1),
        ),
        exact_rows=(_exact_row("must-not-run", _vector(1.0)),),
    )
    database = _Database(reader)
    adapter = CommunityGrafxBoardVectorSearch(
        lambda _board_id: database  # type: ignore[arg-type]
    )

    hits = adapter.vector_search("board-1", "Decision", _vector(1.0), 2, 0.0)

    assert [hit["node_id"] for hit in hits] == ["best", "second"]
    assert len(reader.calls) == 1


def _create_decision_schema(database) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            "CREATE VECTOR SPACE decision_embedding_idx "
            "{dimension: 384, metric: 'cosine', normalized: false, "
            "storage_dtype: 'float64'}"
        )
        transaction.execute(
            "CREATE NODE TABLE Decision("
            "id STRING, title STRING, source_artifact_ref STRING, "
            "content STRING, context STRING, justification STRING, "
            "kind_of STRING, embedding VECTOR(decision_embedding_idx), "
            "superseded_by STRING, graph_layer STRING, revocation_reason STRING, "
            "PRIMARY KEY(id))"
        )


def _insert_decision(
    database,
    node_id: str,
    embedding: list[float] | None,
    *,
    graph_layer: str | None = "canonical",
    superseded_by: str | None = None,
    revocation_reason: str | None = None,
) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            "CREATE (:Decision {"
            "id: $id, title: $title, source_artifact_ref: $source_ref, "
            "content: $content, context: $context, justification: $justification, "
            "kind_of: $kind_of, embedding: $embedding, "
            "superseded_by: $superseded_by, graph_layer: $graph_layer, "
            "revocation_reason: $revocation_reason})",
            {
                "id": node_id,
                "title": f"title {node_id}",
                "source_ref": f"spec:{node_id}",
                "content": f"content {node_id}",
                "context": f"context {node_id}",
                "justification": f"justification {node_id}",
                "kind_of": "decision",
                "embedding": embedding,
                "superseded_by": superseded_by,
                "graph_layer": graph_layer,
                "revocation_reason": revocation_reason,
            },
        )


def test_real_grafx_filters_before_ranking_and_normalizes_exact_scores() -> None:
    with okto_grafx.connect(":memory:") as database:
        _create_decision_schema(database)
        _insert_decision(database, "a-tied", _vector(1.0))
        _insert_decision(database, "b-tied", _vector(1.0))
        _insert_decision(database, "negative", _vector(-1.0))
        _insert_decision(database, "zero-norm", _vector(0.0))
        _insert_decision(database, "working", _vector(1.0), graph_layer="working")
        _insert_decision(database, "legacy", _vector(1.0), graph_layer=None)
        _insert_decision(
            database,
            "superseded",
            _vector(1.0),
            superseded_by="a-tied",
        )
        _insert_decision(database, "null-vector", None)
        for reason in sorted(tpl.ACTIVE_READ_TOMBSTONE_REASONS):
            _insert_decision(
                database,
                f"tombstone-{reason}",
                _vector(1.0),
                superseded_by="a-tied",
                revocation_reason=reason,
            )

        resolved: list[str] = []
        adapter = CommunityGrafxBoardVectorSearch(
            lambda board_id: resolved.append(board_id) or database
        )
        canonical = adapter.vector_search(
            "board-1",
            "Decision",
            _vector(1.0),
            20,
            0.0,
            graph_layer="canonical",
        )
        historical = adapter.vector_search(
            "board-1",
            "Decision",
            _vector(1.0),
            20,
            1.0,
            include_superseded=True,
            graph_layer="canonical",
        )
        working = adapter.vector_search(
            "board-1", "Decision", _vector(1.0), 20, 1.0, graph_layer="working"
        )
        all_layers = adapter.vector_search(
            "board-1", "Decision", _vector(1.0), 20, 1.0, graph_layer="all"
        )

    assert resolved == ["board-1"] * 4
    assert [hit["node_id"] for hit in canonical] == [
        "a-tied",
        "b-tied",
        "negative",
        "zero-norm",
    ]
    assert canonical[-1]["similarity"] == 0.0
    assert [hit["node_id"] for hit in historical] == [
        "a-tied",
        "b-tied",
        "superseded",
    ]
    assert [hit["node_id"] for hit in working] == ["working"]
    assert [hit["node_id"] for hit in all_layers] == [
        "a-tied",
        "b-tied",
        "legacy",
        "working",
    ]
    assert all(set(hit) == RESULT_KEYS for hit in canonical)
    assert all(
        not hit["node_id"].startswith("tombstone-")
        for hit in (*canonical, *historical, *working, *all_layers)
    )


def test_real_grafx_ann_is_stable_cold_warm_and_after_reopen(tmp_path: Path) -> None:
    root = tmp_path / "board-vector"
    database = okto_grafx.connect(root, vector_exact_scan_threshold=0)
    _create_decision_schema(database)
    _insert_decision(database, "best", _vector(1.0))
    _insert_decision(database, "second", _vector(0.8, 0.6))
    _insert_decision(database, "third", _vector(0.0, 1.0))
    _insert_decision(database, "opposite", _vector(-1.0))
    _insert_decision(
        database, "ineligible-working", _vector(1.0), graph_layer="working"
    )
    _insert_decision(
        database, "ineligible-superseded", _vector(1.0), superseded_by="best"
    )
    _insert_decision(
        database,
        "ineligible-tombstone",
        _vector(1.0),
        revocation_reason=next(iter(sorted(tpl.ACTIVE_READ_TOMBSTONE_REASONS))),
    )
    _insert_decision(database, "ineligible-null", None)
    adapter = CommunityGrafxBoardVectorSearch(lambda _board_id: database)
    cold = adapter.vector_search(
        "board-1", "Decision", _vector(1.0), 2, 0.0, graph_layer="canonical"
    )
    warm = adapter.vector_search(
        "board-1", "Decision", _vector(1.0), 2, 0.0, graph_layer="canonical"
    )
    database.close()

    with okto_grafx.connect(root, vector_exact_scan_threshold=0) as reopened:
        reopened_adapter = CommunityGrafxBoardVectorSearch(lambda _board_id: reopened)
        cold_reopen = reopened_adapter.vector_search(
            "board-1",
            "Decision",
            _vector(1.0),
            2,
            0.0,
            graph_layer="canonical",
        )

    assert [hit["node_id"] for hit in cold] == ["best", "second"]
    assert warm == cold
    assert cold_reopen == cold
    assert not {
        "ineligible-working",
        "ineligible-superseded",
        "ineligible-tombstone",
        "ineligible-null",
    }.intersection(hit["node_id"] for hit in (*cold, *warm, *cold_reopen))


def test_grafx_failures_map_to_core_without_leaking_backend_errors() -> None:
    def fail(_board_id: str):
        raise GrafxIndexError("vector index is stale")

    adapter = CommunityGrafxBoardVectorSearch(fail)

    with pytest.raises(GraphIndexUnavailable) as captured:
        adapter.vector_search("board-1", "Decision", _vector(1.0), 2, 0.0)

    assert captured.value.details["backend"] == "okto_grafx"
    assert captured.value.details["operation"] == "grafx_board_vector_search"
    assert captured.value.details["backend_error_type"] == "GrafxIndexError"
