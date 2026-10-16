"""Filesystem Health diagnostics must refuse an incomplete size observation."""

from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters import grafx_board_storage as storage
from okto_pulse.community.adapters.grafx_graph_runtime_store import CommunityGrafxGraphRuntimeStore


def test_health_footprint_has_an_entry_budget_and_never_returns_a_prefix(tmp_path):
    graph = tmp_path / "graph"
    graph.mkdir()
    for index in range(2001):
        (graph / f"part-{index}").write_bytes(b"x")
    effects = []
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board: graph,
        lambda _board: effects.append("close"),
        lambda *_args: effects.append("fence"),
        board_storage_root_resolver=lambda _board: tmp_path,
    )
    result = store.footprint("board")
    assert result.status == "unavailable"
    assert result.total_bytes is None
    assert result.unavailable_reason in {"observation_entry_limit", "observation_timeout"}
    assert effects == []
    assert len(list(graph.iterdir())) == 2001


def test_exact_entry_budget_keeps_complete_size(tmp_path):
    (tmp_path / "first").write_bytes(b"abc")
    (tmp_path / "second").write_bytes(b"de")
    assert storage.grafx_directory_size(tmp_path, max_entries=2) == 5
    with pytest.raises(storage.GrafxDirectoryObservationLimit) as failure:
        storage.grafx_directory_size(tmp_path, max_entries=1)
    assert failure.value.reason == "observation_entry_limit"


def test_depth_budget_refuses_instead_of_measuring_a_prefix(tmp_path):
    leaf = tmp_path / "one" / "two" / "three"
    leaf.mkdir(parents=True)
    (leaf / "data").write_bytes(b"abc")
    with pytest.raises(storage.GrafxDirectoryObservationLimit) as failure:
        storage.grafx_directory_size(tmp_path, max_depth=2)
    assert failure.value.reason == "observation_depth_limit"


def test_deadline_expiration_discards_observed_size(tmp_path, monkeypatch):
    (tmp_path / "first").write_bytes(b"abc")
    clock = iter([100.0, 100.0, 100.0, 100.2])
    monkeypatch.setattr(storage, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(storage.GrafxDirectoryObservationLimit) as failure:
        storage.grafx_directory_size(tmp_path, timeout_seconds=0.15)
    assert failure.value.reason == "observation_timeout"
