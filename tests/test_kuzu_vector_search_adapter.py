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
    def __init__(self, *, fail_index: bool = False):
        self.fail_index = fail_index
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
                ]
                for index in range(params["k"])
            ]
        else:
            rows = [
                ["learning_a", "A", "spec:a", [1.0, 0.0], None],
                ["learning_b", "B", "spec:b", [0.0, 1.0], None],
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
