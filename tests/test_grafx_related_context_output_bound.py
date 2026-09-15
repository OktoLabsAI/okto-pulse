"""Bound output construction without truncating the audited adjacency frontier."""
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.grafx_graph_store import (
    CommunityGrafxGraphStore, _NodeView,
)
from okto_pulse.core.kg.interfaces.graph_store import QueryFilters


def _node(node_id):
    return _NodeView(node_id, "Decision", node_id, "source", 0.9,
                     "canonical", None, None, None)


def _fixture(monkeypatch, degree=10_000, *, initial_null=False):
    store = CommunityGrafxGraphStore(lambda _: None, lambda *_: None)
    inventory = [(_node(f"second:{i}"), "contradicts") for i in range(degree)]
    selected = []
    reader = SimpleNamespace(execute=lambda *_: SimpleNamespace(rows=[[
        "center", "Decision", "center", "source", 0.9, "canonical", None, None, None,
    ]]))
    monkeypatch.setattr(store, "_read", lambda _board, *, operation, callback: callback(reader))

    def adjacent(_reader, node, **kwargs):
        selected.append(node.node_id)
        if node.node_id == "center":
            prefix = [(_node("orphan"), "relates_to")] if initial_null else []
            return prefix + [(_node("first"), "supersedes")]
        if node.node_id == "orphan":
            return []
        assert node.node_id == "first"
        return inventory

    monkeypatch.setattr(store, "_adjacent", adjacent)
    return store, inventory, selected


@pytest.mark.parametrize("initial_null", [False, True])
@pytest.mark.parametrize("limit", [1, 50])
def test_wide_second_hop_only_constructs_remaining_output(monkeypatch, initial_null, limit):
    store, inventory, selected = _fixture(monkeypatch, initial_null=initial_null)
    checked = []
    projected = []
    visible = store._visible_neighbour

    def observe_visibility(node, **kwargs):
        checked.append(node.node_id)
        return visible(node, **kwargs)

    original_get = _NodeView.__getattribute__

    def observe_projection(node, name):
        node_id = original_get(node, "node_id")
        if name == "title" and node_id.startswith("second:"):
            projected.append(node_id)
        return original_get(node, name)

    monkeypatch.setattr(store, "_visible_neighbour", observe_visibility)
    monkeypatch.setattr(_NodeView, "__getattribute__", observe_projection)
    result = store.find_by_artifact("b", "source", QueryFilters(max_rows=limit))
    remaining = limit - int(initial_null)
    expected = ([["center", "center", "orphan", "orphan", None, None, "relates_to", None]]
                if initial_null else [])
    expected += [["center", "center", "first", "first", f"second:{i}", f"second:{i}",
                  "supersedes", "contradicts"] for i in range(remaining)]
    assert result == expected
    assert projected == [f"second:{i}" for i in range(remaining)]
    if remaining:
        assert checked[-len(inventory):] == [node.node_id for node, _ in inventory]
        assert selected == (["center", "orphan", "first"] if initial_null else ["center", "first"])
    else:
        assert checked == ["orphan"]
        assert selected == ["center", "orphan"]


def test_late_visibility_failure_is_not_hidden_by_full_result_prefix(monkeypatch):
    store, inventory, _ = _fixture(monkeypatch)
    visible = store._visible_neighbour

    def refuse_last(node, **kwargs):
        if node is inventory[-1][0]:
            raise ValueError("late visibility refusal")
        return visible(node, **kwargs)

    monkeypatch.setattr(store, "_visible_neighbour", refuse_last)
    with pytest.raises(ValueError, match="late visibility refusal"):
        store.find_by_artifact("b", "source", QueryFilters(max_rows=1))


def test_empty_visible_second_hop_retains_null_extension(monkeypatch):
    store, inventory, _ = _fixture(monkeypatch)
    checked = []

    def invisible_seconds(node, **kwargs):
        checked.append(node.node_id)
        return not node.node_id.startswith("second:")

    monkeypatch.setattr(store, "_visible_neighbour", invisible_seconds)
    assert store.find_by_artifact("b", "source", QueryFilters(max_rows=1)) == [
        ["center", "center", "first", "first", None, None, "supersedes", None],
    ]
    assert len(checked) == len(inventory) + 1
