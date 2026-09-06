from __future__ import annotations

from typing import Any

from okto_pulse.community.api import kg_routes


class _RelationshipAwareExecutor:
    def __init__(self, rows_by_physical: dict[str, list[list[Any]]]) -> None:
        self.rows_by_physical = rows_by_physical
        self.queries: list[str] = []
        self.resolutions: list[tuple[str, str, str, str]] = []

    def relationship_table_name(
        self,
        board_id: str,
        logical_type: str,
        from_type: str,
        to_type: str,
    ) -> str:
        self.resolutions.append((board_id, logical_type, from_type, to_type))
        return f"{logical_type}__{from_type}__{to_type}"

    def execute_read_only(
        self,
        _board_id: str,
        query: str,
        _params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.queries.append(query)
        for physical, rows in self.rows_by_physical.items():
            if f"[r:{physical}]" in query:
                return {"rows": rows}
        raise AssertionError(f"query did not use a physical relationship table: {query}")


def test_graph_edges_use_grafx_physical_relationship_table(monkeypatch) -> None:
    physical = "belongs_to__Requirement__Entity"
    executor = _RelationshipAwareExecutor(
        {physical: [["requirement-1", "entity-1", 0.9]]}
    )
    monkeypatch.setattr(kg_routes, "resolve_cypher_executor", lambda: executor)
    monkeypatch.setattr(
        kg_routes,
        "_relation_pairs",
        lambda *_args: [("belongs_to", "Requirement", "Entity")],
    )

    edges, diagnostics = kg_routes._fetch_edges_for_nodes(
        "board-1",
        {"requirement-1"},
    )

    assert executor.resolutions == [
        ("board-1", "belongs_to", "Requirement", "Entity")
    ]
    assert f"[r:{physical}]" in executor.queries[0]
    assert " WHERE " not in executor.queries[0]
    assert edges == [
        {
            "id": "requirement-1-belongs_to-entity-1",
            "source": "requirement-1",
            "target": "entity-1",
            "edge_type": "belongs_to",
            "confidence": 0.9,
        }
    ]
    assert diagnostics["edge_read_status"] == "ok"
    assert diagnostics["edge_tables_failed"] == 0


def test_graph_edges_use_one_optional_read_batch(monkeypatch) -> None:
    physical = "belongs_to__Requirement__Entity"

    class _BatchExecutor(_RelationshipAwareExecutor):
        def __init__(self) -> None:
            super().__init__({physical: []})
            self.batches: list[list[tuple[str, dict[str, Any] | None, int]]] = []

        def execute_read_only_batch(
            self,
            _board_id: str,
            statements: list[tuple[str, dict[str, Any] | None, int]],
        ) -> list[dict[str, Any]]:
            self.batches.append(statements)
            return [{"rows": [["requirement-1", "entity-1", 0.9]]}]

    executor = _BatchExecutor()
    monkeypatch.setattr(kg_routes, "resolve_cypher_executor", lambda: executor)
    monkeypatch.setattr(
        kg_routes,
        "_relation_pairs",
        lambda *_args: [("belongs_to", "Requirement", "Entity")],
    )

    edges, diagnostics = kg_routes._fetch_edges_for_nodes(
        "board-1",
        {"requirement-1"},
    )

    assert len(executor.batches) == 1
    assert executor.queries == []
    assert executor.batches[0][0][2] == 5000
    assert edges[0]["edge_type"] == "belongs_to"
    assert diagnostics["edge_read_status"] == "ok"


def test_failed_batch_retries_per_table_to_preserve_diagnostics(monkeypatch) -> None:
    good = "supports__Decision__Evidence"
    bad = "blocks__Decision__Requirement"

    class _FailingBatchExecutor(_RelationshipAwareExecutor):
        def execute_read_only_batch(self, *_args, **_kwargs):
            raise RuntimeError("batch failed")

    executor = _FailingBatchExecutor({good: [["d1", "e1", 0.8]], bad: []})
    original_execute = executor.execute_read_only

    def execute(board_id, query, params=None, **kwargs):
        if f"[r:{bad}]" in query:
            raise RuntimeError("bad physical table")
        return original_execute(board_id, query, params, **kwargs)

    executor.execute_read_only = execute  # type: ignore[method-assign]
    monkeypatch.setattr(kg_routes, "resolve_cypher_executor", lambda: executor)
    monkeypatch.setattr(
        kg_routes,
        "_relation_pairs",
        lambda *_args: [
            ("supports", "Decision", "Evidence"),
            ("blocks", "Decision", "Requirement"),
        ],
    )

    edges, diagnostics = kg_routes._fetch_edges_for_nodes("board-1", {"d1"})

    assert [edge["edge_type"] for edge in edges] == ["supports"]
    assert diagnostics["edge_read_status"] == "partial_failure"
    assert diagnostics["edge_tables_failed"] == 1
    assert diagnostics["edge_errors"][0]["relationship"] == "blocks"


def test_edge_counts_sum_physical_endpoint_tables_by_logical_name(monkeypatch) -> None:
    first = "belongs_to__Requirement__Entity"
    second = "belongs_to__Decision__Entity"
    executor = _RelationshipAwareExecutor({first: [[2]], second: [[3]]})
    monkeypatch.setattr(kg_routes, "resolve_cypher_executor", lambda: executor)
    monkeypatch.setattr(
        kg_routes,
        "_relation_pairs",
        lambda *_args: [
            ("belongs_to", "Requirement", "Entity"),
            ("belongs_to", "Decision", "Entity"),
        ],
    )

    counts, diagnostics = kg_routes._count_edges_by_type("board-1")

    assert counts == {"belongs_to": 5}
    assert diagnostics["edge_count_status"] == "ok"
    assert diagnostics["edge_count_tables_scanned"] == 2
    assert diagnostics["edge_count_tables_failed"] == 0
    assert f"MATCH (a:Requirement)-[r:{first}]->(b:Entity)" in executor.queries[0]
    assert f"MATCH (a:Decision)-[r:{second}]->(b:Entity)" in executor.queries[1]


def test_edge_counts_use_one_optional_read_batch(monkeypatch) -> None:
    first = "belongs_to__Requirement__Entity"
    second = "belongs_to__Decision__Entity"

    class _BatchExecutor(_RelationshipAwareExecutor):
        def __init__(self) -> None:
            super().__init__({first: [], second: []})
            self.batches = []

        def execute_read_only_batch(self, _board_id, statements):
            self.batches.append(statements)
            return [{"rows": [[2]]}, {"rows": [[3]]}]

    executor = _BatchExecutor()
    monkeypatch.setattr(kg_routes, "resolve_cypher_executor", lambda: executor)
    monkeypatch.setattr(
        kg_routes,
        "_relation_pairs",
        lambda *_args: [
            ("belongs_to", "Requirement", "Entity"),
            ("belongs_to", "Decision", "Entity"),
        ],
    )

    counts, diagnostics = kg_routes._count_edges_by_type("board-1")

    assert counts == {"belongs_to": 5}
    assert len(executor.batches) == 1
    assert executor.queries == []
    assert diagnostics["edge_count_status"] == "ok"


def test_grouped_node_counts_scan_once_and_fill_declared_zeroes(monkeypatch) -> None:
    calls = []

    class _Executor:
        def execute_read_only(self, board_id, query, params, **kwargs):
            calls.append((board_id, query, params, kwargs))
            return {"rows": [["Decision", 2], ["Entity", 3]]}

    class _Service:
        def count_all_nodes(self, *_args, **_kwargs):
            raise AssertionError("the fallback must not run")

    monkeypatch.setattr(kg_routes, "resolve_cypher_executor", lambda: _Executor())

    counts = kg_routes._count_nodes_by_type(
        "board-1",
        ("Decision", "Entity", "Evidence"),
        _Service(),
        min_relevance=0.25,
        graph_layer="canonical",
    )

    assert counts == {"Decision": 2, "Entity": 3, "Evidence": 0}
    assert len(calls) == 1
    assert "RETURN label(n) AS node_type, count(n) AS c" in calls[0][1]
    assert calls[0][2]["min_relevance"] == 0.25
    assert calls[0][3] == {"max_rows": 4}


def test_grouped_node_count_refusal_falls_back_without_weakening_filters(
    monkeypatch,
) -> None:
    observed = []

    class _Executor:
        @staticmethod
        def execute_read_only(*_args, **_kwargs):
            return {"rows": [["Decision", -1]]}

    class _Service:
        def count_all_nodes(self, _board_id, **kwargs):
            observed.append(kwargs)
            return {"Decision": 2, "Entity": 3}[kwargs["node_type"]]

    monkeypatch.setattr(kg_routes, "resolve_cypher_executor", lambda: _Executor())

    counts = kg_routes._count_nodes_by_type(
        "board-1",
        ("Decision", "Entity"),
        _Service(),
        min_relevance=0.4,
        graph_layer="working",
        include_code_traceability=False,
    )

    assert counts == {"Decision": 2, "Entity": 3}
    assert [call["node_type"] for call in observed] == ["Decision", "Entity"]
    assert all(call["min_relevance"] == 0.4 for call in observed)
    assert all(call["graph_layer"] == "working" for call in observed)
    assert all(call["include_code_traceability"] is False for call in observed)


def test_legacy_executor_without_layout_extension_keeps_logical_name() -> None:
    assert (
        kg_routes._relationship_table_name(
            object(),
            "board-1",
            "belongs_to",
            "Requirement",
            "Entity",
        )
        == "belongs_to"
    )
