"""Grafx implementation of the board ``GraphRecovery`` port.

Grafx owns the safe WAL algorithm: writable open scans the published
checkpoint, preserves damaged ranges in its native forensic quarantine, cuts
only after the evidence is durable and replays complete commits. Pulse must
not replace that algorithm by moving whole segments, because doing so removes
the lineage Grafx needs to decide what is safe.

The Pulse port also promises an operator-restorable quarantine. Native Grafx
entries are byte ranges and deliberately cannot overwrite an existing WAL
segment, so this adapter takes a durable, whole-segment snapshot immediately
before writable open. The snapshot is published only when recovery changed
the WAL (or when open may have failed after starting recovery). A healthy open
discards its private pending copy. Identity, catalog, heap, indexes and
``control/commit.state`` are never moved, renamed or deleted here, so the
observable ``main_untouched`` invariant remains true.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from okto_pulse.core.kg.interfaces.graph_recovery import WalRecoveryReport

from okto_pulse.community.adapters.filesystem_erasure import fsync_directory
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error

_MANIFEST_FORMAT = "pulse_grafx_quarantine/1"
_MANIFEST_FILE = "manifest.json"
_PAYLOAD_DIRECTORY = "payload"
_MOVE_ATTEMPTS = 3
_MOVE_BACKOFF_SECONDS = 0.05
_WAL_SEGMENT = re.compile(r"[0-9]{12}\.wal\Z")

PathResolver = Callable[[str], str | os.PathLike[str]]
DatabaseOpener = Callable[[Path], Any]
BoardCloser = Callable[[str], None]
FenceRevalidator = Callable[[str, str], None]
MutationGuard = Callable[[str], AbstractContextManager[None]]


@dataclass(frozen=True)
class _WalSnapshot:
    quarantine_id: str
    pending_dir: Path
    final_dir: Path
    manifest: dict[str, object]
    files: tuple[dict[str, object], ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    """Recognize both ordinary links and Windows directory junctions."""

    junction_probe = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(callable(junction_probe) and junction_probe())


def _failure_text(failure: BaseException, *, operation: str) -> str:
    mapped = map_grafx_error(failure, operation=operation)
    return f"{type(mapped).__name__}: {mapped}"


def _replace_directory_with_retry(source: Path, destination: Path) -> None:
    last_failure: PermissionError | None = None
    for attempt in range(_MOVE_ATTEMPTS):
        try:
            if destination.exists():
                raise FileExistsError(destination)
            os.replace(source, destination)
            return
        except PermissionError as failure:
            last_failure = failure
            if attempt + 1 < _MOVE_ATTEMPTS:
                gc.collect()
                time.sleep(_MOVE_BACKOFF_SECONDS * (2**attempt))
    assert last_failure is not None
    raise last_failure


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _database_path(value: str | os.PathLike[str]) -> Path:
    if os.fspath(value) == ":memory:":
        raise ValueError("Grafx wal-only recovery requires persistent storage")
    path = Path(value).expanduser().resolve(strict=False)
    if path == path.parent or not path.name:
        raise ValueError("Grafx database path is too broad")
    return path


def _quarantine_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    if path == path.parent or not path.name:
        raise ValueError("Grafx quarantine path is too broad")
    return path


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_disjoint(database_path: Path, quarantine_root: Path) -> None:
    if _contains(database_path, quarantine_root) or _contains(
        quarantine_root, database_path
    ):
        raise ValueError("Grafx database and quarantine roots must be disjoint")


def _close_database(database: Any) -> None:
    close = getattr(database, "close", None)
    if not callable(close):
        raise TypeError("Grafx open probe did not return a closable database")
    close()
    if getattr(database, "close_complete", True) is not True:
        raise RuntimeError("Grafx open probe did not complete close")


def _probe(open_database: DatabaseOpener, path: Path) -> None:
    database = open_database(path)
    primary: BaseException | None = None
    try:
        report = database.verify("all")
        if getattr(report, "clean", None) is not True:
            findings = getattr(report, "findings", ())
            raise RuntimeError(
                "Grafx verification was not clean "
                f"(findings={len(findings) if isinstance(findings, tuple) else 'unknown'})"
            )
    except BaseException as failure:  # noqa: BLE001 - cleanup preserves primary
        primary = failure
    try:
        _close_database(database)
    except BaseException as close_failure:  # noqa: BLE001
        if primary is None:
            primary = close_failure
        else:
            primary.add_note(
                "Grafx recovery probe close also failed: "
                f"{type(close_failure).__name__}: {close_failure}"
            )
    if primary is not None:
        raise primary


def _wal_segments(database_path: Path) -> tuple[Path, ...]:
    wal_root = database_path / "wal"
    if not wal_root.exists():
        return ()
    if _is_link(wal_root) or not wal_root.is_dir():
        raise OSError("Grafx WAL root is not a real directory")
    segments: list[Path] = []
    for candidate in sorted(wal_root.iterdir(), key=lambda item: item.name):
        if (
            _is_link(candidate)
            or not candidate.is_file()
            or _WAL_SEGMENT.fullmatch(candidate.name) is None
        ):
            raise OSError(
                f"unexpected entry in Grafx WAL namespace: {candidate.name!r}"
            )
        segments.append(candidate)
    return tuple(segments)


def _copy_file_durable(source: Path, destination: Path) -> tuple[int, str]:
    before = source.stat()
    if _is_link(source) or not source.is_file():
        raise OSError(f"Grafx WAL source is not a regular file: {source.name!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
            digest.update(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    after = source.stat()
    copied_digest = digest.hexdigest()
    if (
        _is_link(source)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or destination.stat().st_size != before.st_size
        or _sha256_file(source) != copied_digest
        or _sha256_file(destination) != copied_digest
    ):
        raise OSError(f"Grafx WAL changed while snapshotting {source.name!r}")
    return before.st_size, copied_digest


def _prepare_wal_snapshot(
    *,
    board_id: str,
    database_path: Path,
    quarantine_root: Path,
) -> _WalSnapshot | None:
    segments = _wal_segments(database_path)
    if not segments:
        return None
    quarantine_root.mkdir(parents=True, exist_ok=True)
    fsync_directory(quarantine_root)
    fsync_directory(quarantine_root.parent)
    quarantine_id = (
        f"grafx-wal-{_utc_now().strftime('%Y%m%dT%H%M%S%f')}-{secrets.token_hex(8)}"
    )
    pending_dir = quarantine_root / f".{quarantine_id}.pending"
    final_dir = quarantine_root / quarantine_id
    pending_dir.mkdir(parents=False, exist_ok=False)
    fsync_directory(pending_dir)
    fsync_directory(quarantine_root)
    files: list[dict[str, object]] = []
    try:
        for source in segments:
            relative = source.relative_to(database_path).as_posix()
            destination = pending_dir / _PAYLOAD_DIRECTORY / Path(relative)
            size, digest = _copy_file_durable(source, destination)
            files.append(
                {
                    "name": source.name,
                    "relative_path": relative,
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        payload_root = pending_dir / _PAYLOAD_DIRECTORY
        payload_wal = payload_root / "wal"
        fsync_directory(payload_wal)
        fsync_directory(payload_root)
        fsync_directory(pending_dir)
        manifest: dict[str, object] = {
            "format": _MANIFEST_FORMAT,
            "kind": "grafx_wal_only",
            "quarantine_id": quarantine_id,
            "board_id": board_id,
            "database_path": str(database_path),
            "created_at": _utc_now().isoformat(),
            "main_untouched": True,
            "complete": False,
            "phase": "prepared_before_grafx_open",
            "files": files,
            "files_moved": [],
            "native_quarantine_ids": [],
            "error": None,
        }
        _write_json_atomic(pending_dir / _MANIFEST_FILE, manifest)
        return _WalSnapshot(
            quarantine_id=quarantine_id,
            pending_dir=pending_dir,
            final_dir=final_dir,
            manifest=manifest,
            files=tuple(files),
        )
    except BaseException:
        try:
            shutil.rmtree(pending_dir)
            fsync_directory(quarantine_root)
        except OSError:
            pass
        raise


def _snapshot_file_names(snapshot: _WalSnapshot | None) -> tuple[str, ...]:
    if snapshot is None:
        return ()
    return tuple(str(item["relative_path"]) for item in snapshot.files)


def _publish_snapshot(
    snapshot: _WalSnapshot,
    *,
    recovery_report: Any | None,
    phase: str,
    error: str | None,
) -> None:
    findings = getattr(recovery_report, "findings", ()) if recovery_report else ()
    native_ids = tuple(
        dict.fromkeys(
            value
            for finding in findings
            if (value := getattr(finding, "quarantine", ""))
        )
    )
    manifest = dict(snapshot.manifest)
    manifest.update(
        {
            "complete": True,
            "phase": phase,
            "finished_at": _utc_now().isoformat(),
            "files_moved": list(_snapshot_file_names(snapshot)),
            "native_quarantine_ids": list(native_ids),
            "recovery_outcome": getattr(recovery_report, "outcome", None),
            "records_replayed": getattr(recovery_report, "records_replayed", 0),
            "records_discarded": getattr(recovery_report, "records_discarded", 0),
            "error": error,
        }
    )
    _write_json_atomic(snapshot.pending_dir / _MANIFEST_FILE, manifest)
    _replace_directory_with_retry(snapshot.pending_dir, snapshot.final_dir)
    fsync_directory(snapshot.final_dir)
    fsync_directory(snapshot.final_dir.parent)


def _discard_snapshot(snapshot: _WalSnapshot | None) -> None:
    if snapshot is None or not snapshot.pending_dir.exists():
        return
    try:
        shutil.rmtree(snapshot.pending_dir)
        fsync_directory(snapshot.pending_dir.parent)
    except OSError:
        # Pending names are not accepted by QuarantineRestore and cannot be
        # mistaken for successful snapshots.
        pass


class CommunityGrafxGraphRecovery:
    """Delegate WAL recovery to Grafx and publish a restorable Pulse snapshot."""

    def __init__(
        self,
        *,
        quarantine_root: str | os.PathLike[str],
        database_path_resolver: PathResolver,
        open_database: DatabaseOpener,
        close_board: BoardCloser,
        revalidate_fence: FenceRevalidator,
        mutation_guard: MutationGuard,
    ) -> None:
        self._quarantine_root = _quarantine_path(quarantine_root)
        self._database_path_resolver = database_path_resolver
        self._open_database = open_database
        self._close_board = close_board
        self._revalidate_fence = revalidate_fence
        self._mutation_guard = mutation_guard

    async def recover_wal_only(self, board_id: str) -> WalRecoveryReport:
        try:
            path = _database_path(self._database_path_resolver(board_id))
            _require_disjoint(path, self._quarantine_root)
        except BaseException as failure:  # noqa: BLE001 - report boundary
            return self._failed(board_id, failure, operation="resolve_database_path")
        if not path.is_dir():
            return WalRecoveryReport(
                board_id=board_id,
                status="skipped",
                main_untouched=True,
                reason=f"Grafx database missing at {path}",
            )
        try:
            with self._mutation_guard(board_id):
                return self._recover_guarded(board_id, path)
        except BaseException as failure:  # noqa: BLE001 - report boundary
            return self._failed(board_id, failure, operation="recover_wal_only_guard")

    def _recover_guarded(self, board_id: str, path: Path) -> WalRecoveryReport:
        snapshot: _WalSnapshot | None = None
        database: Any | None = None
        recovery_report: Any | None = None
        verification: Any | None = None
        primary: BaseException | None = None
        try:
            self._close_board(board_id)
            self._revalidate_fence(board_id, "wal_recovery_snapshot")
            snapshot = _prepare_wal_snapshot(
                board_id=board_id,
                database_path=path,
                quarantine_root=self._quarantine_root,
            )
            self._revalidate_fence(board_id, "wal_recovery_open")
            database = self._open_database(path)
            recovery_report = getattr(database, "recovery_report", None)
            verification = database.verify("all")
        except BaseException as failure:  # noqa: BLE001 - cleanup preserves primary
            primary = failure

        if database is not None:
            try:
                _close_database(database)
            except BaseException as close_failure:  # noqa: BLE001
                if primary is None:
                    primary = close_failure
                else:
                    primary.add_note(
                        "Grafx recovery close also failed: "
                        f"{type(close_failure).__name__}: {close_failure}"
                    )

        if primary is not None:
            error = _failure_text(primary, operation="recover_wal_only")
            if snapshot is not None:
                try:
                    _publish_snapshot(
                        snapshot,
                        recovery_report=recovery_report,
                        phase="failed_after_grafx_open",
                        error=error,
                    )
                except BaseException as publish_failure:  # noqa: BLE001
                    primary.add_note(
                        "Pulse WAL snapshot publication also failed: "
                        f"{type(publish_failure).__name__}: {publish_failure}"
                    )
                    return self._failed(
                        board_id,
                        primary,
                        operation="recover_wal_only",
                        files_moved=_snapshot_file_names(snapshot),
                    )
                return self._failed(
                    board_id,
                    primary,
                    operation="recover_wal_only",
                    quarantine_id=snapshot.quarantine_id,
                    files_moved=_snapshot_file_names(snapshot),
                )
            return self._failed(board_id, primary, operation="recover_wal_only")

        if recovery_report is None:
            return self._failed_with_snapshot(
                board_id,
                snapshot,
                RuntimeError("Grafx writable open returned no recovery report"),
                operation="recover_wal_only_report",
            )
        if getattr(verification, "clean", None) is not True:
            findings = getattr(verification, "findings", ())
            return self._failed_with_snapshot(
                board_id,
                snapshot,
                RuntimeError(
                    "Grafx recovery completed but verification was not clean "
                    f"(findings={len(findings) if isinstance(findings, tuple) else 'unknown'})"
                ),
                operation="recover_wal_only_verify",
                recovery_report=recovery_report,
            )

        try:
            self._revalidate_fence(board_id, "wal_recovery_reopen")
            _probe(self._open_database, path)
        except BaseException as failure:  # noqa: BLE001
            return self._failed_with_snapshot(
                board_id,
                snapshot,
                failure,
                operation="recover_wal_only_cold_reopen",
                recovery_report=recovery_report,
            )

        outcome = getattr(recovery_report, "outcome", None)
        discarded = getattr(recovery_report, "records_discarded", 0)
        if outcome == "refused":
            return self._failed_with_snapshot(
                board_id,
                snapshot,
                RuntimeError("Grafx recovery policy refused the WAL state"),
                operation="recover_wal_only_policy",
                recovery_report=recovery_report,
            )
        changed_wal = outcome in {"truncated", "quarantined"} or bool(discarded)
        if changed_wal:
            if snapshot is None or not snapshot.files:
                return self._failed(
                    board_id,
                    RuntimeError(
                        "Grafx changed WAL state without a complete restorable Pulse snapshot"
                    ),
                    operation="recover_wal_only_snapshot",
                )
            try:
                _publish_snapshot(
                    snapshot,
                    recovery_report=recovery_report,
                    phase="recovered",
                    error=None,
                )
            except BaseException as failure:  # noqa: BLE001
                return self._failed(
                    board_id,
                    failure,
                    operation="publish_wal_recovery_snapshot",
                    files_moved=_snapshot_file_names(snapshot),
                )
            return WalRecoveryReport(
                board_id=board_id,
                status="recovered",
                quarantine_id=snapshot.quarantine_id,
                files_moved=_snapshot_file_names(snapshot),
                main_untouched=True,
            )

        _discard_snapshot(snapshot)
        return WalRecoveryReport(
            board_id=board_id,
            status="skipped",
            main_untouched=True,
            reason="Grafx recovery found no WAL work",
        )

    def _failed_with_snapshot(
        self,
        board_id: str,
        snapshot: _WalSnapshot | None,
        failure: BaseException,
        *,
        operation: str,
        recovery_report: Any | None = None,
    ) -> WalRecoveryReport:
        if snapshot is None:
            return self._failed(board_id, failure, operation=operation)
        error = _failure_text(failure, operation=operation)
        try:
            _publish_snapshot(
                snapshot,
                recovery_report=recovery_report,
                phase="failed_after_grafx_open",
                error=error,
            )
        except BaseException as publish_failure:  # noqa: BLE001
            failure.add_note(
                "Pulse WAL snapshot publication also failed: "
                f"{type(publish_failure).__name__}: {publish_failure}"
            )
            return self._failed(
                board_id,
                failure,
                operation=operation,
                files_moved=_snapshot_file_names(snapshot),
            )
        return self._failed(
            board_id,
            failure,
            operation=operation,
            quarantine_id=snapshot.quarantine_id,
            files_moved=_snapshot_file_names(snapshot),
        )

    @staticmethod
    def _failed(
        board_id: str,
        failure: BaseException,
        *,
        operation: str,
        quarantine_id: str | None = None,
        files_moved: tuple[str, ...] = (),
    ) -> WalRecoveryReport:
        return WalRecoveryReport(
            board_id=board_id,
            status="failed",
            quarantine_id=quarantine_id,
            files_moved=files_moved,
            main_untouched=True,
            reason=_failure_text(failure, operation=operation),
        )


__all__ = ["CommunityGrafxGraphRecovery"]
