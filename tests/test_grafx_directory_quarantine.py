from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.quarantine_restore import (
    QuarantineRestoreError,
    QuarantineRestoreErrorCode,
)
from okto_pulse.core.kg.quarantine import QuarantineError

from okto_pulse.community.adapters import grafx_board_storage as storage_module
from okto_pulse.community.adapters import grafx_quarantine_restore as restore_module
from okto_pulse.community.adapters.grafx_board_storage import (
    GRAFX_DIRECTORY_PAYLOAD,
    GRAFX_DIRECTORY_QUARANTINE_FORMAT,
    GRAFX_DIRECTORY_QUARANTINE_KIND,
    erase_grafx_board_privacy_storage,
    grafx_board_privacy_scope,
    grafx_board_privacy_storage_present,
    quarantine_grafx_board_storage,
)
from okto_pulse.community.adapters.grafx_quarantine_restore import (
    CommunityGrafxQuarantineRestore,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)

BOARD_ID = "board-directory-quarantine"
PAGE_SIZE = 8192


def _create_database(path: Path, value: str):
    database = connect(path, page_size=PAGE_SIZE)
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE DirectoryProbe(id STRING, PRIMARY KEY(id))")
    transaction.execute("CREATE (:DirectoryProbe {id: $id})", {"id": value})
    transaction.commit()
    database.checkpoint()
    return database


def _bound_database(root: Path, value: str = "original") -> Path:
    path = root / "boards" / BOARD_ID / "grafx" / "generation-1"
    database = _create_database(path, value)
    CommunityGraphBackendBindingStore(root).initialize_board_binding(
        board_id=BOARD_ID,
        backend="grafx",
        generation="generation-1",
        physical_path=path,
        page_size=PAGE_SIZE,
        database=database,
    )
    database.close()
    assert database.close_complete is True
    return path


def _restore(
    root: Path,
    path: Path,
    *,
    open_database=None,
    fences: list[tuple[str, str]] | None = None,
) -> CommunityGrafxQuarantineRestore:
    observed = fences if fences is not None else []
    opener = open_database or (
        lambda candidate: connect(candidate, page_size=PAGE_SIZE)
    )
    return CommunityGrafxQuarantineRestore(
        quarantine_root=root / "quarantine",
        database_path_resolver=lambda board_id: (
            path if board_id == BOARD_ID else root / "missing"
        ),
        open_database=opener,
        close_board=lambda _board_id: None,
        board_is_locked=lambda _board_id: False,
        revalidate_fence=lambda board_id, phase: observed.append((board_id, phase)),
        mutation_guard=lambda _board_id: nullcontext(),
    )


def _query_ids(path: Path) -> tuple[str, ...]:
    database = connect(path, page_size=PAGE_SIZE)
    try:
        rows = database.execute(
            "MATCH (n:DirectoryProbe) RETURN n.id ORDER BY n.id"
        ).rows
        return tuple(str(row[0]) for row in rows)
    finally:
        database.close()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _manifest(root: Path, quarantine_id: str) -> dict[str, object]:
    return json.loads(
        (root / "quarantine" / quarantine_id / "manifest.json").read_text(
            encoding="utf-8"
        )
    )


def _restore_residues(path: Path) -> tuple[Path, ...]:
    return tuple(path.parent.glob(f".{path.name}.*.restore.*"))


def _swap_live_database(path: Path, value: str = "foreign") -> Path:
    saved = path.with_name(f".{path.name}.test.saved")
    os.replace(path, saved)
    foreign = _create_database(path, value)
    foreign.close()
    return saved


def _quarantine(root: Path, path: Path) -> tuple[int, str]:
    moved, quarantine_id = quarantine_grafx_board_storage(
        BOARD_ID,
        path,
        reason="directory-quarantine-test",
    )
    assert moved > 0
    assert quarantine_id is not None
    return moved, quarantine_id


def _windows_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction semantics are required")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr.strip()}")
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    assert int(getattr(link.lstat(), "st_file_attributes", 0)) & reparse


def test_directory_quarantine_uses_kg_root_and_restores_complete_generation(
    tmp_path: Path,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    quarantine_dir = tmp_path / "quarantine" / quarantine_id
    payload = quarantine_dir.joinpath(*Path(GRAFX_DIRECTORY_PAYLOAD).parts)
    manifest = _manifest(tmp_path, quarantine_id)

    assert not path.exists()
    assert quarantine_dir.is_dir()
    assert not (path.parents[1] / "quarantine").exists()
    assert manifest["format"] == GRAFX_DIRECTORY_QUARANTINE_FORMAT
    assert manifest["kind"] == GRAFX_DIRECTORY_QUARANTINE_KIND
    assert manifest["complete"] is True
    relative_files = [str(entry["relative_path"]) for entry in manifest["files"]]
    assert relative_files == sorted(relative_files)
    assert {"grafx.meta", "catalog.dat", "heap.dat"}.issubset(relative_files)
    assert any(name.startswith("wal/") for name in relative_files)
    source_before = _tree_hashes(payload)

    adapter = _restore(tmp_path, path)
    plan = adapter.plan(quarantine_id)
    first = adapter.apply(quarantine_id)
    quarantine_count = len(tuple((tmp_path / "quarantine").glob("grafx-board-*")))
    second = adapter.apply(quarantine_id)

    assert len(plan.files) == len(relative_files)
    assert first.applied is second.applied is True
    assert first.open_validated is second.open_validated is True
    assert first.backup_quarantine_id is second.backup_quarantine_id is None
    assert (
        len(tuple((tmp_path / "quarantine").glob("grafx-board-*"))) == quarantine_count
    )
    assert _query_ids(path) == ("original",)
    assert _tree_hashes(payload) == source_before


def test_directory_quarantine_tamper_fails_before_live_mutation(tmp_path: Path) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    manifest = _manifest(tmp_path, quarantine_id)
    relative = str(manifest["files"][0]["relative_path"])
    payload_file = (
        tmp_path
        / "quarantine"
        / quarantine_id
        / GRAFX_DIRECTORY_PAYLOAD
        / Path(relative)
    )
    payload_file.write_bytes(payload_file.read_bytes() + b"tamper")

    with pytest.raises(QuarantineRestoreError) as captured:
        _restore(tmp_path, path).plan(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND
    assert not path.exists()


@pytest.mark.parametrize(
    ("duplicate_case", "key"),
    (
        ("identity", "kind"),
        ("board", "board_id"),
        ("inventory", "relative_path"),
    ),
)
def test_directory_manifest_duplicate_key_fails_before_restore_or_privacy_mutation(
    tmp_path: Path,
    duplicate_case: str,
    key: str,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    manifest_path = tmp_path / "quarantine" / quarantine_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    value = (
        manifest["files"][0][key] if duplicate_case == "inventory" else manifest[key]
    )
    encoded_pair = f"{json.dumps(key)}:{json.dumps(value)}"
    serialized = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    ambiguous = serialized.replace(
        encoded_pair,
        f"{encoded_pair},{encoded_pair}",
        1,
    )
    assert ambiguous != serialized
    manifest_path.write_text(ambiguous, encoding="utf-8")
    candidate = _create_database(path, "candidate")
    candidate.close()

    with pytest.raises(QuarantineRestoreError) as captured:
        _restore(tmp_path, path).plan(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND
    assert _query_ids(path) == ("candidate",)
    scope = grafx_board_privacy_scope(BOARD_ID, path.parents[1])
    with pytest.raises(ValueError, match="duplicate Grafx manifest key"):
        erase_grafx_board_privacy_storage(scope, before_mutation=lambda: None)
    assert _query_ids(path) == ("candidate",)


def test_directory_quarantine_rejects_external_junction_without_reading_sentinel(
    tmp_path: Path,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    payload = tmp_path / "quarantine" / quarantine_id / GRAFX_DIRECTORY_PAYLOAD
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must-remain-external", encoding="utf-8")
    junction = payload / "escape"
    _windows_junction(junction, external)
    try:
        with pytest.raises(QuarantineRestoreError) as captured:
            _restore(tmp_path, path).plan(quarantine_id)
        assert captured.value.code is QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND
        assert sentinel.read_text(encoding="utf-8") == "must-remain-external"
        assert {item.name for item in external.iterdir()} == {"sentinel.txt"}
        assert not path.exists()
    finally:
        junction.rmdir()


def test_directory_restore_publication_failure_rolls_back_and_retries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    source_payload = tmp_path / "quarantine" / quarantine_id / GRAFX_DIRECTORY_PAYLOAD
    source_before = _tree_hashes(source_payload)
    candidate = _create_database(path, "candidate")
    candidate.close()
    original_replace = restore_module.os.replace
    failed = False

    def fail_publish_once(source, destination):
        nonlocal failed
        source_path = Path(source)
        if (
            not failed
            and source_path.name.endswith(".restore.pending")
            and Path(destination) == path
        ):
            failed = True
            raise PermissionError("injected directory publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr(restore_module.os, "replace", fail_publish_once)
    adapter = _restore(tmp_path, path)
    with pytest.raises(QuarantineRestoreError) as captured:
        adapter.apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert captured.value.details["source_preserved"] is True
    assert captured.value.details["rollback"] == "rolled_back"
    assert _query_ids(path) == ("candidate",)
    assert _tree_hashes(source_payload) == source_before

    report = adapter.apply(quarantine_id)
    assert report.open_validated is True
    assert _query_ids(path) == ("original",)
    assert _tree_hashes(source_payload) == source_before


def test_directory_restore_cold_open_failure_preserves_live_and_source_for_retry(
    tmp_path: Path,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    source_payload = tmp_path / "quarantine" / quarantine_id / GRAFX_DIRECTORY_PAYLOAD
    source_before = _tree_hashes(source_payload)
    candidate = _create_database(path, "candidate")
    candidate.close()

    def fail_candidate_open(candidate_path: Path):
        if candidate_path.name.endswith(".restore.pending"):
            raise RuntimeError("injected cold-open failure")
        return connect(candidate_path, page_size=PAGE_SIZE)

    with pytest.raises(QuarantineRestoreError) as captured:
        _restore(tmp_path, path, open_database=fail_candidate_open).apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert captured.value.details["rollback"] == "rolled_back"
    assert _query_ids(path) == ("candidate",)
    assert _tree_hashes(source_payload) == source_before

    assert _restore(tmp_path, path).apply(quarantine_id).open_validated is True
    assert _query_ids(path) == ("original",)


def test_directory_restore_published_cold_open_failure_rolls_back_and_retries(
    tmp_path: Path,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    source_payload = tmp_path / "quarantine" / quarantine_id / GRAFX_DIRECTORY_PAYLOAD
    source_before = _tree_hashes(source_payload)
    candidate = _create_database(path, "candidate")
    candidate.close()
    failed = False

    def fail_published_open(candidate_path: Path):
        nonlocal failed
        if not failed and candidate_path == path:
            failed = True
            raise RuntimeError("injected published cold-open failure")
        return connect(candidate_path, page_size=PAGE_SIZE)

    adapter = _restore(tmp_path, path, open_database=fail_published_open)
    with pytest.raises(QuarantineRestoreError) as captured:
        adapter.apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert captured.value.details["rollback"] == "rolled_back"
    assert _query_ids(path) == ("candidate",)
    assert _tree_hashes(source_payload) == source_before

    assert adapter.apply(quarantine_id).open_validated is True
    assert _query_ids(path) == ("original",)


@pytest.mark.parametrize("boundary", ("after_displace", "after_publish"))
def test_directory_restore_retry_handles_interrupted_rename_boundaries(
    tmp_path: Path,
    monkeypatch,
    boundary: str,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    source_payload = tmp_path / "quarantine" / quarantine_id / GRAFX_DIRECTORY_PAYLOAD
    source_before = _tree_hashes(source_payload)
    candidate = _create_database(path, "candidate")
    candidate.close()
    adapter = _restore(tmp_path, path)
    original_replace = restore_module.os.replace

    def interrupt_publication(source, destination):
        source_path = Path(source)
        if source_path.name.endswith(".restore.pending") and Path(destination) == path:
            if boundary == "after_publish":
                original_replace(source, destination)
            raise PermissionError(f"simulated crash {boundary}")
        return original_replace(source, destination)

    with monkeypatch.context() as crash:
        crash.setattr(restore_module.os, "replace", interrupt_publication)
        crash.setattr(
            adapter,
            "_rollback_directory_publication",
            lambda **_kwargs: "simulated_process_exit",
        )
        with pytest.raises(QuarantineRestoreError):
            adapter.apply(quarantine_id)

    assert _restore_residues(path)
    if boundary == "after_publish":
        with pytest.raises(QuarantineRestoreError) as captured:
            adapter.apply(quarantine_id)
        assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
        assert _query_ids(path) == ("original",)
        assert _restore_residues(path)
        assert _tree_hashes(source_payload) == source_before
        return
    report = adapter.apply(quarantine_id)

    assert report.open_validated is True
    assert _query_ids(path) == ("original",)
    assert not _restore_residues(path)
    assert _tree_hashes(source_payload) == source_before


def test_directory_restore_retry_completes_publish_after_cleanup_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    source_payload = tmp_path / "quarantine" / quarantine_id / GRAFX_DIRECTORY_PAYLOAD
    source_before = _tree_hashes(source_payload)
    candidate = _create_database(path, "candidate")
    candidate.close()
    adapter = _restore(tmp_path, path)
    original_write = restore_module._write_directory_json_atomic

    def interrupt_terminal_journal(target: Path, payload: dict[str, object]) -> None:
        if target.parent.name == ".grafx_directory_restore_operations" and payload.get(
            "phase"
        ) in {"done", "failed"}:
            raise PermissionError("simulated crash before done journal")
        original_write(target, payload)

    with monkeypatch.context() as crash:
        crash.setattr(
            restore_module,
            "_write_directory_json_atomic",
            interrupt_terminal_journal,
        )
        with pytest.raises(QuarantineRestoreError):
            adapter.apply(quarantine_id)

    assert path.is_dir()
    assert not _restore_residues(path)
    quarantine_count = len(tuple((tmp_path / "quarantine").glob("grafx-board-*")))

    report = adapter.apply(quarantine_id)

    assert report.open_validated is True
    assert _query_ids(path) == ("original",)
    assert not _restore_residues(path)
    assert (
        len(tuple((tmp_path / "quarantine").glob("grafx-board-*"))) == quarantine_count
    )
    assert _tree_hashes(source_payload) == source_before


def test_directory_restore_done_rejects_valid_foreign_database_swap(
    tmp_path: Path,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    adapter = _restore(tmp_path, path)
    assert adapter.apply(quarantine_id).open_validated is True
    saved = _swap_live_database(path)

    with pytest.raises(QuarantineRestoreError) as captured:
        adapter.apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert _query_ids(path) == ("foreign",)
    assert saved.is_dir()


def test_directory_restore_done_rejects_authenticated_terminal_type_confusion(
    tmp_path: Path,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    adapter = _restore(tmp_path, path)
    assert adapter.apply(quarantine_id).open_validated is True
    operation = next(
        (path.parent / ".grafx_directory_restore_operations").glob("*.json")
    )
    state = json.loads(operation.read_text(encoding="utf-8"))
    terminal_files = state["terminal_files"]
    assert type(terminal_files) is list and terminal_files
    assert type(terminal_files[0]) is dict
    terminal_files[0]["size_bytes"] = str(terminal_files[0]["size_bytes"])
    authenticated = storage_module._authenticated_manifest(state)
    operation.write_text(
        json.dumps(authenticated, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(QuarantineRestoreError) as captured:
        adapter.apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert _query_ids(path) == ("original",)


@pytest.mark.parametrize("existing_live", (False, True))
def test_directory_crash_reconcile_rejects_valid_foreign_live_database(
    tmp_path: Path,
    monkeypatch,
    existing_live: bool,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    if existing_live:
        candidate = _create_database(path, "candidate")
        candidate.close()
    adapter = _restore(tmp_path, path)
    original_write = restore_module._write_directory_json_atomic

    def interrupt_terminal_journal(target: Path, payload: dict[str, object]) -> None:
        if target.parent.name == ".grafx_directory_restore_operations" and payload.get(
            "phase"
        ) in {"done", "failed"}:
            raise PermissionError("simulated process exit after terminal inventory")
        original_write(target, payload)

    with monkeypatch.context() as crash:
        crash.setattr(
            restore_module,
            "_write_directory_json_atomic",
            interrupt_terminal_journal,
        )
        crash.setattr(
            adapter,
            "_rollback_directory_publication",
            lambda **_kwargs: "simulated_process_exit",
        )
        with pytest.raises(QuarantineRestoreError):
            adapter.apply(quarantine_id)

    saved = _swap_live_database(path)
    with pytest.raises(QuarantineRestoreError) as captured:
        adapter.apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert _query_ids(path) == ("foreign",)
    assert saved.is_dir()


def test_fresh_restore_crash_before_terminal_inventory_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)

    def fail_published_open(candidate_path: Path):
        if candidate_path == path:
            raise RuntimeError("simulated exit before terminal inventory")
        return connect(candidate_path, page_size=PAGE_SIZE)

    interrupted = _restore(tmp_path, path, open_database=fail_published_open)
    with monkeypatch.context() as crash:
        crash.setattr(
            interrupted,
            "_rollback_directory_publication",
            lambda **_kwargs: "simulated_process_exit",
        )
        with pytest.raises(QuarantineRestoreError):
            interrupted.apply(quarantine_id)

    operation = next(
        (path.parent / ".grafx_directory_restore_operations").glob("*.json")
    )
    state = json.loads(operation.read_text(encoding="utf-8"))
    assert "terminal_inventory_sha256" not in state
    with pytest.raises(QuarantineRestoreError) as captured:
        _restore(tmp_path, path).apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert _query_ids(path) == ("original",)


def test_privacy_erase_reaches_interrupted_restore_and_final_snapshots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    candidate = _create_database(path, "candidate")
    candidate.close()
    adapter = _restore(tmp_path, path)
    original_replace = restore_module.os.replace

    def interrupt_after_displace(source, destination):
        source_path = Path(source)
        if source_path.name.endswith(".restore.pending") and Path(destination) == path:
            raise PermissionError("simulated crash after displace")
        return original_replace(source, destination)

    with monkeypatch.context() as crash:
        crash.setattr(restore_module.os, "replace", interrupt_after_displace)
        crash.setattr(
            adapter,
            "_rollback_directory_publication",
            lambda **_kwargs: "simulated_process_exit",
        )
        with pytest.raises(QuarantineRestoreError):
            adapter.apply(quarantine_id)

    scope = grafx_board_privacy_scope(BOARD_ID, path.parents[1])
    assert _restore_residues(path)
    assert (path.parent / ".grafx_directory_restore_operations").is_dir()
    assert (tmp_path / "quarantine" / quarantine_id).is_dir()

    removed = erase_grafx_board_privacy_storage(
        scope,
        before_mutation=lambda: None,
    )

    assert removed > 0
    assert not path.parent.exists()
    assert not scope.binding_path.exists()
    assert not tuple((tmp_path / "quarantine").glob("grafx-board-*"))
    assert not tuple((tmp_path / "quarantine").glob(".grafx-board-*.pending"))
    assert grafx_board_privacy_storage_present(scope) is False


def test_privacy_erase_reaches_pending_directory_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path)
    original_replace = storage_module.os.replace

    def interrupt_final_publish(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.name.startswith(".grafx-board-")
            and source_path.name.endswith(".pending")
            and destination_path.name.startswith("grafx-board-")
        ):
            raise PermissionError("simulated pending directory snapshot")
        return original_replace(source, destination)

    with monkeypatch.context() as crash:
        crash.setattr(storage_module.os, "replace", interrupt_final_publish)
        with pytest.raises(QuarantineError):
            quarantine_grafx_board_storage(BOARD_ID, path, reason="privacy-test")

    scope = grafx_board_privacy_scope(BOARD_ID, path.parents[1])
    assert tuple((tmp_path / "quarantine").glob(".grafx-board-*.pending"))

    erase_grafx_board_privacy_storage(scope, before_mutation=lambda: None)

    assert not tuple((tmp_path / "quarantine").glob(".grafx-board-*.pending"))
    assert not scope.grafx_root.exists()
    assert not scope.binding_path.exists()
    assert grafx_board_privacy_storage_present(scope) is False


def test_directory_quarantine_publication_retry_recovers_pending_capture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path)
    original_replace = storage_module.os.replace
    failed = False

    def fail_final_publish_once(source, destination):
        nonlocal failed
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not failed
            and source_path.name.startswith(".grafx-board-")
            and source_path.name.endswith(".pending")
            and destination_path.name.startswith("grafx-board-")
        ):
            failed = True
            raise PermissionError("injected quarantine publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr(storage_module.os, "replace", fail_final_publish_once)
    with pytest.raises(QuarantineError):
        quarantine_grafx_board_storage(BOARD_ID, path, reason="retry-test")
    assert not path.exists()
    assert len(tuple((tmp_path / "quarantine").glob(".grafx-board-*.pending"))) == 1

    moved, quarantine_id = quarantine_grafx_board_storage(
        BOARD_ID,
        path,
        reason="retry-test",
    )
    assert moved > 0
    assert quarantine_id is not None
    assert not tuple((tmp_path / "quarantine").glob(".grafx-board-*.pending"))
    assert _manifest(tmp_path, quarantine_id)["complete"] is True


def test_rebuild_compensation_is_fenced_concrete_and_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path)
    _, quarantine_id = _quarantine(tmp_path, path)
    candidate = _create_database(path, "candidate")
    candidate.close()
    fences: list[tuple[str, str]] = []
    adapter = _restore(tmp_path, path, fences=fences)
    monkeypatch.setattr(
        CommunityGrafxQuarantineRestore,
        "_rebuild_owner",
        staticmethod(lambda _board_id, _owner_token: True),
    )

    first = adapter.apply_rebuild_compensation(
        quarantine_id,
        expected_board_id=BOARD_ID,
        run_id="run-directory-compensation",
        owner_token="owner-token",
    )
    count_after_first = len(tuple((tmp_path / "quarantine").glob("grafx-board-*")))
    second = adapter.apply_rebuild_compensation(
        quarantine_id,
        expected_board_id=BOARD_ID,
        run_id="run-directory-compensation",
        owner_token="owner-token",
    )

    assert first.backup_quarantine_id == second.backup_quarantine_id
    assert first.backup_quarantine_id is not None
    assert (
        len(tuple((tmp_path / "quarantine").glob("grafx-board-*"))) == count_after_first
    )
    assert _query_ids(path) == ("original",)
    assert any(phase == "quarantine_restore_directory_publish" for _, phase in fences)


def test_rebuild_candidate_discard_is_fenced_and_retry_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path, value="candidate")
    adapter = _restore(tmp_path, path)
    monkeypatch.setattr(
        CommunityGrafxQuarantineRestore,
        "_rebuild_owner",
        staticmethod(lambda _board_id, _owner_token: True),
    )

    first = adapter.discard_rebuild_candidate(
        expected_board_id=BOARD_ID,
        run_id="run-directory-discard",
        owner_token="owner-token",
    )
    second = adapter.discard_rebuild_candidate(
        expected_board_id=BOARD_ID,
        run_id="run-directory-discard",
        owner_token="owner-token",
    )

    assert first["status"] == "discarded"
    assert first["discarded_files"] > 0
    assert first["quarantine_id"] is not None
    assert first["live_absent"] is True
    assert second == first
    assert not path.exists()


def test_rebuild_candidate_discard_rechecks_owner_at_generation_move(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _bound_database(tmp_path, value="candidate")
    adapter = _restore(tmp_path, path)
    ownership = iter((True, True, False))
    monkeypatch.setattr(
        CommunityGrafxQuarantineRestore,
        "_rebuild_owner",
        staticmethod(lambda _board_id, _owner_token: next(ownership)),
    )

    with pytest.raises(QuarantineError):
        adapter.discard_rebuild_candidate(
            expected_board_id=BOARD_ID,
            run_id="run-directory-discard-fence-loss",
            owner_token="owner-token",
        )

    assert _query_ids(path) == ("candidate",)
    assert not tuple((tmp_path / "quarantine").glob(".grafx-board-*.pending"))
    assert not tuple((tmp_path / "quarantine").glob("grafx-board-*"))
