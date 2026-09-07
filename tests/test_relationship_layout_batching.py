"""Operation-local layout grouping never becomes a retained route authority."""
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.routed_board_graph_facades import CommunityRoutedCypherExecutor
from okto_pulse.community.api import kg_routes
from test_routed_board_graph_facades import _RouteResolver, _Windows, _snapshot, _missing_binding


LAYOUTS = [("r", "A", "B"), ("s", "B", "C"), ("t", "D", "D")]


def test_facade_acquires_once_per_call_and_reacquires_before_query():
    events = []
    routes = _RouteResolver({"board": _snapshot("board", "grafx", generation="1")}, events)
    provider = SimpleNamespace(
        relationship_table_name=lambda *parts: "__".join(parts),
        execute_read_only=lambda *args, **kw: {"rows": [[1]]},
    )
    facade = CommunityRoutedCypherExecutor(routes, ladybug=provider, grafx=provider,
                                          operation_window=_Windows(events).operation)
    assert facade.relationship_table_names("board", LAYOUTS) == ["__".join(x) for x in LAYOUTS]
    assert routes.acquire_calls == ["board"]
    assert events[0] == ("operation_enter", "board")
    assert events[-1] == ("operation_exit", "board")
    routes.routes["board"] = _missing_binding()
    with pytest.raises(type(_missing_binding())):
        facade.execute_read_only("board", "RETURN 1")
    with pytest.raises(type(_missing_binding())):
        facade.relationship_table_names("board", LAYOUTS)
    assert routes.acquire_calls == ["board"] * 3
    assert events[-1] == ("operation_exit", "board")


def test_facade_provider_switch_and_legacy_names_are_not_cached():
    events = []
    routes = _RouteResolver({"board": _snapshot("board", "grafx", generation="1")}, events)
    facade = CommunityRoutedCypherExecutor(routes, ladybug=SimpleNamespace(),
        grafx=SimpleNamespace(relationship_table_name=lambda *p: "__".join(p)),
        operation_window=_Windows(events).operation)
    assert facade.relationship_table_names("board", LAYOUTS)[0] == "r__A__B"
    routes.routes["board"] = _snapshot("board", "ladybug", generation="2")
    assert facade.relationship_table_names("board", LAYOUTS) == ["r", "s", "t"]
    assert facade.relationship_table_names("unknown", []) == []
    assert routes.acquire_calls == ["board", "board"]


class Executor:
    def __init__(self, behavior="ok", fail_scalar=False):
        self.groups = []
        self.singles = []
        self.queries = []
        self.behavior = behavior
        self.fail_scalar = fail_scalar

    def relationship_table_names(self, board, layouts):
        self.groups.append((board, list(layouts)))
        if self.behavior == "raise":
            raise RuntimeError("route refused")
        if self.behavior == "short":
            return ["incorrect_prefix"]
        if self.behavior == "malformed":
            return [None] * len(layouts)
        return ["__".join(p) for p in layouts]

    def relationship_table_name(self, board, *parts):
        self.singles.append((board, parts))
        if self.fail_scalar:
            raise RuntimeError("route still refused")
        return "__".join(parts)

    def execute_read_only(self, board, query, params=None, **kwargs):
        self.queries.append((query, params, kwargs))
        return {"rows": [[2]] if "count(r)" in query else [["a", "b", 0.9]]}


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(kg_routes, "_relation_pairs", lambda *_: LAYOUTS)
    def use(executor):
        monkeypatch.setattr(kg_routes, "resolve_cypher_executor", lambda: executor)
        return executor
    return use


def test_census_uses_complete_batch_and_preserves_queries(install):
    batch = install(Executor())
    actual = kg_routes._count_edges_by_type("board")
    scalar = install(Executor())
    scalar.relationship_table_names = None
    assert kg_routes._count_edges_by_type("board") == actual
    assert batch.queries == scalar.queries
    assert batch.groups == [("board", LAYOUTS)] and batch.singles == []
    assert len(scalar.singles) == 3


@pytest.mark.parametrize("behavior", ["raise", "short", "malformed"])
def test_complete_batch_refusal_restores_scalar_diagnostics(install, behavior):
    executor = install(Executor(behavior))
    counts, diagnostics = kg_routes._count_edges_by_type("board")
    assert counts == {"r": 2, "s": 2, "t": 2}
    assert diagnostics["edge_count_tables_failed"] == 0
    assert len(executor.singles) == 3
    assert all("incorrect_prefix" not in q for q, *_ in executor.queries)


def test_failed_route_never_falls_back_to_logical_query(install):
    executor = install(Executor("raise", fail_scalar=True))
    counts, diagnostics = kg_routes._count_edges_by_type("board")
    assert counts == {} and diagnostics["edge_count_tables_failed"] == 3
    assert not executor.queries


def test_graph_maps_only_eligible_page_layouts_and_keeps_visibility(install):
    executor = install(Executor())
    _edges, diagnostics = kg_routes._fetch_edges_for_nodes(
        "board", {"a"}, node_types_by_id={"a": "A"})
    assert executor.groups == [("board", LAYOUTS[:1])]
    assert not executor.singles
    assert diagnostics["edge_tables_skipped_by_page_type"] == 2
    assert executor.queries[0][1] == {"from_node_ids": ("a",), "to_node_ids": ()}
    executor.groups.clear()
    executor.queries.clear()
    kg_routes._fetch_edges_for_nodes("board", {"a"}, node_types_by_id={"a": "A"},
                                    include_code_traceability=False)
    assert executor.groups == [("board", LAYOUTS)]
    assert all(p == {"include_code_traceability": False} for _, p, _ in executor.queries)
