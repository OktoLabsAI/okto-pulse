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
from okto_pulse.community.config import (
    CommunitySettings,
    CommunitySettingsAliasConflict,
)


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


def _ladybug_database(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ladybug")
    return path


def _grafx_database(path: Path, *, page_size: int = 8192) -> _FakeGrafxDatabase:
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.grafx").write_bytes(b"grafx")
    return _FakeGrafxDatabase(path, page_size=page_size)


def test_settings_default_to_ladybug_and_safe_grafx_geometry(tmp_path: Path) -> None:
    settings = CommunitySettings(data_dir=str(tmp_path), _env_file=None)

    assert settings.kg_graph_backend == "ladybug"
    assert settings.kg_global_graph_backend == "ladybug"
    assert settings.kg_grafx_page_size == 8192
    assert settings.kg_grafx_descriptor_revalidation == "generation"
    assert settings.kg_ladybug_buffer_pool_mb == settings.kg_kuzu_buffer_pool_mb
    assert (
        settings.kg_global_ladybug_buffer_pool_mb
        == settings.kg_global_kuzu_buffer_pool_mb
    )
    assert settings.kg_ladybug_max_db_size_gb == settings.kg_kuzu_max_db_size_gb


def test_settings_accept_only_semantically_identical_ladybug_aliases(
    tmp_path: Path,
) -> None:
    canonical = CommunitySettings(
        data_dir=str(tmp_path),
        kg_ladybug_buffer_pool_mb=384,
        kg_global_ladybug_buffer_pool_mb=256,
        kg_ladybug_max_db_size_gb=8,
        _env_file=None,
    )
    identical_pair = CommunitySettings(
        data_dir=str(tmp_path),
        kg_ladybug_buffer_pool_mb=384,
        kg_kuzu_buffer_pool_mb=384,
        _env_file=None,
    )

    assert canonical.kg_kuzu_buffer_pool_mb == 384
    assert canonical.kg_global_kuzu_buffer_pool_mb == 256
    assert canonical.kg_kuzu_max_db_size_gb == 8
    assert identical_pair.kg_kuzu_buffer_pool_mb == 384

    with pytest.raises(CommunitySettingsAliasConflict) as captured:
        CommunitySettings(
            data_dir=str(tmp_path),
            kg_ladybug_buffer_pool_mb=384,
            kg_kuzu_buffer_pool_mb=256,
            _env_file=None,
        )
    assert captured.value.code == "community_settings_alias_conflict"
    assert captured.value.source == "init"


def test_settings_reject_conflicting_aliases_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KG_LADYBUG_MAX_DB_SIZE_GB", "8")
    monkeypatch.setenv("KG_KUZU_MAX_DB_SIZE_GB", "4")

    with pytest.raises(CommunitySettingsAliasConflict) as captured:
        CommunitySettings(data_dir=str(tmp_path), _env_file=None)

    assert captured.value.source == "environment"


def test_settings_reject_conflicting_aliases_from_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "KG_LADYBUG_BUFFER_POOL_MB=384\nKG_KUZU_BUFFER_POOL_MB=256\n",
        encoding="utf-8",
    )

    with pytest.raises(CommunitySettingsAliasConflict) as captured:
        CommunitySettings(data_dir=str(tmp_path), _env_file=dotenv)

    assert captured.value.source == "dotenv"


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

    assert "okto-grafx[accel]==0.0.1" in project["project"]["dependencies"]


def test_missing_binding_fails_closed_without_creating_state(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        store.acquire_board_binding("board-1")

    assert captured.value.details["reason"] == "binding_missing"
    assert list(tmp_path.iterdir()) == []


def test_board_ladybug_binding_is_durable_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    ladybug_path = _ladybug_database(store.board_ladybug_path("board-1"))

    first = store.initialize_board_binding(
        board_id="board-1",
        backend="ladybug",
        generation="generation-1",
        physical_path=ladybug_path,
    )
    second = store.initialize_board_binding(
        board_id="board-1",
        backend="ladybug",
        generation="generation-1",
        physical_path=ladybug_path,
    )

    assert first == second == store.acquire_board_binding("board-1")
    assert first.backend == "ladybug"
    assert first.page_size is None
    assert first.physical_path == ladybug_path
    with pytest.raises(FrozenInstanceError):
        first.backend = "grafx"  # type: ignore[misc]

    document = json.loads(
        (ladybug_path.parent / "graph_backend_binding.json").read_text(encoding="utf-8")
    )
    assert document["physical_path"] == "boards/board-1/graph.lbug"
    assert len(document["binding_sha256"]) == 64


def test_binding_refuses_rebind_without_m7_cas(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    ladybug_path = _ladybug_database(store.board_ladybug_path("board-1"))
    store.initialize_board_binding(
        board_id="board-1",
        backend="ladybug",
        generation="generation-1",
        physical_path=ladybug_path,
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
    assert store.acquire_board_binding("board-1").backend == "ladybug"


def test_global_binding_is_separate_from_each_board(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    board_path = _ladybug_database(store.board_ladybug_path("board-1"))
    global_path = _ladybug_database(store.global_ladybug_path())

    board = store.initialize_board_binding(
        board_id="board-1",
        backend="ladybug",
        generation="board-generation",
        physical_path=board_path,
    )
    global_binding = store.initialize_global_binding(
        backend="ladybug",
        generation="global-generation",
        physical_path=global_path,
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
    ladybug_path = _ladybug_database(store.board_ladybug_path("board-1"))
    store.initialize_board_binding(
        board_id="board-1",
        backend="ladybug",
        generation="generation-1",
        physical_path=ladybug_path,
    )
    binding_path = ladybug_path.parent / "graph_backend_binding.json"
    document = json.loads(binding_path.read_text(encoding="utf-8"))
    document["generation"] = "generation-tampered"
    binding_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(GraphCorruption) as tampered:
        store.acquire_board_binding("board-1")
    assert tampered.value.details["reason"] == "binding_document_invalid"

    binding_path.unlink()
    store.initialize_board_binding(
        board_id="board-1",
        backend="ladybug",
        generation="generation-1",
        physical_path=ladybug_path,
    )
    ladybug_path.unlink()
    inspected = store.inspect_board_binding("board-1")
    assert inspected.backend == "ladybug"
    assert inspected.physical_path == ladybug_path
    with pytest.raises(GraphUnavailable) as missing:
        store.acquire_board_binding("board-1")
    assert missing.value.details["reason"] == "physical_database_missing"


def test_global_binding_inspection_survives_missing_database_but_not_tampering(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    global_path = _ladybug_database(store.global_ladybug_path())
    expected = store.initialize_global_binding(
        backend="ladybug",
        generation="generation-1",
        physical_path=global_path,
    )
    global_path.unlink()

    assert store.inspect_global_binding() == expected
    with pytest.raises(GraphUnavailable) as missing:
        store.acquire_global_binding()
    assert missing.value.details["reason"] == "physical_database_missing"

    binding_path = global_path.parent / "graph_backend_binding.json"
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

    wrong_path = _ladybug_database(
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
    ladybug_path = _ladybug_database(store.board_ladybug_path("board-1"))
    fsynced: list[Path] = []
    monkeypatch.setattr(binding_module, "fsync_directory", fsynced.append)

    store.initialize_board_binding(
        board_id="board-1",
        backend="ladybug",
        generation="generation-1",
        physical_path=ladybug_path,
    )
    assert fsynced == [ladybug_path.parent]

    second_path = _ladybug_database(store.board_ladybug_path("board-2"))

    def refuse_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr(binding_module.os, "replace", refuse_replace)
    with pytest.raises(GraphUnavailable) as captured:
        store.initialize_board_binding(
            board_id="board-2",
            backend="ladybug",
            generation="generation-2",
            physical_path=second_path,
        )
    assert captured.value.details["reason"] == "binding_publication_failed"
    assert not (second_path.parent / "graph_backend_binding.json").exists()
    assert list(second_path.parent.glob(".*.tmp")) == []
