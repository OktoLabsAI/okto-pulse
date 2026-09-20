"""Internal SQLite recovery artifacts, never Board attachments or product APIs.

A recovery database can contain several Boards and credentials. The caller must
provide an operator-protected recovery directory; it must never serve these files
through Board history/attachment routes. No default runtime paths, CLI, startup
hook or in-place restore is provided. The Board-scoped historical archive is a
separate concern. Call from the migration mechanism outside an async event loop.
"""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
from typing import Callable

from filelock import FileLock

from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory,
    reject_filesystem_alias_ancestry,
    remove_contained_tree,
)


_FORMAT = "relational-recovery-snapshot/v1"
_DATABASE = "database.sqlite3"
_MANIFEST = "manifest.json"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MANIFEST_KEYS = frozenset({"format", "snapshot_id", "source_name", "created_at", "database_file",
    "database_sha256", "schema_sha256", "table_counts", "user_version", "application_id"})


@dataclass(frozen=True, slots=True)
class SqliteRecoverySnapshot:
    directory: Path
    manifest_sha256: str
    database_sha256: str


def _path(value: Path) -> Path:
    path = Path(os.path.abspath(value))
    reject_filesystem_alias_ancestry(path)
    return path


def _sidecars_absent(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists() or candidate.is_symlink():
            raise ValueError("relational_snapshot_unexpected_sidecar")


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _deadline(seconds: float) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 3600:
        raise ValueError("relational_snapshot_timeout_invalid")
    return time.monotonic() + seconds


def _check_time(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise TimeoutError("relational_snapshot_timeout")


def _facts(connection: sqlite3.Connection, deadline: float) -> dict:
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    _check_time(deadline)
    if connection.execute("PRAGMA integrity_check").fetchmany(2) != [("ok",)]:
        raise ValueError("relational_snapshot_integrity_failed")
    schema_hash = hashlib.sha256()
    tables = []
    schema_bytes = 0
    for index, row in enumerate(connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name")):
        encoded = _encode(row)
        schema_bytes += len(encoded)
        if index >= 10_000 or schema_bytes > 16 * 1024 * 1024:
            raise ValueError("relational_snapshot_schema_limit")
        schema_hash.update(encoded + b"\n")
        if row[0] == "table":
            tables.append(row[1])
        _check_time(deadline)
    counts = []
    for name in sorted(tables):
        quoted = '"' + name.replace('"', '""') + '"'
        counts.append([name, connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]])
        _check_time(deadline)
    return {
        "schema_sha256": schema_hash.hexdigest(), "table_counts": counts,
        "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "application_id": connection.execute("PRAGMA application_id").fetchone()[0],
    }


def _readonly(path: Path, *, immutable: bool = False):
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    return sqlite3.connect(uri, uri=True, timeout=1.0)


def create_sqlite_recovery_snapshot(
    source_database: Path, recovery_directory: Path, *, snapshot_id: str,
    max_seconds: float = 60, progress: Callable[[int, int, int], None] | None = None,
) -> SqliteRecoverySnapshot:
    """Capture committed SQLite+WAL state through the online backup API.

    A read transaction pins the database before backup and census, including
    concurrent WAL writers. Cutover still needs a write fence to bind this
    snapshot to its own mutations; this function does not acquire that fence.
    Existing snapshot IDs are never overwritten or silently reused.
    """
    deadline = _deadline(max_seconds)
    if not isinstance(snapshot_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", snapshot_id):
        raise ValueError("relational_snapshot_id_invalid")
    source, root = _path(source_database), _path(recovery_directory)
    if not source.is_file() or not root.is_dir():
        raise ValueError("relational_snapshot_explicit_paths_required")
    final = root / snapshot_id
    staging = root / f".{snapshot_id}.{secrets.token_hex(12)}.partial"

    def backup_progress(status, remaining, total):
        _check_time(deadline)
        if progress is not None:
            progress(status, remaining, total)

    lock_path = _path(root / ".relational-recovery.lock")
    with FileLock(str(lock_path), timeout=max_seconds):
        reject_filesystem_alias_ancestry(final)
        if final.exists():
            raise FileExistsError("relational_snapshot_already_exists")
        staging.mkdir(mode=0o700)
        try:
            destination = staging / _DATABASE
            with closing(_readonly(source)) as reader:
                reader.execute("BEGIN")
                # Python's transaction flag alone does not pin a SQLite read.
                reader.execute("SELECT count(*) FROM sqlite_schema").fetchone()
                with closing(sqlite3.connect(destination)) as writer:
                    reader.backup(writer, pages=128, progress=backup_progress, sleep=0.01)
                    if writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                        raise ValueError("relational_snapshot_standalone_mode_failed")
                    facts = _facts(reader, deadline)
                    if _facts(writer, deadline) != facts:
                        raise ValueError("relational_snapshot_census_mismatch")
            _sidecars_absent(destination)
            with destination.open("r+b") as handle:
                os.fsync(handle.fileno())
            database_hash = _digest(destination)
            manifest = {"format": _FORMAT, "snapshot_id": snapshot_id, "source_name": source.name,
                "created_at": datetime.now(timezone.utc).isoformat(), "database_file": _DATABASE,
                "database_sha256": database_hash, **facts}
            encoded = _encode(manifest)
            if len(encoded) > _MAX_MANIFEST_BYTES:
                raise ValueError("relational_snapshot_manifest_limit")
            with (staging / _MANIFEST).open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(staging)
            _check_time(deadline)
            # All cooperating creators use the same operator-root lock. Never
            # replace an existing artifact, even if its directory is empty.
            if final.exists():
                raise FileExistsError("relational_snapshot_already_exists")
            staging.rename(final)
            fsync_directory(root)
            return SqliteRecoverySnapshot(final, hashlib.sha256(encoded).hexdigest(), database_hash)
        finally:
            if staging.exists():
                remove_contained_tree(staging, base_dir=root)


def verify_sqlite_recovery_snapshot(snapshot: SqliteRecoverySnapshot, *, max_seconds: float = 60) -> dict:
    """Verify against the hashes retained by the trusted migration caller.

    Never obtain the expected manifest digest from the artifact being verified.
    This verifies recovery integrity, not migration readiness or source authority.
    """
    deadline = _deadline(max_seconds)
    directory = _path(snapshot.directory)
    manifest_path, database = _path(directory / _MANIFEST), _path(directory / _DATABASE)
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("relational_snapshot_manifest_limit")
    encoded = manifest_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != snapshot.manifest_sha256:
        raise ValueError("relational_snapshot_manifest_hash_mismatch")
    manifest = json.loads(encoded)
    if (not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS
            or manifest["format"] != _FORMAT or manifest["database_file"] != _DATABASE
            or manifest["snapshot_id"] != directory.name):
        raise ValueError("relational_snapshot_manifest_invalid")
    _sidecars_absent(database)
    if _digest(database) != snapshot.database_sha256 or manifest["database_sha256"] != snapshot.database_sha256:
        raise ValueError("relational_snapshot_database_hash_mismatch")
    with closing(_readonly(database, immutable=True)) as reader:
        facts = _facts(reader, deadline)
    if any(manifest[key] != value for key, value in facts.items()):
        raise ValueError("relational_snapshot_census_mismatch")
    return manifest


def restore_sqlite_recovery_snapshot(
    snapshot: SqliteRecoverySnapshot, target_database: Path, *, max_seconds: float = 60,
) -> None:
    """Restore only to a new file, for an isolated recovery/rollback environment.

    Copy the verified standalone artifact, never a live SQLite main file. Final
    publication uses an exclusive hard link in the destination filesystem; it
    cannot overwrite an existing database. No runtime process is restarted.
    """
    deadline = _deadline(max_seconds)
    verify_sqlite_recovery_snapshot(snapshot, max_seconds=max_seconds)
    target = _path(target_database)
    if target.exists() or not target.parent.is_dir():
        raise FileExistsError("relational_restore_requires_new_target")
    _sidecars_absent(target)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as writer, (_path(snapshot.directory) / _DATABASE).open("rb") as reader:
            while chunk := reader.read(1024 * 1024):
                _check_time(deadline)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if _digest(temporary) != snapshot.database_sha256:
            raise ValueError("relational_restore_copy_hash_mismatch")
        _check_time(deadline)
        reject_filesystem_alias_ancestry(target)
        _sidecars_absent(target)
        os.link(temporary, target)
        fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
