"""M-PULSE-7 binding compare-and-swap contract."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphUnavailable,
)

import okto_pulse.community.adapters.graph_backend_binding as binding_module
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
    GraphBindingCompareAndSwapConflict,
)


class _FakeGrafxDatabase:
    def __init__(self, path: Path, *, page_size: int = 8192) -> None:
        self.path = str(path)
        self.identity = SimpleNamespace(page_size=page_size)
        self.mutations = 0


def _ladybug_database(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ladybug")
    return path


def _grafx_database(path: Path, *, page_size: int = 8192) -> _FakeGrafxDatabase:
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.grafx").write_bytes(b"grafx")
    return _FakeGrafxDatabase(path, page_size=page_size)


def _initial_board_binding(
    store: CommunityGraphBackendBindingStore,
    *,
    board_id: str = "board-1",
):
    path = _ladybug_database(store.board_ladybug_path(board_id))
    return store.initialize_board_binding(
        board_id=board_id,
        backend="ladybug",
        generation="ladybug-source",
        physical_path=path,
    )


def test_board_binding_cas_publishes_admitted_grafx_and_fsyncs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    source = _initial_board_binding(store)
    grafx_path = store.board_grafx_path("board-1", "grafx-candidate")
    database = _grafx_database(grafx_path)
    fsynced: list[Path] = []
    monkeypatch.setattr(binding_module, "fsync_directory", fsynced.append)

    published = store.compare_and_swap_board_binding(
        board_id="board-1",
        expected_binding_sha256=source.binding_sha256,
        backend="grafx",
        generation="grafx-candidate",
        physical_path=grafx_path,
        page_size=8192,
        database=database,
    )

    assert published == store.acquire_board_binding("board-1")
    assert published.backend == "grafx"
    assert published.physical_path == grafx_path
    assert published.binding_sha256 != source.binding_sha256
    assert source.physical_path.read_bytes() == b"ladybug"
    assert fsynced == [source.physical_path.parent]

    document = json.loads(
        (source.physical_path.parent / "graph_backend_binding.json").read_text(
            encoding="utf-8"
        )
    )
    assert document["binding_sha256"] == published.binding_sha256


def test_board_candidate_digest_is_certified_without_publishing_binding(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    source = _initial_board_binding(store)
    binding_path = source.physical_path.parent / "graph_backend_binding.json"
    original_document = binding_path.read_bytes()
    grafx_path = store.board_grafx_path("board-1", "grafx-candidate")
    database = _grafx_database(grafx_path)

    candidate = store.prepare_board_binding_candidate(
        board_id="board-1",
        backend="grafx",
        generation="grafx-candidate",
        physical_path=grafx_path,
        page_size=8192,
        database=database,
    )

    assert candidate.backend == "grafx"
    assert candidate.physical_path == grafx_path
    assert len(candidate.binding_sha256) == 64
    assert binding_path.read_bytes() == original_document
    assert store.acquire_board_binding("board-1") == source

    published = store.compare_and_swap_board_binding(
        board_id="board-1",
        expected_binding_sha256=source.binding_sha256,
        backend="grafx",
        generation="grafx-candidate",
        physical_path=grafx_path,
        page_size=8192,
        database=database,
    )
    assert published == candidate


def test_global_binding_cas_uses_the_same_admission_and_authenticated_readback(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    source_path = _ladybug_database(store.global_ladybug_path())
    source = store.initialize_global_binding(
        backend="ladybug",
        generation="ladybug-source",
        physical_path=source_path,
    )
    grafx_path = store.global_grafx_path("grafx-candidate")
    database = _grafx_database(grafx_path, page_size=4096)

    published = store.compare_and_swap_global_binding(
        expected_binding_sha256=source.binding_sha256,
        backend="grafx",
        generation="grafx-candidate",
        physical_path=grafx_path,
        page_size=4096,
        database=database,
    )

    assert published == store.acquire_global_binding()
    assert published.backend == "grafx"
    assert published.page_size == 4096
    assert source_path.read_bytes() == b"ladybug"


def test_stale_binding_cas_fails_closed_with_a_specific_conflict(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    source = _initial_board_binding(store)
    binding_path = source.physical_path.parent / "graph_backend_binding.json"
    original_document = binding_path.read_bytes()
    grafx_path = store.board_grafx_path("board-1", "grafx-candidate")
    database = _grafx_database(grafx_path)

    with pytest.raises(GraphBindingCompareAndSwapConflict) as captured:
        store.compare_and_swap_board_binding(
            board_id="board-1",
            expected_binding_sha256="0" * 64,
            backend="grafx",
            generation="grafx-candidate",
            physical_path=grafx_path,
            page_size=8192,
            database=database,
        )

    assert captured.value.code == "graph_binding_compare_and_swap_conflict"
    assert captured.value.retryable is False
    assert captured.value.details == {
        "operation": "compare_and_swap_graph_backend_binding",
        "reason": "binding_compare_and_swap_stale",
        "scope": "board",
        "scope_id": "board-1",
        "expected_binding_sha256": "0" * 64,
        "observed_binding_sha256": source.binding_sha256,
    }
    assert binding_path.read_bytes() == original_document
    assert store.acquire_board_binding("board-1") == source


def test_concurrent_binding_cas_has_exactly_one_winner(tmp_path: Path) -> None:
    initial_store = CommunityGraphBackendBindingStore(tmp_path)
    source = _initial_board_binding(initial_store)
    candidates = []
    for generation in ("grafx-a", "grafx-b"):
        path = initial_store.board_grafx_path("board-1", generation)
        candidates.append((generation, path, _grafx_database(path)))
    start = Barrier(2)

    def compete(candidate):
        generation, path, database = candidate
        store = CommunityGraphBackendBindingStore(tmp_path)
        start.wait(timeout=5)
        try:
            published = store.compare_and_swap_board_binding(
                board_id="board-1",
                expected_binding_sha256=source.binding_sha256,
                backend="grafx",
                generation=generation,
                physical_path=path,
                page_size=8192,
                database=database,
            )
        except GraphBindingCompareAndSwapConflict:
            return "stale", generation, None
        return "published", generation, published.binding_sha256

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(compete, candidates))

    assert sorted(result[0] for result in results) == ["published", "stale"]
    winner = next(result for result in results if result[0] == "published")
    persisted = initial_store.acquire_board_binding("board-1")
    assert persisted.generation == winner[1]
    assert persisted.binding_sha256 == winner[2]


def test_binding_cas_rejects_invalid_target_and_failed_grafx_admission(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    source = _initial_board_binding(store)
    binding_path = source.physical_path.parent / "graph_backend_binding.json"
    original_document = binding_path.read_bytes()

    with pytest.raises(GraphCapabilityUnavailable) as invalid_expected_digest:
        store.compare_and_swap_board_binding(
            board_id="board-1",
            expected_binding_sha256="not-a-sha256",
            backend="ladybug",
            generation="ladybug-candidate",
            physical_path=source.physical_path,
        )
    assert (
        invalid_expected_digest.value.details["reason"]
        == "expected_binding_sha256_invalid"
    )

    wrong_generation_path = store.board_grafx_path("board-1", "other-generation")
    wrong_generation_database = _grafx_database(wrong_generation_path)
    with pytest.raises(GraphCapabilityUnavailable) as invalid_target:
        store.compare_and_swap_board_binding(
            board_id="board-1",
            expected_binding_sha256=source.binding_sha256,
            backend="grafx",
            generation="grafx-candidate",
            physical_path=wrong_generation_path,
            page_size=8192,
            database=wrong_generation_database,
        )
    assert invalid_target.value.details["reason"] == "binding_argument_invalid"

    unsafe_path = store.board_grafx_path("board-1", "grafx-unsafe")
    unsafe_database = _grafx_database(unsafe_path, page_size=2048)
    with pytest.raises(GraphCapabilityUnavailable) as failed_admission:
        store.compare_and_swap_board_binding(
            board_id="board-1",
            expected_binding_sha256=source.binding_sha256,
            backend="grafx",
            generation="grafx-unsafe",
            physical_path=unsafe_path,
            page_size=8192,
            database=unsafe_database,
        )
    assert (
        failed_admission.value.details["reason"]
        == "grafx_page_size_below_pulse_minimum"
    )
    assert unsafe_database.mutations == 0
    assert binding_path.read_bytes() == original_document
    assert store.acquire_board_binding("board-1") == source


def test_binding_cas_requires_exact_authenticated_readback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    source = _initial_board_binding(store)
    binding_path = source.physical_path.parent / "graph_backend_binding.json"
    original_document = binding_path.read_bytes()

    monkeypatch.setattr(
        CommunityGraphBackendBindingStore,
        "_write_json_atomic",
        staticmethod(lambda path, body: None),
    )
    with pytest.raises(GraphCorruption) as captured:
        store.compare_and_swap_board_binding(
            board_id="board-1",
            expected_binding_sha256=source.binding_sha256,
            backend="ladybug",
            generation="ladybug-candidate",
            physical_path=source.physical_path,
        )

    assert captured.value.details["reason"] == "binding_readback_mismatch"
    assert binding_path.read_bytes() == original_document


def test_binding_cas_replace_failure_preserves_the_previous_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    source = _initial_board_binding(store)
    binding_path = source.physical_path.parent / "graph_backend_binding.json"
    original_document = binding_path.read_bytes()

    def refuse_replace(source_path: Path, destination_path: Path) -> None:
        del source_path, destination_path
        raise OSError("injected replace failure")

    monkeypatch.setattr(binding_module.os, "replace", refuse_replace)
    with pytest.raises(GraphUnavailable) as captured:
        store.compare_and_swap_board_binding(
            board_id="board-1",
            expected_binding_sha256=source.binding_sha256,
            backend="ladybug",
            generation="ladybug-candidate",
            physical_path=source.physical_path,
        )

    assert (
        captured.value.details["reason"]
        == "binding_compare_and_swap_publication_failed"
    )
    assert binding_path.read_bytes() == original_document
    assert list(binding_path.parent.glob(".*.tmp")) == []
