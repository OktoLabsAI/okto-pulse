"""A Health absence proof must be complete within a bounded observation."""

from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.filesystem_observation import (
    FilesystemObservationBudget, FilesystemObservationLimit,
)
from okto_pulse.community.adapters.grafx_graph_runtime_store import CommunityGrafxGraphRuntimeStore

from okto_pulse.community.adapters.routed_board_graph_composition import (
    build_community_routed_board_graph_composition,
)


def test_health_absence_proof_refuses_oversized_board_namespace(tmp_path):
    root = tmp_path / "graph"
    board_root = root / "boards" / "absent-board"
    board_root.mkdir(parents=True)
    for index in range(2001):
        (board_root / f"unrelated-{index}").write_bytes(b"")
    bundle = build_community_routed_board_graph_composition(settings=SimpleNamespace(
        kg_base_dir=str(root), kg_graph_backend="grafx",
        kg_global_graph_backend="grafx", kg_grafx_page_size=4096,
    ))
    with bundle.graph_health_observation.scope("absent-board"):
        result = bundle.graph_runtime_store.graph_state("absent-board")
    assert result.status == "unavailable"
    # The ordinary complete absence proof remains valid and non-destructive.
    foreground = bundle.graph_runtime_store.graph_state("absent-board")
    assert foreground.status == "absent"
    assert len(list(board_root.iterdir())) == 2001
    assert bundle.grafx_pool.pooled_paths() == ()


def test_missing_primary_never_overrides_incomplete_canonical_absence(tmp_path):
    board_root = tmp_path / "boards" / "board"
    board_root.mkdir(parents=True)
    for index in range(1001):
        (board_root / str(index)).write_bytes(b"")
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board: board_root / "missing-primary",
        lambda _board: pytest.fail("observation closed a handle"),
        lambda *_args: pytest.fail("observation entered mutation fence"),
        board_storage_root_resolver=lambda _board: board_root,
        observation_timeout=lambda _board: 0.3,
    )
    result = store.graph_state("board", generation="generation-1")
    assert result.status == "unavailable"
    assert result.reason_code == "board_graph_absence_observation_unavailable"
    assert result.generation == "generation-1"


def test_metadata_byte_budget_is_aggregate_and_does_not_trust_stat(tmp_path):
    path = tmp_path / "manifest"
    path.write_bytes(b"x" * (1024 * 1024))
    budget = FilesystemObservationBudget(lambda: 0.3)
    for _ in range(4):
        assert len(budget.read_text(path, max_file_bytes=1024 * 1024)) == 1024 * 1024
    with pytest.raises(FilesystemObservationLimit, match="observation_byte_limit"):
        budget.read_text(path, max_file_bytes=1024 * 1024)
    # Even without a reliable prior stat, one read cannot exceed its envelope.
    budget = FilesystemObservationBudget(lambda: 0.3)
    with pytest.raises(FilesystemObservationLimit, match="observation_byte_limit"):
        budget.read_text(path, max_file_bytes=32)


def test_metadata_entry_budget_is_shared_across_directories(tmp_path):
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        for index in range(1001):
            (directory / str(index)).write_bytes(b"")
    budget = FilesystemObservationBudget(lambda: 0.3)
    assert len(budget.children(tmp_path / "first")) == 1001
    with pytest.raises(FilesystemObservationLimit, match="observation_entry_limit"):
        budget.children(tmp_path / "second")


def test_metadata_budget_refuses_expired_or_missing_scope(tmp_path):
    for remaining in (0.0, None):
        budget = FilesystemObservationBudget(lambda: remaining)
        with pytest.raises(FilesystemObservationLimit, match="observation_timeout"):
            budget.children(tmp_path)


def test_absence_proof_expiring_during_final_metadata_step_is_discarded(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import grafx_graph_runtime_store as runtime

    board_root = tmp_path / "boards" / "board"
    board_root.mkdir(parents=True)
    remaining = [0.3]

    def unavailable_path(_board):
        raise OSError("missing binding")

    def finish_absence(_scope, **_kwargs):
        remaining[0] = 0.0
        return False

    monkeypatch.setattr(runtime, "grafx_board_privacy_storage_present", finish_absence)
    store = CommunityGrafxGraphRuntimeStore(
        unavailable_path, lambda _board: None, lambda *_args: None,
        board_storage_root_resolver=lambda _board: board_root,
        observation_timeout=lambda _board: remaining[0],
    )
    assert store.graph_state("board").status == "unavailable"
