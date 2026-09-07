"""Real Grafx coverage for the Pulse related-context filter contract."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.interfaces.graph_store import QueryFilters
from okto_pulse.community.adapters.grafx_relationship_layout import resolve_relationship_table
from okto_pulse.community.adapters.routed_board_graph_facades import CommunityRoutedSemanticGraphStore
from test_grafx_graph_store import BOARD_ID, _attrs, real_store  # noqa: F401


SOURCE = "filter-checkpoint:artifact"


@pytest.fixture(scope="module")
def graph(request):
    store, database, _fence, _path = request.getfixturevalue("real_store")
    for name in ("orphan", "center", "out", "incoming", "second", "working", "revoked", "old"):
        attrs = _attrs(name, SOURCE if name in {"orphan", "center"} else f"filter:{name}", "filter-test")
        if name in {"center", "working"}:
            attrs["graph_layer"] = "working"
        if name == "revoked":
            attrs["revocation_reason"] = "source_deleted"
        if name == "old":
            attrs["superseded_by"] = "filter:out"
        store.create_node(BOARD_ID, "Decision", f"filter:{name}", attrs)
    store.create_node(BOARD_ID, "Decision", "filter:ct-center",
                      _attrs("ct-center", "filter:ct-root", "filter-test"))
    with database.begin("write") as tx:
        tx.execute("CREATE (:Entity {id:'filter:ct',title:'trace',source_artifact_ref:'filter:ct-source',"
                   "source_confidence:0.9,graph_layer:'canonical',kind_of:'code_evidence'})")
        ct_table = resolve_relationship_table("belongs_to", "Decision", "Entity")
        tx.execute("MATCH (a:Decision {id:'filter:ct-center'}),(b:Entity {id:'filter:ct'}) "
                   f"CREATE (a)-[:{ct_table} {{confidence:0.9}}]->(b)")
        for source, target, kind in (
            ("center", "out", "supersedes"),
            ("center", "out", "supersedes"),
            ("incoming", "center", "contradicts"),
            ("out", "second", "contradicts"),
            ("center", "working", "supersedes"),
            ("center", "revoked", "supersedes"),
            ("center", "old", "supersedes"),
        ):
            table = resolve_relationship_table(kind, "Decision", "Decision")
            tx.execute(f"MATCH (a:Decision {{id:$source}}),(b:Decision {{id:$target}}) "
                       f"CREATE (a)-[:{table} {{confidence:0.9}}]->(b)",
                       {"source": f"filter:{source}", "target": f"filter:{target}"})
    return store, database


def read(graph, **kwargs):
    return graph[0].find_by_artifact_filtered(
        BOARD_ID, SOURCE, QueryFilters(min_confidence=0.5, max_rows=50),
        graph_layer="canonical", **kwargs,
    )


@pytest.mark.parametrize("direction,expected", [
    ("outgoing", ["filter:out", "filter:out"]),
    ("incoming", ["filter:incoming"]),
    ("both", ["filter:incoming", "filter:out", "filter:out"]),
])
def test_direction_depth_and_visibility_are_applied(graph, direction, expected):
    rows = read(graph, direction=direction, max_depth=1)
    assert sorted(row[2] for row in rows) == sorted(expected)
    assert all(row[4:6] == [None, None] and row[7] is None for row in rows)
    # The explicit anchor is allowed from another layer; expanded nodes are not.
    assert all(row[0] == "filter:center" for row in rows)


def test_type_and_direction_apply_only_to_first_hop(graph):
    rows = read(graph, rel_types=["supersedes"], direction="outgoing", max_depth=2)
    assert [(r[2], r[4], r[6], r[7]) for r in rows] == [
        ("filter:out", "filter:second", "supersedes", "contradicts"),
    ] * 2


def test_no_visible_second_hop_keeps_null_extension(graph):
    rows = read(graph, direction="incoming", max_depth=2)
    assert len(rows) == 1
    assert rows[0][2:] == ["filter:incoming", "incoming", None, None, "contradicts", None]


def test_result_limit_does_not_cut_off_earlier_isolated_centers(graph):
    rows = graph[0].find_by_artifact_filtered(
        BOARD_ID, SOURCE, QueryFilters(min_confidence=0.5, max_rows=1),
        direction="outgoing", max_depth=1, graph_layer="canonical",
    )
    assert len(rows) == 1 and rows[0][0] == "filter:center"
    rows = graph[0].find_by_artifact(
        BOARD_ID, SOURCE, QueryFilters(min_confidence=0.5, max_rows=1),
        graph_layer="canonical",
    )
    assert len(rows) == 1 and rows[0][0] == "filter:center"


def test_default_filtered_shape_matches_unfiltered(graph):
    filters = QueryFilters(min_confidence=0.5, max_rows=50)
    expected = graph[0].find_by_artifact(BOARD_ID, SOURCE, filters, graph_layer="canonical")
    assert read(graph) == expected
    assert read(graph, rel_types=[]) == expected


def test_superseded_opt_in_and_layer_are_preserved(graph):
    rows = graph[0].find_by_artifact_filtered(
        BOARD_ID, SOURCE, QueryFilters(min_confidence=0.5, max_rows=50, include_superseded=True),
        direction="outgoing", max_depth=1, graph_layer="all",
    )
    assert sorted(row[2] for row in rows) == ["filter:old", "filter:out", "filter:out", "filter:working"]


def test_depth_one_never_expands_a_second_hop(graph, monkeypatch):
    store = graph[0]
    adjacent = store._adjacent
    visited = []

    def observed(reader, node, **kwargs):
        visited.append((node.node_id, kwargs))
        return adjacent(reader, node, **kwargs)

    monkeypatch.setattr(store, "_adjacent", observed)
    assert read(graph, direction="outgoing", rel_types=["supersedes"], max_depth=1)
    assert [node for node, _ in visited] == ["filter:orphan", "filter:center"]
    assert all(options == {"direction": "outgoing", "rel_types": frozenset({"supersedes"})}
               for _, options in visited)


@pytest.mark.parametrize("source", ["filter:ct-root", "filter:ct-source"])
def test_code_traceability_is_filtered_at_center_and_neighbour(graph, source):
    filters = QueryFilters(min_confidence=0.5, max_rows=50)
    assert graph[0].find_by_artifact_filtered(
        BOARD_ID, source, filters, max_depth=1, include_code_traceability=True,
    )
    assert graph[0].find_by_artifact_filtered(
        BOARD_ID, source, filters, max_depth=1, include_code_traceability=False,
    ) == []


def test_core_service_uses_filtered_capability_through_routed_facade(graph, monkeypatch):
    from okto_pulse.core.kg import kg_service, equivalence_fold

    @contextmanager
    def window(_board):
        yield

    facade = CommunityRoutedSemanticGraphStore(None, ladybug=None, grafx=None, operation_window=window)
    facade._provider = lambda _board: graph[0]
    monkeypatch.setattr(kg_service, "_get_graph_store", lambda: facade)
    monkeypatch.setattr(equivalence_fold, "load_equivalence_mapping", lambda _board: {})
    service = kg_service.KGService()
    monkeypatch.setattr(service, "_cached_call", lambda _name, _board, _params, call: call())
    rows = service.get_related_context(
        BOARD_ID, SOURCE, rel_types=["supersedes"], direction="outgoing", max_depth=1,
    )
    assert [row["hop1_id"] for row in rows] == ["filter:out", "filter:out"]
    assert all(row["hop2_id"] is None and row["rel1_type"] == "supersedes" for row in rows)


@pytest.mark.parametrize("kwargs", [
    {"direction": "sideways"}, {"max_depth": 3}, {"max_depth": True},
    {"rel_types": ["supersedes) DELETE n"]}, {"rel_types": ["missing"]},
])
def test_invalid_filter_never_broadens_the_query(graph, monkeypatch, kwargs):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid filters must refuse before graph I/O")

    monkeypatch.setattr(graph[0], "_read", forbidden)
    with pytest.raises(ValueError):
        read(graph, **kwargs)


def test_routed_facade_preserves_operation_window_and_all_filter_arguments():
    events = []

    @contextmanager
    def window(board):
        events.append(("enter", board))
        try:
            yield
        finally:
            events.append(("exit", board))

    def operation(*args, **kwargs):
        assert events == [("enter", BOARD_ID)]
        events.append((args, kwargs))
        return [["kept"]]

    provider = SimpleNamespace(find_by_artifact_filtered=operation)
    facade = CommunityRoutedSemanticGraphStore(None, ladybug=None, grafx=None, operation_window=window)
    facade._provider = lambda board: provider
    filters = QueryFilters(min_confidence=0.5, max_rows=1)
    kwargs = dict(rel_types=["supersedes"], direction="outgoing", max_depth=1,
                  graph_layer="canonical", include_code_traceability=False)
    assert facade.find_by_artifact_filtered(BOARD_ID, SOURCE, filters, **kwargs) == [["kept"]]
    assert events == [("enter", BOARD_ID), ((BOARD_ID, SOURCE, filters), kwargs), ("exit", BOARD_ID)]


def test_missing_filtered_provider_refuses_instead_of_returning_unfiltered_results():
    @contextmanager
    def window(_board):
        yield

    facade = CommunityRoutedSemanticGraphStore(None, ladybug=None, grafx=None, operation_window=window)
    facade._provider = lambda board: SimpleNamespace()
    with pytest.raises(GraphCapabilityUnavailable):
        facade.find_by_artifact_filtered(BOARD_ID, SOURCE, QueryFilters(), max_depth=1)
