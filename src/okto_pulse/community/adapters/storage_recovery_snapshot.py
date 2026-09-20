"""Operator-only recovery of Community's flat upload namespace and erasure flags.

Caller supplies all relational Board IDs under its SQL reservation. This helper
also fences observed orphan namespaces and opaque lifecycle hashes. It copies
unreferenced objects without granting ownership or making them product-visible.
It is not a SQL/file distributed transaction and does not rewrite stored paths.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

from filelock import FileLock

from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory, remove_contained_tree, validate_scope_id,
)
from okto_pulse.community.adapters.relational_recovery_snapshot import (
    _check_time, _deadline, _encode, _path,
)
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage


_FORMAT = "community-storage-recovery/v1"
_CONTROL = ".board_lifecycle"
_MAX_MANIFEST = 16 * 1024 * 1024
_MAX_BYTES = 16 * 1024**3
_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StorageRecoverySnapshot:
    directory: Path
    manifest_sha256: str


def _explicit(value):
    value = Path(value)
    if not value.is_absolute() or ".." in value.parts:
        raise ValueError("storage_recovery_explicit_path_required")
    return _path(value)


def _boards(values):
    if type(values) is not tuple or len(values) > 100_000:
        raise ValueError("storage_recovery_board_selection_invalid")
    for value in values:
        if type(value) is not str or not value or len(value) > 256 or _is_control(value):
            raise ValueError("storage_recovery_board_identity_invalid")
        validate_scope_id(value)
    if len(set(values)) != len(values):
        raise ValueError("storage_recovery_duplicate_board")
    if len({os.path.normcase(value) for value in values}) != len(values):
        raise ValueError("storage_recovery_board_case_alias")
    return tuple(sorted(values))


def _is_control(name):
    return os.path.normcase(name) == os.path.normcase(_CONTROL)


def _limits(max_files, max_bytes):
    if type(max_files) is not int or not 1 <= max_files <= 100_000:
        raise ValueError("storage_recovery_file_limit_invalid")
    if type(max_bytes) is not int or not 0 <= max_bytes <= 1024**4:
        raise ValueError("storage_recovery_byte_limit_invalid")


def _stamp(path):
    state = _path(path).stat()
    return (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns)


def _inventory(root, *, max_files, max_bytes):
    directories, files, hashes, markers = [], {}, set(), set()
    entries, total = 0, 0
    def checked(directory):
        nonlocal entries
        for path in directory.iterdir():
            entries += 1
            if entries > max_files + 200_000:
                raise ValueError("storage_recovery_entry_limit")
            yield _path(path)
    for directory in checked(root):
        if not directory.is_dir():
            raise ValueError("storage_recovery_root_layout_invalid")
        directories.append(directory.name)
        if not _is_control(directory.name):
            _boards((directory.name,))
            hashes.add(hashlib.sha256(directory.name.encode("utf-8")).hexdigest())
        for path in checked(directory):
            if not path.is_file():
                raise ValueError("storage_recovery_nested_or_special_object")
            if _is_control(directory.name):
                match = re.fullmatch(r"([0-9a-f]{64})\.(lock|erased)", os.path.normcase(path.name))
                if not match:
                    raise ValueError("storage_recovery_lifecycle_entry_invalid")
                hashes.add(match[1])
                if match[2] == "lock":
                    continue  # kernel rendezvous files are recreated, not data
                markers.add(match[1])
            identity = _stamp(path)
            total += identity[2]
            files[path.relative_to(root).as_posix()] = identity
            if len(files) > max_files or total > max_bytes:
                raise ValueError("storage_recovery_content_limit")
    for board in directories:
        if not _is_control(board) and hashlib.sha256(board.encode("utf-8")).hexdigest() in markers:
            raise ValueError("storage_recovery_erased_namespace_present")
    return tuple(sorted(directories)), dict(sorted(files.items())), hashes, markers


@contextmanager
def _window(root, board_ids, *, max_files, max_bytes, max_seconds, deadline):
    _check_time(deadline)
    inventory = _inventory(root, max_files=max_files, max_bytes=max_bytes)
    declared = {os.path.normcase(board): board for board in board_ids}
    for observed in inventory[0]:
        expected = declared.get(os.path.normcase(observed))
        if expected is not None and observed != expected:
            raise ValueError("storage_recovery_namespace_case_alias")
    storage = CommunityFileSystemStorage(str(root))
    locks = {storage._lifecycle_paths(board)[0] for board in board_ids}
    locks.update(root / _CONTROL / f"{digest}.lock" for digest in inventory[2])
    control = _path(root / _CONTROL)
    control.mkdir(exist_ok=True)
    with ExitStack() as stack:
        for path in sorted(locks):
            _check_time(deadline)
            remaining = max(0, min(max_seconds, deadline - time.monotonic()))
            stack.enter_context(FileLock(str(_path(path)), timeout=remaining))
        captured = _inventory(root, max_files=max_files, max_bytes=max_bytes)
        held = {path.stem for path in locks}
        if not captured[2] <= held:
            raise ValueError("storage_recovery_namespace_changed_before_fence")
        yield captured


def _stream(source, *, deadline, limit, destination=None):
    digest, size = hashlib.sha256(), 0
    with _path(source).open("rb") as reader:
        while chunk := reader.read(_CHUNK):
            _check_time(deadline)
            digest.update(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("storage_recovery_object_size_changed")
            if destination is not None:
                destination.write(chunk)
    return size, digest.hexdigest()


def _copy(source, target, *, deadline, limit):
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as writer:
        result = _stream(source, deadline=deadline, limit=limit, destination=writer)
        writer.flush()
        os.fsync(writer.fileno())
    return result


def _publish(stage, target):
    _explicit(target)
    if target.exists():
        raise FileExistsError("storage_recovery_destination_exists")
    stage.rename(target)  # caller holds the common operator-root publication lock
    fsync_directory(target.parent)


def create_storage_recovery_snapshot(
    storage_root: Path, recovery_root: Path, *, snapshot_id: str, board_ids: tuple[str, ...],
    max_files: int = 100_000, max_bytes: int = _MAX_BYTES, max_seconds: float = 60,
) -> StorageRecoverySnapshot:
    """Copy all physical objects and erasure flags under Board lifecycle locks.

    Required SQL coordination and startup exclusion belong to the caller. Only
    lifecycle mutex sidecars may be created in the source; content is read-only.
    Recovering an erased namespace with residual files is refused, never repaired.
    """
    deadline = _deadline(max_seconds)
    _limits(max_files, max_bytes)
    board_ids = _boards(board_ids)
    if type(snapshot_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", snapshot_id):
        raise ValueError("storage_recovery_snapshot_id_invalid")
    source, root = _explicit(storage_root), _explicit(recovery_root)
    if not source.is_dir() or not root.is_dir() or root.is_relative_to(source):
        raise ValueError("storage_recovery_separate_existing_roots_required")
    target = _explicit(root / snapshot_id)
    stage = root / f".{snapshot_id}.{secrets.token_hex(12)}.partial"
    with FileLock(str(_path(root / ".storage-recovery.lock")), timeout=max_seconds):
        if target.exists():
            raise FileExistsError("storage_recovery_destination_exists")
        stage.mkdir(mode=0o700)
        try:
            payload = stage / "payload"
            payload.mkdir(mode=0o700)
            with _window(source, board_ids, max_files=max_files, max_bytes=max_bytes, max_seconds=max_seconds, deadline=deadline) as before:
                directories, files, _, markers = before
                for directory in directories:
                    (payload / directory).mkdir(mode=0o700)
                records = []
                for name, stamp in files.items():
                    _check_time(deadline)
                    original, copied = source / name, payload / name
                    size, digest = _copy(original, copied, deadline=deadline, limit=stamp[2])
                    if size != stamp[2] or _stamp(original) != stamp:
                        raise ValueError("storage_recovery_object_changed")
                    os.utime(copied, ns=(stamp[3], stamp[3]))
                    records.append({"path": name, "size": size, "sha256": digest, "mtime_ns": stamp[3]})
                if _inventory(source, max_files=max_files, max_bytes=max_bytes) != before:
                    raise ValueError("storage_recovery_namespace_changed")
                for record in records:
                    if _stream(source / record["path"], deadline=deadline, limit=record["size"]) != (record["size"], record["sha256"]):
                        raise ValueError("storage_recovery_object_changed")
                manifest = {"format": _FORMAT, "snapshot_id": snapshot_id, "source_root": str(source),
                    "board_ids": list(board_ids), "directories": list(directories), "files": records,
                    "erased_hashes": sorted(markers)}
                encoded = _encode(manifest)
                if len(encoded) > _MAX_MANIFEST:
                    raise ValueError("storage_recovery_manifest_limit")
                with (stage / "manifest.json").open("xb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                for directory in directories:
                    fsync_directory(payload / directory)
                fsync_directory(payload)
                fsync_directory(stage)
                _check_time(deadline)
                _publish(stage, target)
            return StorageRecoverySnapshot(target, hashlib.sha256(encoded).hexdigest())
        finally:
            if stage.exists():
                remove_contained_tree(stage, base_dir=root)


def verify_storage_recovery_snapshot(snapshot: StorageRecoverySnapshot, *, max_seconds: float = 60) -> dict:
    """Authenticate bytes and exact namespace, with no live-source dependency."""
    deadline = _deadline(max_seconds)
    root = _explicit(snapshot.directory)
    manifest_path = _path(root / "manifest.json")
    if manifest_path.stat().st_size > _MAX_MANIFEST:
        raise ValueError("storage_recovery_manifest_limit")
    encoded = manifest_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != snapshot.manifest_sha256:
        raise ValueError("storage_recovery_manifest_hash_mismatch")
    manifest = json.loads(encoded)
    if (type(manifest) is not dict or set(manifest) != {"format", "snapshot_id", "source_root", "board_ids", "directories", "files", "erased_hashes"}
        or manifest["format"] != _FORMAT or manifest["snapshot_id"] != root.name):
        raise ValueError("storage_recovery_manifest_invalid")
    if type(manifest["board_ids"]) is not list or list(_boards(tuple(manifest["board_ids"]))) != manifest["board_ids"]:
        raise ValueError("storage_recovery_manifest_boards_invalid")
    payload = _path(root / "payload")
    observed = _inventory(payload, max_files=100_000, max_bytes=1024**4)
    if list(observed[0]) != manifest["directories"] or sorted(observed[3]) != manifest["erased_hashes"]:
        raise ValueError("storage_recovery_manifest_namespace_mismatch")
    if type(manifest["files"]) is not list or len(manifest["files"]) != len(observed[1]):
        raise ValueError("storage_recovery_manifest_files_mismatch")
    for record, (name, stamp) in zip(manifest["files"], observed[1].items(), strict=True):
        if (type(record) is not dict or set(record) != {"path", "size", "sha256", "mtime_ns"}
            or record["path"] != name or type(record["size"]) is not int or record["size"] != stamp[2]
            or type(record["mtime_ns"]) is not int):
            raise ValueError("storage_recovery_manifest_object_invalid")
        if _stream(payload / name, deadline=deadline, limit=record["size"]) != (record["size"], record["sha256"]):
            raise ValueError("storage_recovery_object_hash_mismatch")
    return manifest


def restore_storage_recovery_snapshot(
    snapshot: StorageRecoverySnapshot, target_root: Path, *, current_storage_root: Path,
    max_seconds: float = 60,
) -> Path:
    """Restore only to a new isolated root, respecting current erasure state.

    The caller must supply the authoritative CURRENT storage root. A snapshot
    cannot authorize resurrection after any later erasure; newer markers refuse
    the entire restore. Persisted absolute SQL paths are not rewritten here.
    """
    deadline = _deadline(max_seconds)
    manifest = verify_storage_recovery_snapshot(snapshot, max_seconds=max_seconds)
    current, target = _explicit(current_storage_root), _explicit(target_root)
    if current != _explicit(manifest["source_root"]):
        raise ValueError("storage_recovery_current_authority_root_mismatch")
    if not current.is_dir() or not target.parent.is_dir() or target.exists():
        raise FileExistsError("storage_recovery_restore_requires_new_root")
    if target.is_relative_to(current) or target.is_relative_to(_explicit(snapshot.directory)):
        raise ValueError("storage_recovery_restore_root_overlap")
    stage = target.parent / f".{target.name}.{secrets.token_hex(12)}.restore"
    board_ids = _boards(tuple(sorted(set(manifest["board_ids"]) | {
        name for name in manifest["directories"] if not _is_control(name)
    })))
    with FileLock(str(_path(target.parent / ".storage-recovery-restore.lock")), timeout=max_seconds):
        if target.exists():
            raise FileExistsError("storage_recovery_restore_requires_new_root")
        with _window(current, board_ids, max_files=100_000, max_bytes=1024**4, max_seconds=max_seconds, deadline=deadline) as live:
            if not live[3] <= set(manifest["erased_hashes"]):
                raise ValueError("storage_recovery_newer_erasure_refused")
            stage.mkdir(mode=0o700)
            try:
                for directory in manifest["directories"]:
                    (stage / directory).mkdir(mode=0o700)
                for record in manifest["files"]:
                    copied = stage / record["path"]
                    result = _copy(_path(snapshot.directory / "payload" / record["path"]), copied, deadline=deadline, limit=record["size"])
                    if result != (record["size"], record["sha256"]):
                        raise ValueError("storage_recovery_restore_hash_mismatch")
                    os.utime(copied, ns=(record["mtime_ns"], record["mtime_ns"]))
                now = _inventory(current, max_files=100_000, max_bytes=1024**4)
                if now[3] != live[3] or not now[2] <= live[2]:
                    raise ValueError("storage_recovery_privacy_state_changed")
                for directory in manifest["directories"]:
                    fsync_directory(stage / directory)
                fsync_directory(stage)
                _check_time(deadline)
                _publish(stage, target)
                return target
            finally:
                if stage.exists():
                    remove_contained_tree(stage, base_dir=target.parent)
