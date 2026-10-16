"""Health metadata observations must not enumerate unbounded directories."""

from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from okto_pulse.community.adapters.grafx_graph_runtime_store import CommunityGrafxGraphRuntimeStore
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

from okto_pulse.community.adapters.routed_board_graph_composition import (
    build_community_routed_board_graph_composition,
)


def test_composed_health_board_metadata_refuses_entry_overflow(tmp_path):
    bundle = build_community_routed_board_graph_composition(settings=SimpleNamespace(
        kg_base_dir=str(tmp_path / "graph"), kg_graph_backend="grafx",
        kg_global_graph_backend="grafx", kg_grafx_page_size=4096,
    ))
    try:
        route = bundle.initialize_board_route("metadata-board")
        writer = bundle.grafx_pool.get(route.active_path, page_size=4096)
        writer.checkpoint()
        for index in range(2001):
            (route.active_path / f"observation-fixture-{index}").write_bytes(b"")
        with bundle.graph_health_observation.scope("metadata-board"):
            result = bundle.graph_runtime_store.graph_state("metadata-board")
        assert result.status == "unavailable"
        assert result.reason_code == "board_graph_metadata_entry_limit"
        assert "entry_count" not in result.details
        # Ordinary governance/erasure consumers retain complete observations.
        foreground = bundle.graph_runtime_store.graph_state("metadata-board")
        assert foreground.status == "available"
        assert foreground.details["entry_count"] >= 2001
    finally:
        for pool in (*bundle.grafx_read_pools, bundle.grafx_pool):
            for path in pool.pooled_paths():
                pool.close(path)


@pytest.mark.parametrize("entries, expected", [(2000, "available"), (5000, "unavailable")])
def test_metadata_entry_budget_stops_consumption_without_prefix(tmp_path, monkeypatch, entries, expected):
    (tmp_path / "grafx.meta").write_bytes(b"fixture identity")
    consumed = []
    closed = []

    def enumerate_entries():
        for index in range(entries):
            consumed.append(index)
            yield SimpleNamespace(path=str(tmp_path / str(index)))

    @contextmanager
    def scan(_path):
        try:
            yield enumerate_entries()
        finally:
            closed.append(True)

    import os
    monkeypatch.setattr(os, "scandir", scan)
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board: tmp_path, lambda _board: None, lambda *_args: None,
        board_storage_root_resolver=lambda _board: tmp_path,
        observation_timeout=lambda _board: 0.3,
    )
    result = store.graph_state("board", generation="generation-1")
    assert result.status == expected
    assert result.generation == "generation-1"
    assert len(consumed) == min(entries, 2001)
    assert closed == [True]
    if expected == "available":
        assert result.details["entry_count"] == 2000
    else:
        assert "entry_count" not in result.details


def test_metadata_entry_scan_uses_shared_deadline(tmp_path):
    (tmp_path / "grafx.meta").write_bytes(b"fixture identity")
    for index in range(10):
        (tmp_path / str(index)).write_bytes(b"")
    calls = []

    def remaining(board_id):
        calls.append(board_id)
        if len(calls) == 4:
            raise GraphCapabilityUnavailable("observation deadline exhausted")
        return 0.01

    store = CommunityGrafxGraphRuntimeStore(
        lambda _board: tmp_path, lambda _board: None, lambda *_args: None,
        board_storage_root_resolver=lambda _board: tmp_path,
        observation_timeout=remaining,
    )
    with pytest.raises(GraphCapabilityUnavailable):
        store.graph_state("board")
    assert calls == ["board"] * 4
