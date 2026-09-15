"""Independent serial oracles for the composed Pulse operations on real Grafx."""

from collections import Counter
from contextlib import contextmanager
from statistics import median
from time import perf_counter
from unittest.mock import AsyncMock

import pytest

from okto_pulse.community.adapters import grafx_graph_store as adapter
from okto_pulse.community.adapters.grafx_relationship_layout import (
    resolve_relationship_table,
)
from test_grafx_composed_reads import ObservedReader
from test_grafx_graph_store import BOARD_ID, _attrs, real_store  # noqa: F401


@pytest.fixture(scope="module")
def composed_graph(request):
    store, db, _fence, _path = request.getfixturevalue("real_store")
    names = [
        "center",
        "left",
        "right",
        "diamond",
        "tail",
        "revoked",
        "hidden",
        "old",
        "oldhidden",
    ]
    for name in names:
        attrs = _attrs(name, f"composed:{name}", "composed-session")
        if name == "revoked":
            attrs["revocation_reason"] = "source_deleted"
        if name == "old":
            attrs["superseded_by"] = "composed:tail"
        store.create_node(BOARD_ID, "Decision", f"composed:{name}", attrs)
    # Same business ID in two native node tables must remain two identities.
    store.create_node(
        BOARD_ID,
        "Entity",
        "composed:left",
        _attrs("entity", "composed:entity", "composed-session"),
    )
    with db.begin("write") as tx:
        table = resolve_relationship_table("supersedes", "Decision", "Decision")
        for source, target in [
            ("center", "left"),
            ("center", "left"),
            ("center", "right"),
            ("center", "center"),
            ("center", "revoked"),
            ("center", "old"),
            ("left", "diamond"),
            ("right", "diamond"),
            ("diamond", "center"),
            ("diamond", "tail"),
            ("revoked", "hidden"),
            ("old", "oldhidden"),
        ]:
            tx.execute(
                f"MATCH(a:Decision {{id:$a}}),(b:Decision {{id:$b}}) CREATE(a)-[:{table}]->(b)",
                {"a": f"composed:{source}", "b": f"composed:{target}"},
            )
        table = resolve_relationship_table("belongs_to", "Decision", "Entity")
        tx.execute(
            f"MATCH(a:Decision {{id:'composed:center'}}),(b:Entity {{id:'composed:left'}}) "
            f"CREATE(a)-[:{table}]->(b)"
        )
    return store, db


def serial_adjacent(store, reader, node, *, rel_types=None, direction="both"):
    """Previous branch-per-execute algorithm; never calls the new helper."""
    projection = (
        "neighbor.id, neighbor.title, neighbor.source_artifact_ref, "
        "neighbor.source_confidence, neighbor.graph_layer, "
        "neighbor.superseded_by, neighbor.revocation_reason, neighbor.kind_of"
    )
    answer = []
    for entry in store._incident_entries(node.node_type):
        if rel_types is not None and entry.logical_type not in rel_types:
            continue
        if entry.from_type == node.node_type and direction != "incoming":
            result = reader.execute(
                f"MATCH(center:{entry.from_type})-[r:{entry.physical_table}]->"
                f"(neighbor:{entry.to_type}) WHERE center.id=$node_id RETURN {projection}",
                {"node_id": node.node_id},
            )
            answer.extend(
                (adapter._node_view(row, node_type=entry.to_type), entry.logical_type)
                for row in adapter._rows(result)
            )
        if entry.to_type == node.node_type and direction != "outgoing":
            result = reader.execute(
                f"MATCH(neighbor:{entry.from_type})-[r:{entry.physical_table}]->"
                f"(center:{entry.to_type}) WHERE center.id=$node_id RETURN {projection}",
                {"node_id": node.node_id},
            )
            answer.extend(
                (adapter._node_view(row, node_type=entry.from_type), entry.logical_type)
                for row in adapter._rows(result)
            )
    return answer


@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
@pytest.mark.parametrize("rel_types", [None, frozenset({"supersedes"}), frozenset()])
def test_exact_serial_adjacency_equivalence_and_bounded_calls(
    composed_graph, direction, rel_types, record_property
):
    store, db = composed_graph
    node = adapter._node_view(
        ["composed:center", "center", None, 0.9, "canonical", None, "", "semantic"],
        node_type="Decision",
    )
    with db.begin("read") as tx:
        serial, composed = ObservedReader(tx), ObservedReader(tx)
        expected = serial_adjacent(
            store, serial, node, direction=direction, rel_types=rel_types
        )
        actual = store._adjacent(
            composed, node, direction=direction, rel_types=rel_types
        )
        assert actual == expected
        assert len(composed.calls) == (len(serial.calls) + 15) // 16
        record_property("serial_queries", len(serial.calls))
        record_property("composed_queries", len(composed.calls))
        if direction == "both" and rel_types is None:
            counts = Counter((n.node_type, n.node_id, kind) for n, kind in actual)
            assert counts[("Decision", "composed:left", "supersedes")] == 2
            assert counts[("Entity", "composed:left", "belongs_to")] == 1
            assert counts[("Decision", "composed:center", "supersedes")] == 2


def serial_chain(reader, node_id, max_depth):
    table = resolve_relationship_table("supersedes", "Decision", "Decision")
    answer, frontier, seen = [], [node_id], {node_id}
    for _depth in range(max_depth):
        following = []
        for current in frontier:
            rows = adapter._rows(
                reader.execute(
                    f"MATCH(current:Decision)-[r:{table}]->(next:Decision) "
                    f"WHERE current.id=$current_id AND {adapter.tpl.active_read_filter_clause('current')} "
                    f"AND {adapter.tpl.active_read_filter_clause('next')} "
                    "RETURN next.id,next.title,next.created_at,next.superseded_by,next.superseded_at",
                    {"current_id": current},
                )
            )
            for row in rows:
                if type(row[0]) is str and row[0] not in seen:
                    seen.add(row[0])
                    following.append(row[0])
                    answer.append(row)
        if not following:
            break
        frontier = following
    return answer


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 100])
def test_supersedence_exact_bfs_order_depth_dedup_and_active_filter(
    composed_graph, monkeypatch, depth
):
    store, db = composed_graph
    with db.begin("read") as tx:
        serial, composed = ObservedReader(tx), ObservedReader(tx)
        expected = serial_chain(serial, "composed:center", depth)
        monkeypatch.setattr(
            store, "_read", lambda _board, *, operation, callback: callback(composed)
        )
        actual = store.traverse_supersedence(
            BOARD_ID, "composed:center", max_depth=depth
        )
        assert actual == expected
        ids = [row[0] for row in actual]
        assert len(ids) == len(set(ids))
        assert not set(ids).intersection(
            {f"composed:{x}" for x in ["center", "revoked", "hidden"]}
        )
        # History deliberately keeps superseded decisions; active-read excludes
        # permanent tombstones, not superseded_by (Core cypher_templates.py).
        if depth:
            assert "composed:old" in ids
        if depth >= 2:
            assert "composed:oldhidden" in ids
        assert len(composed.calls) == min(depth, 4)
        if depth >= 2:
            assert len(composed.calls) < len(serial.calls)
        if depth >= 3:
            assert set(ids) == {
                f"composed:{x}"
                for x in ["left", "right", "old", "diamond", "oldhidden", "tail"]
            }


def test_warm_adjacency_cost_observation(composed_graph, record_property):
    """Record a small fixture observation, not a performance acceptance gate."""
    store, db = composed_graph
    node = adapter._node_view(
        ["composed:center", "center", None, 0.9, "canonical", None, "", "semantic"],
        node_type="Decision",
    )
    serial_ms, composed_ms = [], []
    with db.begin("read") as tx:
        expected = serial_adjacent(store, tx, node)
        assert store._adjacent(tx, node) == expected
        for iteration in range(6):
            cases = [
                (serial_adjacent, serial_ms),
                (lambda s, r, n: s._adjacent(r, n), composed_ms),
            ]
            for operation, timings in cases if iteration % 2 else reversed(cases):
                started = perf_counter()
                assert operation(store, tx, node) == expected
                timings.append((perf_counter() - started) * 1000)
    record_property("serial_warm_median_ms", round(median(serial_ms), 3))
    record_property("composed_warm_median_ms", round(median(composed_ms), 3))


def test_rest_supersedence_uses_real_core_routed_store_and_native_engine(
    composed_graph, monkeypatch
):
    """Transport + service + adapter; ACL denial is covered by its separate suite."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from okto_pulse.community.adapters.routed_board_graph_facades import (
        CommunityRoutedSemanticGraphStore,
    )
    from okto_pulse.community.api import kg_routes
    from okto_pulse.core.application.use_cases.base import ActorContext
    from okto_pulse.core.kg import kg_service

    store, _db = composed_graph
    events = []

    @contextmanager
    def window(board):
        assert board == BOARD_ID
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    facade = CommunityRoutedSemanticGraphStore(
        None, grafx=None, operation_window=window
    )
    facade._provider = lambda _board: store
    monkeypatch.setattr(kg_service, "_get_graph_store", lambda: facade)
    monkeypatch.setattr(kg_routes, "get_kg_service", kg_service.KGService)
    admitted = AsyncMock()
    monkeypatch.setattr(kg_routes, "_require_kg_operation", admitted)
    app = FastAPI()
    app.include_router(kg_routes.router, prefix="/api/v1")
    app.dependency_overrides[kg_routes.require_kg_board_actor] = lambda: ActorContext(
        "test-user", "rest"
    )
    app.dependency_overrides[kg_routes.get_unit_of_work] = lambda: object()
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/kg/boards/{BOARD_ID}/supersedence/composed:right"
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [row["id"] for row in payload["chain"]] == [
        "composed:diamond",
        "composed:center",
        "composed:left",
    ]
    assert payload["depth"] == 3 and payload["current_active"] == "composed:right"
    assert all(
        row["created_at"].startswith("2026-08-28T00:00:00") for row in payload["chain"]
    )
    assert events and events == ["enter", "exit"] * (len(events) // 2)
    admitted.assert_awaited_once()
    assert admitted.call_args.kwargs["operation"] == "kg.query.supersedence_chain"
