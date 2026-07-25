from __future__ import annotations

from contextlib import contextmanager

from okto_pulse.community.adapters import kuzu_graph_store as graph_store_module
from okto_pulse.community.adapters.kuzu_graph_store import CommunityKuzuGraphStore
from okto_pulse.core.kg.scoring import DECAY_REORDER_POOL_MULTIPLIER


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)
        self.closed = False

    def has_next(self):
        return bool(self._rows)

    def get_next(self):
        return self._rows.pop(0)

    def close(self):
        self.closed = True


class _Connection:
    def __init__(
        self,
        *,
        fail_index: bool = False,
        tombstoned_ids: frozenset[str] = frozenset(),
    ):
        self.fail_index = fail_index
        self.tombstoned_ids = tombstoned_ids
        self.requested_k = None
        self.results = []

    def execute(self, statement, params=None):
        if statement.startswith("CALL QUERY_VECTOR_INDEX"):
            if self.fail_index:
                raise RuntimeError("vector index unavailable")
            self.requested_k = params["k"]
            rows = [
                [
                    f"learning_{index:03d}",
                    f"title {index}",
                    None,
                    index * 0.01,
                    "newer" if index % 2 == 0 else None,
                    "canonical" if index % 2 else "working",
                    None,
                    None,
                    None,
                    (
                        "source_deleted"
                        if f"learning_{index:03d}" in self.tombstoned_ids
                        else None
                    ),
                ]
                for index in range(params["k"])
            ]
        else:
            rows = [
                [
                    "learning_a",
                    "A",
                    "spec:a",
                    [1.0, 0.0],
                    None,
                    "canonical",
                    None,
                    None,
                    None,
                    "source_deleted" if "learning_a" in self.tombstoned_ids else None,
                ],
                [
                    "learning_b",
                    "B",
                    "spec:b",
                    [0.0, 1.0],
                    None,
                    "working",
                    None,
                    None,
                    None,
                    "source_deleted" if "learning_b" in self.tombstoned_ids else None,
                ],
            ]
        result = _Result(rows)
        self.results.append(result)
        return result


def _install_connection(monkeypatch, connection):
    @contextmanager
    def _open(_board_id):
        yield object(), connection

    monkeypatch.setattr(graph_store_module, "open_board_connection", _open)


def test_indexed_search_overfetches_filters_and_closes_cursor(monkeypatch):
    connection = _Connection()
    _install_connection(monkeypatch, connection)

    hits = CommunityKuzuGraphStore().vector_search(
        "board-1",
        "Learning",
        [1.0, 0.0],
        top_k=5,
        min_similarity=0.0,
    )

    assert connection.requested_k == 5 * DECAY_REORDER_POOL_MULTIPLIER
    assert [hit["node_id"] for hit in hits] == [
        "learning_001",
        "learning_003",
        "learning_005",
        "learning_007",
        "learning_009",
    ]
    assert all(result.closed for result in connection.results)


def test_indexed_search_opt_in_keeps_superseded_nodes(monkeypatch):
    connection = _Connection()
    _install_connection(monkeypatch, connection)

    hits = CommunityKuzuGraphStore().vector_search(
        "board-1",
        "Learning",
        [1.0, 0.0],
        top_k=4,
        min_similarity=0.0,
        include_superseded=True,
    )

    assert connection.requested_k == 4
    assert [hit["node_id"] for hit in hits] == [
        "learning_000",
        "learning_001",
        "learning_002",
        "learning_003",
    ]


def test_index_failure_uses_linear_fallback(monkeypatch):
    connection = _Connection(fail_index=True)
    _install_connection(monkeypatch, connection)

    hits = CommunityKuzuGraphStore().vector_search(
        "board-1",
        "Learning",
        [1.0, 0.0],
        top_k=2,
        min_similarity=0.0,
    )

    assert [hit["node_id"] for hit in hits] == ["learning_a", "learning_b"]
    assert hits[0]["similarity"] == 1.0
    assert hits[1]["similarity"] == 0.0
    assert all(result.closed for result in connection.results)


def test_index_and_fallback_filter_source_deleted_tombstones(monkeypatch):
    indexed = _Connection(tombstoned_ids=frozenset({"learning_001"}))
    _install_connection(monkeypatch, indexed)

    indexed_hits = CommunityKuzuGraphStore().vector_search(
        "board-1",
        "Learning",
        [1.0, 0.0],
        top_k=3,
        min_similarity=0.0,
        include_superseded=False,
        graph_layer="all",
    )

    assert [hit["node_id"] for hit in indexed_hits] == [
        "learning_003",
        "learning_005",
        "learning_007",
    ]

    fallback = _Connection(
        fail_index=True,
        tombstoned_ids=frozenset({"learning_a"}),
    )
    _install_connection(monkeypatch, fallback)
    fallback_hits = CommunityKuzuGraphStore().vector_search(
        "board-1",
        "Learning",
        [1.0, 0.0],
        top_k=2,
        min_similarity=0.0,
        include_superseded=True,
        graph_layer="all",
    )

    assert [hit["node_id"] for hit in fallback_hits] == ["learning_b"]


def test_real_ladybug_canonical_excludes_demoted_and_all_preserves_it(
    monkeypatch,
):
    """Exercise the production layer predicate against installed Ladybug."""

    import ladybug

    database = ladybug.Database(":memory:")
    connection = ladybug.Connection(database)
    ddl = connection.execute(
        "CREATE NODE TABLE Decision("
        "id STRING PRIMARY KEY, title STRING, source_artifact_ref STRING, "
        "content STRING, context STRING, justification STRING, "
        "embedding DOUBLE[3], superseded_by STRING, graph_layer STRING, "
        "maturity_status STRING, revocation_reason STRING, "
        "relevance_score DOUBLE, generation INT64)"
    )
    ddl.close()
    for node_id, title, layer, maturity, reason, relevance in (
        (
            "decision-canonical",
            "Canonical decision",
            "canonical",
            "canonical_eligible",
            None,
            0.8,
        ),
        (
            "decision-demoted",
            "Demoted decision",
            "working",
            "working_immature",
            None,
            0.5,
        ),
        (
            "decision-deleted",
            "Deleted decision",
            "working",
            "working_stale",
            "source_deleted",
            0.0,
        ),
    ):
        created = connection.execute(
            "CREATE (:Decision {"
            "id: $id, title: $title, source_artifact_ref: $source_ref, "
            "content: $content, context: $context, "
            "justification: $justification, "
            "embedding: $embedding, superseded_by: $superseded_by, "
            "graph_layer: $graph_layer, maturity_status: $maturity_status, "
            "revocation_reason: $revocation_reason, "
            "relevance_score: $relevance_score, generation: $generation})",
            {
                "id": node_id,
                "title": title,
                "source_ref": f"spec:{node_id}",
                "content": f"content for {node_id}",
                "context": "test context",
                "justification": "test justification",
                "embedding": [1.0, 0.0, 0.0],
                "superseded_by": None,
                "graph_layer": layer,
                "maturity_status": maturity,
                "revocation_reason": reason,
                "relevance_score": relevance,
                "generation": 0,
            },
        )
        created.close()

    _install_connection(monkeypatch, connection)
    store = CommunityKuzuGraphStore()
    try:
        canonical_hits = store.vector_search(
            "board-1",
            "Decision",
            [1.0, 0.0, 0.0],
            top_k=10,
            min_similarity=0.9,
            graph_layer="canonical",
        )
        diagnostic_hits = store.vector_search(
            "board-1",
            "Decision",
            [1.0, 0.0, 0.0],
            top_k=10,
            min_similarity=0.9,
            graph_layer="all",
        )
        deleted_exact = store.find_active_by_source_ref(
            "board-1",
            "Decision",
            "spec:decision-deleted",
        )
    finally:
        connection.close()
        database.close()

    assert [item["node_id"] for item in canonical_hits] == ["decision-canonical"]
    assert {item["node_id"] for item in diagnostic_hits} == {
        "decision-canonical",
        "decision-demoted",
    }
    assert canonical_hits[0]["content"] == "content for decision-canonical"
    assert deleted_exact is None


def test_exact_source_ref_lookup_returns_active_semantic_payload(monkeypatch):
    store = CommunityKuzuGraphStore()
    captured = {}

    def _exec(board_id, statement, params):
        captured.update(
            {
                "board_id": board_id,
                "statement": statement,
                "params": params,
            }
        )
        return [
            [
                "decision-generation-2",
                "Use Kafka",
                "spec:1:decision:dec_1",
                "Kafka remains the choice",
                "Event streaming",
                "Operational maturity",
                2,
            ]
        ]

    monkeypatch.setattr(store, "_exec", _exec)

    found = store.find_active_by_source_ref(
        "board-1",
        "Decision",
        "spec:1:decision:dec_1",
    )

    assert found == {
        "node_id": "decision-generation-2",
        "node_type": "Decision",
        "title": "Use Kafka",
        "source_artifact_ref": "spec:1:decision:dec_1",
        "content": "Kafka remains the choice",
        "context": "Event streaming",
        "justification": "Operational maturity",
        "generation": 2,
    }
    assert "n.superseded_by IS NULL" in captured["statement"]
    assert "n.revocation_reason" in captured["statement"]
    assert captured["params"]["source_deleted_reason"] == "source_deleted"
