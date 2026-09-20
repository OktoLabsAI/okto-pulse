"""Physical storage recovery, real lifecycle fences and no erasure resurrection."""

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from okto_pulse.community.adapters import storage_recovery_snapshot as recovery
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage


@pytest.fixture
def source(tmp_path):
    root, backups = tmp_path / "uploads", tmp_path / "backups"
    root.mkdir()
    backups.mkdir()
    storage = CommunityFileSystemStorage(str(root))
    attachment = asyncio.run(storage.save("a", "attachment.bin", b"\x00\xff\r\nattachment"))
    archive = asyncio.run(storage.save("b", "historical-archive.json", b'{ "history": "e\xcc\x81" }\r\n'))
    orphan = asyncio.run(storage.save("orphan", "unreferenced.bin", b"retain without inventing owner"))
    asyncio.run(storage.purge_board("deleted"))
    return root, backups, storage, (Path(attachment), Path(archive), Path(orphan))


def capture(source, **kwargs):
    return recovery.create_storage_recovery_snapshot(source[0], source[1], snapshot_id="s1", board_ids=("a", "b"), **kwargs)


def test_roundtrip_preserves_all_objects_markers_and_unowned_namespace(source, tmp_path):
    root, _, _, objects = source
    before = {path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns) for path in objects}
    snapshot = capture(source)
    manifest = recovery.verify_storage_recovery_snapshot(snapshot)
    assert manifest["board_ids"] == ["a", "b"]
    assert "orphan" in manifest["directories"]
    erased_hash = hashlib.sha256(b"deleted").hexdigest()
    assert manifest["erased_hashes"] == [erased_hash]
    assert not list((snapshot.directory / "payload").rglob("*.lock"))
    restored = recovery.restore_storage_recovery_snapshot(snapshot, tmp_path / "restored", current_storage_root=root)
    for name, (data, modified) in before.items():
        assert (restored / name).read_bytes() == data
        assert (restored / name).stat().st_mtime_ns == modified
        assert (root / name).read_bytes() == data
    with pytest.raises(RuntimeError, match="permanently erased"):
        asyncio.run(CommunityFileSystemStorage(str(restored)).save("deleted", "new", b"forbidden"))

    asyncio.run(source[2].purge_board("a"))
    with pytest.raises(ValueError, match="newer_erasure_refused"):
        recovery.restore_storage_recovery_snapshot(snapshot, tmp_path / "after-later-erasure", current_storage_root=source[0])
    with pytest.raises(FileExistsError):
        capture(source)
    with pytest.raises(FileExistsError):
        recovery.restore_storage_recovery_snapshot(snapshot, restored, current_storage_root=root)


def test_capture_blocks_real_save_from_another_process_and_releases_it(source, monkeypatch):
    script = """
import asyncio, sys
from filelock import FileLock, Timeout
from okto_pulse.community.adapters import storage as module
module.FileLock = lambda path, timeout: FileLock(path, timeout=0.05)
try:
    asyncio.run(module.CommunityFileSystemStorage(sys.argv[1]).save('a', 'late.bin', b'late'))
    print('saved')
except Timeout:
    print('blocked')
"""
    def attempt():
        return subprocess.run([sys.executable, "-c", script, str(source[0])],
            capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    original, tried = recovery._copy, []
    def checking_copy(*args, **kwargs):
        if not tried:
            tried.append(attempt())
        return original(*args, **kwargs)
    monkeypatch.setattr(recovery, "_copy", checking_copy)
    capture(source)
    assert tried == ["blocked"]
    assert attempt() == "saved"


def test_later_erasure_blocks_restore_and_cannot_be_bypassed_with_empty_root(source, tmp_path):
    snapshot = capture(source)
    asyncio.run(source[2].purge_board("a"))
    target = tmp_path / "restored"
    with pytest.raises(ValueError, match="newer_erasure_refused"):
        recovery.restore_storage_recovery_snapshot(snapshot, target, current_storage_root=source[0])
    assert not target.exists()
    wrong = tmp_path / "empty"
    wrong.mkdir()
    with pytest.raises(ValueError, match="authority_root_mismatch"):
        recovery.restore_storage_recovery_snapshot(snapshot, target, current_storage_root=wrong)


def test_erased_namespace_residue_is_not_copied_or_silently_deleted(source):
    residual = source[0] / "deleted"
    residual.mkdir()
    content = residual / "residue"
    content.write_bytes(b"needs authorized erasure reconciliation")
    with pytest.raises(ValueError, match="erased_namespace_present"):
        capture(source)
    assert content.read_bytes() == b"needs authorized erasure reconciliation"
    assert not (source[1] / "s1").exists()


@pytest.mark.parametrize("kind", ["manifest", "object", "extra_object"])
def test_tampering_prevents_restore_before_target_creation(source, tmp_path, kind):
    snapshot = capture(source)
    if kind == "manifest":
        with (snapshot.directory / "manifest.json").open("ab") as handle:
            handle.write(b" ")
    elif kind == "object":
        relative = source[3][0].relative_to(source[0])
        path = snapshot.directory / "payload" / relative
        path.write_bytes(b"x" * path.stat().st_size)
    else:
        (snapshot.directory / "payload" / "a" / "extra").write_bytes(b"unlisted")
    with pytest.raises(ValueError):
        recovery.restore_storage_recovery_snapshot(snapshot, tmp_path / "restored", current_storage_root=source[0])
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("phase", ["capture", "restore"])
def test_copy_failure_keeps_sources_and_removes_unpublished_payload(source, tmp_path, monkeypatch, phase):
    snapshot = capture(source) if phase == "restore" else None
    original, calls = recovery._copy, []
    def failing(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("copy interrupted")
        return original(*args, **kwargs)
    monkeypatch.setattr(recovery, "_copy", failing)
    with pytest.raises(OSError, match="copy interrupted"):
        if snapshot:
            recovery.restore_storage_recovery_snapshot(snapshot, tmp_path / "restored", current_storage_root=source[0])
        else:
            capture(source)
    assert all(path.exists() for path in source[3])
    assert not list(source[1].glob("*.partial"))
    assert not list(tmp_path.glob("*.restore"))
    assert not (tmp_path / "restored").exists()
    assert asyncio.run(source[2].save("a", "after-error", b"lock released"))


def test_uncooperative_source_change_is_detected(source, monkeypatch):
    original, changed = recovery._copy, []
    def mutating(*args, **kwargs):
        result = original(*args, **kwargs)
        if not changed:
            changed.append(True)
            source[3][0].write_bytes(b"concurrent replacement")
        return result
    monkeypatch.setattr(recovery, "_copy", mutating)
    with pytest.raises(ValueError, match="changed"):
        capture(source)
    assert not (source[1] / "s1").exists()


def test_new_privacy_marker_during_restore_prevents_publication(source, tmp_path, monkeypatch):
    snapshot = capture(source)
    original, changed = recovery._copy, []
    def mutating(*args, **kwargs):
        result = original(*args, **kwargs)
        if not changed:
            changed.append(True)
            # Fault injection bypasses the production lifecycle lock on purpose.
            digest = hashlib.sha256(b"another-deleted-board").hexdigest()
            (source[0] / ".board_lifecycle" / f"{digest}.erased").write_bytes(b"erased\n")
        return result
    monkeypatch.setattr(recovery, "_copy", mutating)
    with pytest.raises(ValueError, match="privacy_state_changed"):
        recovery.restore_storage_recovery_snapshot(snapshot, tmp_path / "restored", current_storage_root=source[0])
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("limit", [{"max_files": 1}, {"max_bytes": 1}])
def test_content_limits_fail_without_partial_success(source, limit):
    with pytest.raises(ValueError, match="content_limit"):
        capture(source, **limit)
    assert not (source[1] / "s1").exists()


@pytest.mark.parametrize("kind", ["object", "namespace", "control"])
def test_aliases_cannot_escape_storage(source, tmp_path, kind):
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"private unrelated bytes")
    if kind == "object":
        link = source[0] / "a" / "alias"
    elif kind == "control":
        link = source[0] / ".board_lifecycle" / ("0" * 64 + ".erased")
    else:
        link = source[0] / "alias"
    try:
        link.symlink_to(tmp_path if kind == "namespace" else unrelated, target_is_directory=kind == "namespace")
    except OSError as failure:
        pytest.skip(f"symlink unavailable: {failure}")
    with pytest.raises(ValueError):
        capture(source)
    assert unrelated.read_bytes() == b"private unrelated bytes"


def test_manifest_path_traversal_rejected_even_with_matching_digest(source):
    snapshot = capture(source)
    path = snapshot.directory / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"][0]["path"] = "../../foreign"
    encoded = json.dumps(manifest).encode()
    path.write_bytes(encoded)
    supplied = replace(snapshot, manifest_sha256=hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ValueError, match="manifest_object_invalid"):
        recovery.verify_storage_recovery_snapshot(supplied)


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive namespace contract")
def test_case_variant_lifecycle_directory_and_marker_keep_erasure(source, tmp_path):
    original = source[0] / ".board_lifecycle"
    temporary = source[0] / "rename-control"
    upper = source[0] / ".BOARD_LIFECYCLE"
    original.rename(temporary)
    temporary.rename(upper)
    marker = next(upper.glob("*.erased"))
    intermediate = upper / "rename-marker"
    marker.rename(intermediate)
    intermediate.rename(upper / marker.name.upper())
    snapshot = capture(source)
    restored = recovery.restore_storage_recovery_snapshot(snapshot, tmp_path / "restored", current_storage_root=source[0])
    with pytest.raises(RuntimeError, match="permanently erased"):
        asyncio.run(CommunityFileSystemStorage(str(restored)).save("deleted", "new", b"forbidden"))

    asyncio.run(source[2].purge_board("a"))
    with pytest.raises(ValueError, match="newer_erasure_refused"):
        recovery.restore_storage_recovery_snapshot(snapshot, tmp_path / "after-later-erasure", current_storage_root=source[0])


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive namespace contract")
@pytest.mark.parametrize("case", ["declared", "physical"])
def test_case_aliased_board_identity_is_not_inferred(source, case):
    if case == "declared":
        with pytest.raises(ValueError, match="board_case_alias"):
            recovery.create_storage_recovery_snapshot(source[0], source[1], snapshot_id="s1", board_ids=("a", "A"))
    else:
        path = source[0] / "a"
        temporary = source[0] / "rename-board"
        path.rename(temporary)
        temporary.rename(source[0] / "A")
        with pytest.raises(ValueError, match="namespace_case_alias"):
            capture(source)
    assert not (source[1] / "s1").exists()


def test_borrowed_restore_guard_expires_without_open_output_lockfiles(source, tmp_path):
    snapshot = capture(source)
    with recovery.storage_recovery_restore_window(snapshot, current_storage_root=source[0]) as guard:
        guard.copy_into_new_root(tmp_path / "private-candidate")
        guard.validate()
        assert not (tmp_path / ".storage-recovery-restore.lock").exists()
    with pytest.raises(ValueError, match="guard_expired"):
        guard.copy_into_new_root(tmp_path / "after-exit")
    assert not (tmp_path / "after-exit").exists()


@pytest.mark.parametrize("destination", ["uploads", "snapshot"])
def test_restore_overlap_is_refused_before_creating_any_sidecar(source, destination):
    snapshot = capture(source)
    root = source[0] if destination == "uploads" else snapshot.directory
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="restore_root_overlap"):
        recovery.restore_storage_recovery_snapshot(snapshot, root / "invalid-restore", current_storage_root=source[0])
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
    assert not (root / "invalid-restore").exists()
