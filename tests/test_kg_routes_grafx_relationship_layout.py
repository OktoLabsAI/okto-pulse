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
