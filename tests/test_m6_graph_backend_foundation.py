"""M-PULSE-6 foundation: settings, immutable bindings, and Grafx admission."""

from __future__ import annotations

import json
import tomllib
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphUnavailable,
)
from pydantic import ValidationError

import okto_pulse.community.adapters.graph_backend_binding as binding_module
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
    admit_grafx_database,
)
from okto_pulse.community.config import CommunitySettings


class _FakeGrafxDatabase:
    def __init__(
        self,
        path: Path,
        *,
        page_size: int,
        descriptor_revalidation: str = "strict",
    ) -> None:
        self.path = str(path)
        self.identity = SimpleNamespace(page_size=page_size)
        self.descriptor_revalidation = descriptor_revalidation
        self.mutations = 0


def _legacy_database(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"legacy")
    return path


def _grafx_database(path: Path, *, page_size: int = 8192) -> _FakeGrafxDatabase:
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.grafx").write_bytes(b"grafx")
    return _FakeGrafxDatabase(path, page_size=page_size)


def _native_directory(path: Path) -> Path:
    _grafx_database(path)
    return path


def test_settings_default_to_grafx_and_safe_grafx_geometry(tmp_path: Path) -> None:
    settings = CommunitySettings(data_dir=str(tmp_path), _env_file=None)

    assert settings.kg_graph_backend == "grafx"
    assert settings.kg_global_graph_backend == "grafx"
    assert settings.kg_grafx_page_size == 8192
    assert settings.kg_grafx_descriptor_revalidation == "generation"


@pytest.mark.parametrize("page_size", [512, 2048, 4095, 5000, 65536])
def test_settings_reject_unsafe_or_invalid_grafx_page_sizes(
    tmp_path: Path,
    page_size: int,
) -> None:
    with pytest.raises(ValidationError):
        CommunitySettings(
            data_dir=str(tmp_path),
            kg_grafx_page_size=page_size,
            _env_file=None,
        )


def test_settings_reject_unknown_backends(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        CommunitySettings(
            data_dir=str(tmp_path),
            kg_graph_backend="automatic",
            _env_file=None,
        )


@pytest.mark.parametrize("mode", ["always", "GENERATION", "", None, 1])
def test_settings_reject_unknown_grafx_descriptor_revalidation_modes(
    tmp_path: Path, mode: object
) -> None:
    with pytest.raises(ValidationError):
        CommunitySettings(
            data_dir=str(tmp_path),
            kg_grafx_descriptor_revalidation=mode,  # type: ignore[arg-type]
            _env_file=None,
        )


def test_community_dependency_pins_the_release_candidate_exactly() -> None:
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert "okto-grafx[accel]==0.0.7" in project["project"]["dependencies"]


def test_missing_binding_fails_closed_without_creating_state(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        store.acquire_board_binding("board-1")

    assert captured.value.details["reason"] == "binding_missing"
    assert list(tmp_path.iterdir()) == []


def test_board_native_binding_is_durable_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    native_path = _native_directory(store.board_grafx_path("board-1", "generation-1"))

    first = store.initialize_board_binding(
        board_id="board-1",
        backend="grafx",
        generation="generation-1",
        physical_path=native_path,
        database=_FakeGrafxDatabase(native_path, page_size=8192),
    )
    second = store.initialize_board_binding(
        board_id="board-1",
        backend="grafx",
        generation="generation-1",
        physical_path=native_path,
        database=_FakeGrafxDatabase(native_path, page_size=8192),
    )

    assert first == second == store.acquire_board_binding("board-1")
    assert first.backend == "grafx"
    assert first.page_size == 8192
    assert first.physical_path == native_path
    with pytest.raises(FrozenInstanceError):
        first.backend = "grafx"  # type: ignore[misc]

    document = json.loads(
        (native_path.parent.parent / "graph_backend_binding.json").read_text(encoding="utf-8")
    )
    assert document["physical_path"] == "boards/board-1/grafx/generation-1"
    assert len(document["binding_sha256"]) == 64


def test_binding_refuses_rebind_without_explicit_cas(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    native_path = _native_directory(store.board_grafx_path("board-1", "generation-1"))
    store.initialize_board_binding(
        board_id="board-1",
        backend="grafx",
        generation="generation-1",
        physical_path=native_path,
        database=_FakeGrafxDatabase(native_path, page_size=8192),
    )
    grafx_path = store.board_grafx_path("board-1", "generation-2")
    database = _grafx_database(grafx_path)

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        store.initialize_board_binding(
            board_id="board-1",
            backend="grafx",
            generation="generation-2",
            physical_path=grafx_path,
            page_size=8192,
            database=database,
        )

    assert captured.value.details["reason"] == "binding_conflict"
    assert store.acquire_board_binding("board-1").backend == "grafx"


def test_global_binding_is_separate_from_each_board(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    board_path = _native_directory(store.board_grafx_path("board-1", "generation-1"))
    global_path = _native_directory(store.global_grafx_path("generation-1"))

    board = store.initialize_board_binding(
        board_id="board-1",
        backend="grafx",
        generation="generation-1",
        physical_path=board_path,
        database=_FakeGrafxDatabase(board_path, page_size=8192),
    )
    global_binding = store.initialize_global_binding(
        backend="grafx",
        generation="generation-1",
        physical_path=global_path,
        database=_FakeGrafxDatabase(global_path, page_size=8192),
    )

    assert board.scope == "board"
    assert global_binding.scope == "global"
    assert board.binding_sha256 != global_binding.binding_sha256
    assert board.physical_path != global_binding.physical_path
    assert store.acquire_board_binding("board-1") == board
    assert store.acquire_global_binding() == global_binding


def test_grafx_binding_requires_admission_before_publication(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    grafx_path = store.board_grafx_path("board-1", "generation-1")
    unsafe_database = _grafx_database(grafx_path, page_size=2048)

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        store.initialize_board_binding(
            board_id="board-1",
            backend="grafx",
            generation="generation-1",
            physical_path=grafx_path,
            page_size=8192,
            database=unsafe_database,
        )

    assert captured.value.details["reason"] == "grafx_page_size_below_pulse_minimum"
    assert unsafe_database.mutations == 0
    assert not (grafx_path.parent.parent / "graph_backend_binding.json").exists()


def test_grafx_binding_persists_and_revalidates_page_geometry(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    grafx_path = store.global_grafx_path("generation-1")
    database = _grafx_database(grafx_path, page_size=4096)

    binding = store.initialize_global_binding(
        backend="grafx",
        generation="generation-1",
        physical_path=grafx_path,
        page_size=4096,
        database=database,
    )
    admission = store.admit_database(
        binding,
        database,
        operation="bootstrap_global_grafx_schema",
    )

    assert binding.page_size == 4096
    assert admission.page_size == 4096
    assert admission.minimum_page_size == 4096
    assert store.acquire_global_binding() == binding


def test_binding_admits_the_real_grafx_persisted_identity(tmp_path: Path) -> None:
    from okto_grafx import connect

    store = CommunityGraphBackendBindingStore(tmp_path)
    grafx_path = store.board_grafx_path("board-real", "generation-real")
    database = connect(grafx_path, page_size=4096)
    try:
        binding = store.initialize_board_binding(
            board_id="board-real",
            backend="grafx",
            generation="generation-real",
            physical_path=grafx_path,
            page_size=4096,
            database=database,
        )
    finally:
        database.close()

    assert binding.page_size == 4096
    assert binding.physical_path == grafx_path
    assert store.acquire_board_binding("board-real") == binding


def test_admission_rejects_config_and_database_path_mismatch_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "grafx-a"
    database = _grafx_database(path, page_size=8192)

    database.identity.page_size = 5000
    with pytest.raises(GraphCapabilityUnavailable) as malformed_identity:
        admit_grafx_database(
            database,
            expected_page_size=8192,
            expected_path=path,
            operation="ensure_current_grafx_board_schema",
        )
    assert (
        malformed_identity.value.details["reason"]
        == "grafx_persisted_page_size_invalid"
    )
    database.identity.page_size = 8192

    with pytest.raises(GraphCapabilityUnavailable) as page_mismatch:
        admit_grafx_database(
            database,
            expected_page_size=4096,
            expected_path=path,
            operation="ensure_current_grafx_board_schema",
        )
    assert (
        page_mismatch.value.details["reason"]
        == "grafx_page_size_configuration_mismatch"
    )

    with pytest.raises(GraphCapabilityUnavailable) as path_mismatch:
        admit_grafx_database(
            database,
            expected_page_size=8192,
            expected_path=tmp_path / "grafx-b",
            operation="ensure_current_grafx_board_schema",
        )
    assert path_mismatch.value.details["reason"] == "grafx_database_path_mismatch"
    assert database.mutations == 0


def test_admission_rejects_descriptor_revalidation_mismatch_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "grafx"
    database = _grafx_database(path)

    with pytest.raises(GraphCapabilityUnavailable) as mismatch:
        admit_grafx_database(
            database,
            expected_page_size=8192,
            expected_descriptor_revalidation="generation",
            expected_path=path,
            operation="grafx_database_pool_get",
        )

    assert (
        mismatch.value.details["reason"]
        == "grafx_descriptor_revalidation_configuration_mismatch"
    )
    assert database.mutations == 0


def test_binding_rejects_tampering_and_missing_physical_database(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    native_path = _native_directory(store.board_grafx_path("board-1", "generation-1"))
    store.initialize_board_binding(
        board_id="board-1",
        backend="grafx",
        generation="generation-1",
        physical_path=native_path,
        database=_FakeGrafxDatabase(native_path, page_size=8192),
    )
    binding_path = native_path.parent.parent / "graph_backend_binding.json"
    document = json.loads(binding_path.read_text(encoding="utf-8"))
    document["generation"] = "generation-tampered"
    binding_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(GraphCorruption) as tampered:
        store.acquire_board_binding("board-1")
    assert tampered.value.details["reason"] == "binding_document_invalid"

    binding_path.unlink()
    store.initialize_board_binding(
        board_id="board-1",
        backend="grafx",
        generation="generation-1",
        physical_path=native_path,
        database=_FakeGrafxDatabase(native_path, page_size=8192),
    )
    (native_path / "meta.grafx").unlink()
    native_path.rmdir()
    inspected = store.inspect_board_binding("board-1")
    assert inspected.backend == "grafx"
    assert inspected.physical_path == native_path
    with pytest.raises(GraphUnavailable) as missing:
        store.acquire_board_binding("board-1")
    assert missing.value.details["reason"] == "physical_database_missing"


def test_global_binding_inspection_survives_missing_database_but_not_tampering(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    global_path = _native_directory(store.global_grafx_path("generation-1"))
    expected = store.initialize_global_binding(
        backend="grafx",
        generation="generation-1",
        physical_path=global_path,
        database=_FakeGrafxDatabase(global_path, page_size=8192),
    )
    (global_path / "meta.grafx").unlink()
    global_path.rmdir()

    assert store.inspect_global_binding() == expected
    with pytest.raises(GraphUnavailable) as missing:
        store.acquire_global_binding()
    assert missing.value.details["reason"] == "physical_database_missing"

    binding_path = global_path.parent.parent / "graph_backend_binding.json"
    document = json.loads(binding_path.read_text(encoding="utf-8"))
    document["generation"] = "tampered"
    binding_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(GraphCorruption):
        store.inspect_global_binding()


def test_binding_rejects_unsafe_ids_and_cross_backend_paths(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)

    with pytest.raises(GraphCapabilityUnavailable):
        store.acquire_board_binding("../outside")
    with pytest.raises(GraphCapabilityUnavailable):
        store.acquire_board_binding("CON")

    wrong_path = _legacy_database(
        tmp_path / "boards" / "board-1" / "grafx" / "generation-1" / "db.lbug"
    )
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        store.initialize_board_binding(
            board_id="board-1",
            backend="ladybug",
            generation="generation-1",
            physical_path=wrong_path,
        )
    assert captured.value.details["reason"] == "binding_argument_invalid"


def test_atomic_publication_fsyncs_and_replace_failure_leaves_no_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    native_path = _native_directory(store.board_grafx_path("board-1", "generation-1"))
    fsynced: list[Path] = []
    monkeypatch.setattr(binding_module, "fsync_directory", fsynced.append)

    store.initialize_board_binding(
        board_id="board-1",
        backend="grafx",
        generation="generation-1",
        physical_path=native_path,
        database=_FakeGrafxDatabase(native_path, page_size=8192),
    )
    assert fsynced == [native_path.parent.parent]

    second_path = _native_directory(store.board_grafx_path("board-2", "generation-2"))

    def refuse_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr(binding_module.os, "replace", refuse_replace)
    with pytest.raises(GraphUnavailable) as captured:
        store.initialize_board_binding(
            board_id="board-2",
            backend="grafx",
            generation="generation-2",
            physical_path=second_path,
        database=_FakeGrafxDatabase(second_path, page_size=8192),
        )
    assert captured.value.details["reason"] == "binding_publication_failed"
    assert not (second_path.parent.parent / "graph_backend_binding.json").exists()
    assert list(second_path.parent.parent.glob(".*.tmp")) == []

@pytest.mark.parametrize("scope", ["board", "global"])
def test_retired_backend_cannot_be_initialized_or_read(tmp_path: Path, scope: str) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    parent = tmp_path / "boards" / "old" if scope == "board" else tmp_path / "global"
    old_path = _legacy_database(parent / ("graph.lbug" if scope == "board" else "discovery.lbug"))
    before = {str(p.relative_to(tmp_path)): p.read_bytes() if p.is_file() else None
              for p in tmp_path.rglob("*")}
    with pytest.raises(GraphCapabilityUnavailable) as refused:
        if scope == "board":
            store.initialize_board_binding(board_id="old", backend="ladybug",
                generation="old", physical_path=old_path)
        else:
            store.initialize_global_binding(backend="ladybug", generation="old", physical_path=old_path)
    assert refused.value.details["reason"] == "binding_argument_invalid"
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() if p.is_file() else None
                      for p in tmp_path.rglob("*")}

    # An authenticated old document is still incompatible, not an import source.
    body = {"binding_format": binding_module.BINDING_FORMAT, "scope": scope,
            "scope_id": "old" if scope == "board" else "global", "backend": "ladybug",
            "generation": "old", "physical_path": old_path.relative_to(tmp_path).as_posix(),
            "page_size": None}
    document = dict(body, binding_sha256=binding_module._binding_sha256(body))
    binding_path = parent / "graph_backend_binding.json"
    binding_path.write_text(json.dumps(document), encoding="utf-8")
    old_bytes = binding_path.read_bytes()
    with pytest.raises(GraphCorruption) as refused_read:
        if scope == "board":
            store.inspect_board_binding("old")
        else:
            store.inspect_global_binding()
    assert refused_read.value.details["reason"] == "binding_document_invalid"
    assert binding_path.read_bytes() == old_bytes and old_path.read_bytes() == b"legacy"
