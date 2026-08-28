from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.quarantine_restore import (
    QuarantineRestoreError,
    QuarantineRestoreErrorCode,
)

from okto_pulse.community.adapters import grafx_quarantine_restore as restore_module
from okto_pulse.community.adapters.grafx_graph_recovery import (
    CommunityGrafxGraphRecovery,
)
from okto_pulse.community.adapters.grafx_quarantine_restore import (
    CommunityGrafxQuarantineRestore,
)

BOARD_ID = "board-m6-recovery"


def _seed_checkpointed_database(path: Path) -> None:
    database = connect(path)
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE RecoveryProbe(id STRING, PRIMARY KEY(id))")
    transaction.execute("CREATE (n:RecoveryProbe {id: 'kept'})")
    transaction.commit()
    database.checkpoint()
    database.close()
    assert database.close_complete is True


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _protected_digests(path: Path) -> dict[str, str]:
    names = [
        "grafx.meta",
        "catalog.dat",
        "heap.dat",
        "control/commit.state",
    ]
    names.extend(
        item.relative_to(path).as_posix()
        for item in sorted((path / "index").glob("*.idx"))
    )
    return {name: _digest(path / name) for name in names}


def _tree_digests(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {
        item.relative_to(path).as_posix(): _digest(item)
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _wal_digests(database_path: Path) -> dict[str, str]:
    wal_root = database_path / "wal"
    return {
        item.relative_to(database_path).as_posix(): _digest(item)
        for item in sorted(wal_root.rglob("*"))
        if item.is_file()
    }


def _damage_wal_tail(database_path: Path, *, length: int = 4096) -> None:
    segment = next(iter(sorted((database_path / "wal").glob("*.wal"))))
    with segment.open("ab") as stream:
        stream.write(bytes(length))
        stream.flush()


def _recovery(
    database_path: Path,
    *,
    fences: list[tuple[str, str]] | None = None,
    open_database=connect,
) -> CommunityGrafxGraphRecovery:
    observed = fences if fences is not None else []
    quarantine_root = database_path.parents[3] / "quarantine"
    return CommunityGrafxGraphRecovery(
        quarantine_root=quarantine_root,
        database_path_resolver=lambda board_id: (
            database_path if board_id == BOARD_ID else Path("missing")
        ),
        open_database=open_database,
        close_board=lambda _board_id: None,
        revalidate_fence=lambda board_id, phase: observed.append((board_id, phase)),
        mutation_guard=lambda _board_id: nullcontext(),
    )


def _external_wal_quarantine(
    database_path: Path, quarantine_root: Path, *, quarantine_id: str
) -> Path:
    quarantine_dir = quarantine_root / quarantine_id
    files: list[dict[str, object]] = []
    for source in sorted((database_path / "wal").glob("*.wal")):
        relative = source.relative_to(database_path).as_posix()
        destination = quarantine_dir / "payload" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files.append(
            {
                "name": source.name,
                "relative_path": relative,
                "size_bytes": source.stat().st_size,
                "sha256": _digest(source),
            }
        )
    assert files
    manifest = {
        "format": "pulse_grafx_quarantine/1",
        "kind": "grafx_wal_only",
        "quarantine_id": quarantine_id,
        "board_id": BOARD_ID,
        "database_path": str(database_path.resolve()),
        "created_at": "2026-08-28T00:00:00+00:00",
        "main_untouched": True,
        "complete": True,
        "phase": "recovered",
        "files": files,
        "files_moved": [entry["relative_path"] for entry in files],
        "error": None,
    }
    (quarantine_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return quarantine_dir


def _restore(
    database_path: Path,
    quarantine_root: Path,
    *,
    board_is_locked=lambda _board_id: False,
    open_database=connect,
    fences: list[tuple[str, str]] | None = None,
    mutation_guard=None,
    revalidate_fence=None,
) -> CommunityGrafxQuarantineRestore:
    observed = fences if fences is not None else []
    fence_callback = revalidate_fence or (
        lambda board_id, phase: observed.append((board_id, phase))
    )
    guard_callback = mutation_guard or (lambda _board_id: nullcontext())
    return CommunityGrafxQuarantineRestore(
        quarantine_root=quarantine_root,
        database_path_resolver=lambda board_id: (
            database_path if board_id == BOARD_ID else Path("missing")
        ),
        open_database=open_database,
        close_board=lambda _board_id: None,
        board_is_locked=board_is_locked,
        revalidate_fence=fence_callback,
        mutation_guard=guard_callback,
    )


def test_recover_wal_only_skips_a_healthy_database_without_main_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _seed_checkpointed_database(database_path)
    protected_before = _protected_digests(database_path)
    wal_before = _wal_digests(database_path)
    fences: list[tuple[str, str]] = []

    report = asyncio.run(
        _recovery(database_path, fences=fences).recover_wal_only(BOARD_ID)
    )

    assert report.status == "skipped"
    assert report.main_untouched is True
    assert report.files_moved == ()
    assert report.reason == "Grafx recovery found no WAL work"
    assert _protected_digests(database_path) == protected_before
    assert _wal_digests(database_path) == wal_before
    assert fences == [
        (BOARD_ID, "wal_recovery_snapshot"),
        (BOARD_ID, "wal_recovery_open"),
        (BOARD_ID, "wal_recovery_reopen"),
    ]
    quarantine_root = database_path.parents[3] / "quarantine"
    assert not quarantine_root.exists() or not tuple(quarantine_root.iterdir())


def test_replay_without_a_published_quarantine_is_reported_as_skipped(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _seed_checkpointed_database(database_path)
    opens = 0

    class FirstOpen:
        def __init__(self, delegate) -> None:
            self._delegate = delegate
            self.recovery_report = SimpleNamespace(
                outcome="clean",
                records_replayed=1,
                records_discarded=0,
                findings=(),
            )

        @property
        def close_complete(self):
            return self._delegate.close_complete

        def verify(self, scope: str):
            return self._delegate.verify(scope)

        def close(self) -> None:
            self._delegate.close()

    def open_database(path: Path):
        nonlocal opens
        opens += 1
        database = connect(path)
        return FirstOpen(database) if opens == 1 else database

    report = asyncio.run(
        _recovery(database_path, open_database=open_database).recover_wal_only(BOARD_ID)
    )

    assert opens == 2
    assert report.status == "skipped"
    assert report.quarantine_id is None
    assert report.files_moved == ()


def test_recovery_refuses_a_linked_wal_root_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _seed_checkpointed_database(database_path)
    protected_before = _protected_digests(database_path)
    path_type = type(database_path)
    original = getattr(path_type, "is_junction", lambda _path: False)

    def report_wal_as_junction(path: Path) -> bool:
        return path.name == "wal" or bool(original(path))

    opened = False

    def open_database(_path: Path):
        nonlocal opened
        opened = True
        raise AssertionError("linked WAL root reached Grafx open")

    monkeypatch.setattr(
        path_type,
        "is_junction",
        report_wal_as_junction,
        raising=False,
    )
    report = asyncio.run(
        _recovery(database_path, open_database=open_database).recover_wal_only(BOARD_ID)
    )

    assert report.status == "failed"
    assert report.quarantine_id is None
    assert opened is False
    assert "OSError" in (report.reason or "")
    assert _protected_digests(database_path) == protected_before


def test_recover_wal_only_delegates_damage_to_grafx_and_keeps_main_untouched(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _seed_checkpointed_database(database_path)
    protected_before = _protected_digests(database_path)
    _damage_wal_tail(database_path)
    damaged_wal = _wal_digests(database_path)
    damaged_size = next((database_path / "wal").glob("*.wal")).stat().st_size

    report = asyncio.run(_recovery(database_path).recover_wal_only(BOARD_ID))

    assert report.status == "recovered", report
    assert report.main_untouched is True
    assert report.quarantine_id is not None
    assert any(name.startswith("wal/") for name in report.files_moved)
    quarantine_root = database_path.parents[3] / "quarantine"
    plan = _restore(database_path, quarantine_root).plan(report.quarantine_id)
    assert {entry.name for entry in plan.files} == set(damaged_wal)
    for entry in plan.files:
        assert _digest(Path(entry.source_path)) == damaged_wal[entry.name]
    manifest = json.loads(
        (quarantine_root / report.quarantine_id / "manifest.json").read_text("utf-8")
    )
    assert manifest["complete"] is True
    assert manifest["phase"] == "recovered"
    assert manifest["native_quarantine_ids"]
    assert _protected_digests(database_path) == protected_before
    assert next((database_path / "wal").glob("*.wal")).stat().st_size < damaged_size
    with connect(database_path) as reopened:
        assert reopened.verify("all").clean is True
        assert reopened.execute("MATCH (n:RecoveryProbe) RETURN n.id").rows == (
            ("kept",),
        )


def test_recovery_snapshot_is_directly_restorable_end_to_end(tmp_path: Path) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _seed_checkpointed_database(database_path)
    protected_before = _protected_digests(database_path)
    _damage_wal_tail(database_path)

    recovered = asyncio.run(_recovery(database_path).recover_wal_only(BOARD_ID))
    assert recovered.status == "recovered"
    assert recovered.quarantine_id is not None

    quarantine_root = database_path.parents[3] / "quarantine"
    restored = _restore(database_path, quarantine_root).apply(recovered.quarantine_id)

    assert restored.applied is True
    assert restored.open_validated is True
    assert set(restored.restored_files) == set(recovered.files_moved)
    assert restored.backup_quarantine_id is not None
    assert (quarantine_root / recovered.quarantine_id).is_dir()
    assert _protected_digests(database_path) == protected_before
    with connect(database_path) as reopened:
        assert reopened.verify("all").clean is True
        assert reopened.execute("MATCH (n:RecoveryProbe) RETURN n.id").rows == (
            ("kept",),
        )


def test_external_wal_restore_is_fenced_auditable_and_main_untouched(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    quarantine_root = tmp_path / "quarantine"
    _seed_checkpointed_database(database_path)
    protected_before = _protected_digests(database_path)
    original_wal = _wal_digests(database_path)
    quarantine_id = "grafx-wal-fixture"
    quarantine_dir = _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id=quarantine_id
    )
    source_before_plan = _tree_digests(quarantine_dir)
    restore_fences: list[tuple[str, str]] = []
    restore = _restore(
        database_path,
        quarantine_root,
        fences=restore_fences,
    )
    caplog.set_level("INFO", logger="okto_pulse.kg.quarantine.restore")

    plan = restore.plan(quarantine_id)
    assert plan.board_id == BOARD_ID
    assert {entry.name for entry in plan.files} == set(original_wal)
    assert _tree_digests(quarantine_dir) == source_before_plan

    restored = restore.apply(quarantine_id)

    assert restored.applied is True
    assert restored.open_validated is True
    assert set(restored.restored_files) == set(original_wal)
    assert restored.backup_quarantine_id is not None
    assert _tree_digests(quarantine_dir) == source_before_plan
    assert _protected_digests(database_path) == protected_before
    for relative, digest in original_wal.items():
        assert _digest(database_path / relative) == digest
    assert restore_fences[0] == (BOARD_ID, "quarantine_restore_begin")
    assert restore_fences[-1] == (BOARD_ID, "quarantine_restore_complete")
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "kg.quarantine.restore_dry_run" in events
    assert "kg.quarantine.restored" in events


def test_restore_refuses_a_live_board_without_any_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    quarantine_root = tmp_path / "quarantine"
    _seed_checkpointed_database(database_path)
    quarantine_id = "grafx-wal-locked"
    _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id=quarantine_id
    )
    live_before = _tree_digests(database_path)
    quarantine_before = _tree_digests(quarantine_root)

    restore = _restore(
        database_path,
        quarantine_root,
        board_is_locked=lambda _board_id: True,
    )
    with pytest.raises(QuarantineRestoreError) as captured:
        restore.apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.BOARD_LOCKED
    assert _tree_digests(database_path) == live_before
    assert _tree_digests(quarantine_root) == quarantine_before


def test_restore_records_partial_state_when_the_final_probe_fails(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    quarantine_root = tmp_path / "quarantine"
    _seed_checkpointed_database(database_path)
    quarantine_id = "grafx-wal-partial"
    _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id=quarantine_id
    )

    def fail_open(_path: Path):
        raise RuntimeError("injected restored open failure")

    restore = _restore(
        database_path,
        quarantine_root,
        open_database=fail_open,
    )
    with pytest.raises(QuarantineRestoreError) as captured:
        restore.apply(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert captured.value.details["step"] == "validate_open"
    operation = Path(str(captured.value.details["operation_manifest"]))
    state = json.loads(operation.read_text("utf-8"))
    assert state["phase"] == "failed"
    assert state["copied_from_snapshot"]
    assert state["rollback_instruction"]


def test_restore_holds_the_mutation_guard_through_done(tmp_path: Path) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    quarantine_root = tmp_path / "quarantine"
    _seed_checkpointed_database(database_path)
    quarantine_id = "grafx-wal-guarded"
    _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id=quarantine_id
    )
    active = False
    events: list[str] = []

    def guard(_board_id: str) -> AbstractContextManager[None]:
        @contextmanager
        def held():
            nonlocal active
            active = True
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")
                active = False

        return held()

    def fence(_board_id: str, phase: str) -> None:
        assert active is True
        events.append(phase)

    restored = _restore(
        database_path,
        quarantine_root,
        mutation_guard=guard,
        revalidate_fence=fence,
    ).apply(quarantine_id)

    assert restored.applied is True
    assert events[0] == "enter"
    assert events[-2:] == ["quarantine_restore_complete", "exit"]


def test_plan_rejects_a_canonical_wal_symlink_to_main_data(tmp_path: Path) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    quarantine_root = tmp_path / "quarantine"
    _seed_checkpointed_database(database_path)
    quarantine_id = "grafx-wal-symlink"
    _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id=quarantine_id
    )
    live_segment = next((database_path / "wal").glob("*.wal"))
    saved = tmp_path / "saved-live.wal"
    shutil.move(live_segment, saved)
    try:
        live_segment.symlink_to(database_path / "heap.dat")
    except OSError as failure:
        pytest.skip(f"host cannot create a symlink for this safety probe: {failure}")
    protected_before = _protected_digests(database_path)

    with pytest.raises(QuarantineRestoreError) as captured:
        _restore(database_path, quarantine_root).plan(quarantine_id)

    assert captured.value.code is QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND
    assert _protected_digests(database_path) == protected_before


def test_payload_change_after_plan_is_partial_and_never_published(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    quarantine_root = tmp_path / "quarantine"
    _seed_checkpointed_database(database_path)
    quarantine_id = "grafx-wal-toctou"
    quarantine_dir = _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id=quarantine_id
    )
    source = next((quarantine_dir / "payload" / "wal").glob("*.wal"))
    expected = _digest(source)
    changed = False

    def mutate_source(_board_id: str, phase: str) -> None:
        nonlocal changed
        if phase != "quarantine_restore_copy" or changed:
            return
        body = bytearray(source.read_bytes())
        body[-1] ^= 0xFF
        source.write_bytes(body)
        changed = True

    with pytest.raises(QuarantineRestoreError) as captured:
        _restore(
            database_path,
            quarantine_root,
            revalidate_fence=mutate_source,
        ).apply(quarantine_id)

    assert changed is True
    assert _digest(source) != expected
    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert captured.value.details["moved_to_backup"]
    assert captured.value.details["step"].startswith("copy_snapshot:")


def test_failure_after_backup_rename_reconciles_the_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    quarantine_root = tmp_path / "quarantine"
    _seed_checkpointed_database(database_path)
    quarantine_id = "grafx-wal-journal"
    _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id=quarantine_id
    )
    real_move = restore_module._atomic_move
    injected = False

    def move_then_fail(source: Path, destination: Path) -> None:
        nonlocal injected
        real_move(source, destination)
        injected = True
        raise OSError("injected after atomic backup rename")

    monkeypatch.setattr(restore_module, "_atomic_move", move_then_fail)
    with pytest.raises(QuarantineRestoreError) as captured:
        _restore(database_path, quarantine_root).apply(quarantine_id)

    assert injected is True
    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert captured.value.details["moved_to_backup"]
    assert (
        captured.value.details["reconciliation"]["backup_pending"] == "rename_completed"
    )
    operation = Path(str(captured.value.details["operation_manifest"]))
    persisted = json.loads(operation.read_text("utf-8"))
    assert persisted["moved_to_backup"]
    assert persisted["reconciliation"]["backup_pending"] == "rename_completed"


def test_done_journal_failure_is_a_typed_partial_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    quarantine_root = tmp_path / "quarantine"
    _seed_checkpointed_database(database_path)
    quarantine_id = "grafx-wal-done-journal"
    _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id=quarantine_id
    )
    real_write = restore_module._write_json_atomic
    injected = False

    def fail_done_once(path: Path, payload: dict[str, object]) -> None:
        nonlocal injected
        if payload.get("phase") == "done" and not injected:
            injected = True
            raise OSError("injected durable done failure")
        real_write(path, payload)

    monkeypatch.setattr(restore_module, "_write_json_atomic", fail_done_once)
    with pytest.raises(QuarantineRestoreError) as captured:
        _restore(database_path, quarantine_root).apply(quarantine_id)

    assert injected is True
    assert captured.value.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    assert captured.value.details["step"] == "validate_open"
    operation = Path(str(captured.value.details["operation_manifest"]))
    persisted = json.loads(operation.read_text("utf-8"))
    assert persisted["phase"] == "failed"
    assert persisted["open_validated"] is True


# --- Windows junctions, proved on the floor of the supported range ------------------------------
#
# ``Path.is_junction`` arrived in CPython 3.12 and Pulse supports 3.11, so these
# tests DELETE it before exercising the boundary. Without that, the suite proves
# the guard only on the interpreter that happens to be running and stays green on
# 3.11 while the protection is off -- which is exactly how this defect survived a
# clean audit. A real junction is created here; nothing is monkeypatched into
# existence.


def _make_junction(link: Path, target: Path) -> bool:
    """Create a real Windows directory junction, or report that we cannot."""

    import subprocess

    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        # A host that cannot make a junction is a skip, not an error, so the
        # return code is read rather than raised.
        check=False,
    )
    return completed.returncode == 0 and link.exists()


@pytest.fixture
def without_is_junction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present the pathlib surface of Python 3.11: no ``Path.is_junction``.

    The method is inherited, so deleting it from ``Path`` alone leaves it
    reachable through the MRO. Every class that defines it is stripped, which is
    what makes this fixture reproduce 3.11 rather than merely look like it.
    """

    for owner in Path.__mro__:
        if "is_junction" in owner.__dict__:
            monkeypatch.delattr(owner, "is_junction", raising=False)
    assert not hasattr(Path, "is_junction")


def test_recovery_refuses_a_real_wal_junction_without_reading_outside(
    tmp_path: Path,
    without_is_junction: None,
) -> None:
    """The read direction: an external WAL must not be adopted or copied."""

    outside = tmp_path / "outside-the-database"
    outside.mkdir()
    external_bytes = b"content that lives outside the database\n"
    (outside / "000000000001.wal").write_bytes(external_bytes)

    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _seed_checkpointed_database(database_path)
    protected_before = _protected_digests(database_path)
    shutil.rmtree(database_path / "wal")
    if not _make_junction(database_path / "wal", outside):
        pytest.skip("host cannot create a Windows junction for this safety probe")

    # The junction is real: pathlib alone does not see it as a link on 3.11.
    assert (database_path / "wal").is_symlink() is False

    opened = False

    def open_database(_path: Path):
        nonlocal opened
        opened = True
        raise AssertionError("a junctioned WAL root reached Grafx open")

    report = asyncio.run(
        _recovery(database_path, open_database=open_database).recover_wal_only(BOARD_ID)
    )

    assert report.status == "failed"
    assert report.quarantine_id is None
    assert opened is False
    assert _protected_digests(database_path) == protected_before

    # Nothing from outside the database was copied anywhere under quarantine.
    quarantine_root = database_path.parents[3] / "quarantine"
    copied = (
        [
            path
            for path in quarantine_root.rglob("*")
            if path.is_file() and path.read_bytes() == external_bytes
        ]
        if quarantine_root.exists()
        else []
    )
    assert copied == []
    assert (outside / "000000000001.wal").read_bytes() == external_bytes


def test_restore_plan_refuses_a_real_junctioned_live_wal(
    tmp_path: Path,
    without_is_junction: None,
) -> None:
    """The write direction: plan must refuse before anything is renamed."""

    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _seed_checkpointed_database(database_path)
    quarantine_root = tmp_path / "quarantine"
    _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id="external-junction-plan"
    )
    outside = tmp_path / "outside-the-database"
    outside.mkdir()
    (outside / "decoy.txt").write_bytes(b"must not be written through\n")
    shutil.rmtree(database_path / "wal")
    if not _make_junction(database_path / "wal", outside):
        pytest.skip("host cannot create a Windows junction for this safety probe")
    # Captured with the junction already in place, so the comparison is about
    # what plan did and not about the trap the test just set.
    tree_before = _tree_digests(database_path)

    with pytest.raises(QuarantineRestoreError) as refused:
        _restore(database_path, quarantine_root).plan("external-junction-plan")

    assert refused.value.code is not None
    assert _tree_digests(database_path) == tree_before
    assert sorted(path.name for path in outside.iterdir()) == ["decoy.txt"]


def test_restore_apply_refuses_a_real_junctioned_live_wal(
    tmp_path: Path,
    without_is_junction: None,
) -> None:
    """Apply refuses too, so a junction planted after plan cannot be written through."""

    database_path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _seed_checkpointed_database(database_path)
    quarantine_root = tmp_path / "quarantine"
    _external_wal_quarantine(
        database_path, quarantine_root, quarantine_id="external-junction-apply"
    )

    outside = tmp_path / "outside-the-database"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"external sentinel\n")
    shutil.rmtree(database_path / "wal")
    if not _make_junction(database_path / "wal", outside):
        pytest.skip("host cannot create a Windows junction for this safety probe")
    tree_before = _tree_digests(database_path)

    with pytest.raises(QuarantineRestoreError):
        _restore(database_path, quarantine_root).apply("external-junction-apply")

    assert _tree_digests(database_path) == tree_before
    # The sentinel outside the database is untouched: nothing was written or
    # deleted through the junction.
    assert sentinel.read_bytes() == b"external sentinel\n"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel.txt"]


def test_the_alias_guard_does_not_depend_on_pathlib_is_junction(
    tmp_path: Path,
    without_is_junction: None,
) -> None:
    """The guard answers from lstat, so it holds where pathlib cannot help."""

    from okto_pulse.community.adapters.grafx_graph_recovery import _is_link

    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "as_junction"
    if not _make_junction(link, target):
        pytest.skip("host cannot create a Windows junction for this safety probe")

    assert not hasattr(Path, "is_junction")
    assert link.is_symlink() is False
    assert _is_link(link) is True
    assert _is_link(target) is False
    assert _is_link(tmp_path / "does-not-exist") is False
