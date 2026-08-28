"""Operator-driven restore for Pulse Grafx WAL snapshots.

The adapter consumes the whole-segment, versioned snapshots emitted by
``CommunityGrafxGraphRecovery``. A dry-run derives every destination from the
currently bound generation and validates sizes and digests without mutation.
Apply holds an injected maintenance guard, journals every rename write-ahead,
backs up the complete live WAL namespace, installs each restored segment by a
durable same-directory temp+replace, retains the source snapshot and proves a
cold Grafx open before reporting success.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from okto_pulse.core.kg.interfaces.quarantine_restore import (
    QuarantineRestoreError,
    QuarantineRestoreErrorCode,
    RestoreFileEntry,
    RestorePlan,
    RestoreReport,
)

from okto_pulse.community.adapters.filesystem_erasure import fsync_directory
from okto_pulse.community.adapters.grafx_graph_recovery import (
    _MANIFEST_FILE,
    _MANIFEST_FORMAT,
    _PAYLOAD_DIRECTORY,
    _WAL_SEGMENT,
    _copy_file_durable,
    _database_path,
    _is_link,
    _probe,
    _quarantine_path,
    _require_disjoint,
    _sha256_file,
    _write_json_atomic,
)

_QUARANTINE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_MAX_MANIFEST_BYTES = 1024 * 1024
_OPERATION_FILE = "restore_operation.json"

logger = logging.getLogger("okto_pulse.kg.quarantine.restore")

PathResolver = Callable[[str], str | os.PathLike[str]]
DatabaseOpener = Callable[[Path], Any]
BoardCloser = Callable[[str], None]
BoardLockProbe = Callable[[str], bool]
FenceRevalidator = Callable[[str, str], None]
MutationGuard = Callable[[str], AbstractContextManager[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_MANIFEST_BYTES:
            raise ValueError(f"manifest size {size} is outside the accepted bound")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as failure:
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
            reason=f"Grafx quarantine manifest is unreadable: {type(failure).__name__}",
            details={"manifest_path": str(path)},
        ) from failure
    if type(payload) is not dict:
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
            reason="Grafx quarantine manifest is not an object",
            details={"manifest_path": str(path)},
        )
    return payload


def _required_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
            reason=f"Grafx quarantine manifest has invalid {field}",
        )
    return value


def _required_nonnegative_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
            reason=f"Grafx quarantine manifest has invalid {field}",
        )
    return value


def _contained_path(root: Path, relative_text: str) -> Path:
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
            reason=f"unsafe Grafx quarantine relative path: {relative_text!r}",
        )
    destination = (root / relative).resolve(strict=False)
    try:
        destination.relative_to(root.resolve(strict=False))
    except ValueError as failure:
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
            reason=f"Grafx quarantine path escapes its root: {relative_text!r}",
        ) from failure
    return destination


def _reject_symlink_components(root: Path, relative_text: str) -> None:
    """Reject a symlink/junction at every existing lexical path component."""

    current = root
    for component in Path(relative_text).parts:
        current = current / component
        if _is_link(current):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason=(
                    "Grafx quarantine path crosses a symlink/reparse point: "
                    f"{relative_text!r}"
                ),
            )


def _atomic_move(source: Path, destination: Path) -> None:
    """Rename one live WAL into its unique same-filesystem backup."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or _is_link(destination):
        raise FileExistsError(destination)
    os.replace(source, destination)
    fsync_directory(source.parent)
    fsync_directory(destination.parent)


class CommunityGrafxQuarantineRestore:
    """Grafx filesystem adapter for the Core ``QuarantineRestore`` port."""

    def __init__(
        self,
        *,
        quarantine_root: str | os.PathLike[str],
        database_path_resolver: PathResolver,
        open_database: DatabaseOpener,
        close_board: BoardCloser,
        board_is_locked: BoardLockProbe,
        revalidate_fence: FenceRevalidator,
        mutation_guard: MutationGuard,
    ) -> None:
        self._quarantine_root = _quarantine_path(quarantine_root)
        self._database_path_resolver = database_path_resolver
        self._open_database = open_database
        self._close_board = close_board
        self._board_is_locked = board_is_locked
        self._revalidate_fence = revalidate_fence
        self._mutation_guard = mutation_guard

    def plan(self, quarantine_id: str) -> RestorePlan:
        quarantine_dir = self._quarantine_dir(quarantine_id)
        manifest = _read_manifest(quarantine_dir / _MANIFEST_FILE)
        if manifest.get("format") != _MANIFEST_FORMAT or manifest.get("kind") not in {
            "grafx_wal_only",
            "grafx_restore_backup",
        }:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="unsupported Grafx quarantine manifest format or kind",
                details={"quarantine_id": quarantine_id},
            )
        if manifest.get("complete") is not True:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx quarantine snapshot is incomplete",
                details={"quarantine_id": quarantine_id},
            )
        if manifest.get("quarantine_id") != quarantine_id:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx quarantine identity does not match its directory",
            )
        if manifest.get("main_untouched") is not True:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx WAL quarantine does not prove main_untouched",
            )

        board_id = _required_text(manifest, "board_id")
        board_dir = _database_path(self._database_path_resolver(board_id))
        _require_disjoint(board_dir, self._quarantine_root)
        recorded_path = _database_path(_required_text(manifest, "database_path"))
        if recorded_path != board_dir:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx quarantine belongs to a different bound generation",
                details={
                    "recorded_database_path": str(recorded_path),
                    "bound_database_path": str(board_dir),
                },
            )

        raw_files = manifest.get("files")
        if type(raw_files) is not list or not raw_files:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx quarantine contains no restorable files",
            )

        entries: list[RestoreFileEntry] = []
        conflicts: list[str] = []
        total_bytes = 0
        seen: set[str] = set()
        for raw in raw_files:
            if type(raw) is not dict:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason="Grafx quarantine file entry is not an object",
                )
            relative = _required_text(raw, "relative_path")
            if relative in seen:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=f"Grafx quarantine repeats {relative!r}",
                )
            seen.add(relative)
            normalized_parts = Path(relative).parts
            if (
                len(normalized_parts) != 2
                or normalized_parts[0] != "wal"
                or _WAL_SEGMENT.fullmatch(normalized_parts[1]) is None
            ):
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=(
                        "Grafx wal-only quarantine names a non-canonical WAL "
                        f"segment {relative!r}"
                    ),
                )
            _reject_symlink_components(
                quarantine_dir / _PAYLOAD_DIRECTORY,
                relative,
            )
            _reject_symlink_components(board_dir, relative)
            source = _contained_path(
                quarantine_dir / _PAYLOAD_DIRECTORY,
                relative,
            )
            destination = _contained_path(board_dir, relative)
            size = _required_nonnegative_int(raw, "size_bytes")
            digest = _required_text(raw, "sha256")
            try:
                if _is_link(source):
                    raise OSError("payload is a symlink/reparse point")
                source_size = source.stat().st_size
                source_digest = _sha256_file(source)
            except OSError as failure:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=f"Grafx quarantine payload {relative!r} is unreadable",
                ) from failure
            if source_size != size or source_digest != digest:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=f"Grafx quarantine payload {relative!r} failed integrity",
                )
            if _is_link(destination):
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=(
                        "Grafx live WAL destination is a symlink/reparse point: "
                        f"{relative!r}"
                    ),
                )
            if destination.exists() and not destination.is_file():
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=f"Grafx live WAL destination is not a file: {relative!r}",
                )
            conflict = destination.is_file()
            live_size = destination.stat().st_size if conflict else None
            if conflict:
                conflicts.append(relative)
            total_bytes += size
            entries.append(
                RestoreFileEntry(
                    name=relative,
                    source_path=str(source),
                    destination_path=str(destination),
                    size_bytes=size,
                    conflict=conflict,
                    live_size_bytes=live_size,
                )
            )

        plan = RestorePlan(
            quarantine_id=quarantine_id,
            board_id=board_id,
            board_dir=str(board_dir),
            manifest_format=_MANIFEST_FORMAT,
            files=tuple(entries),
            conflicts=tuple(conflicts),
            total_bytes=total_bytes,
        )
        logger.info(
            "kg.quarantine.restore_dry_run quarantine_id=%s board=%s "
            "files=%d conflicts=%d total_bytes=%d",
            plan.quarantine_id,
            plan.board_id,
            len(plan.files),
            len(plan.conflicts),
            plan.total_bytes,
            extra={
                "event": "kg.quarantine.restore_dry_run",
                "quarantine_id": plan.quarantine_id,
                "board_id": plan.board_id,
                "board_dir": plan.board_dir,
                "manifest_format": plan.manifest_format,
                "files": len(plan.files),
                "conflicts": list(plan.conflicts),
                "total_bytes": plan.total_bytes,
            },
        )
        return plan

    def apply(self, quarantine_id: str) -> RestoreReport:
        preliminary = self.plan(quarantine_id)
        try:
            with self._mutation_guard(preliminary.board_id):
                # Re-read every input while the startup/maintenance fence is
                # held. A dry-run result is never authority for a later apply.
                plan = self.plan(quarantine_id)
                if plan.board_id != preliminary.board_id:
                    raise QuarantineRestoreError(
                        QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                        reason="Grafx quarantine binding changed before apply",
                    )
                self._require_same_filesystem(plan)
                expected_digests = self._manifest_digests(quarantine_id)
                return self._apply_guarded(plan, expected_digests)
        except QuarantineRestoreError:
            raise
        except BaseException as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason=(
                    "Grafx maintenance guard could not be held for quarantine "
                    f"restore: {type(failure).__name__}"
                ),
                details={"board_id": preliminary.board_id},
            ) from failure

    def _apply_guarded(
        self,
        plan: RestorePlan,
        expected_digests: dict[str, str],
    ) -> RestoreReport:
        self._require_unlocked(plan.board_id)
        try:
            self._close_board(plan.board_id)
        except BaseException as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason=f"Grafx board could not be closed: {type(failure).__name__}",
                details={"board_id": plan.board_id},
            ) from failure
        self._require_unlocked(plan.board_id)
        self._revalidate_fence(plan.board_id, "quarantine_restore_begin")
        self._validate_snapshot_inputs(plan, expected_digests)

        backup_id = f"grafx-restore-backup-{secrets.token_hex(16)}"
        backup_dir = self._quarantine_root / backup_id
        operation_path = backup_dir / _OPERATION_FILE
        board_dir = Path(plan.board_dir)
        state: dict[str, object] = {
            "format": _MANIFEST_FORMAT,
            "operation": "grafx_quarantine_restore",
            "source_quarantine_id": plan.quarantine_id,
            "source_quarantine_dir": str(self._quarantine_dir(plan.quarantine_id)),
            "backup_quarantine_id": backup_id,
            "board_id": plan.board_id,
            "database_path": plan.board_dir,
            "started_at": _utc_now().isoformat(),
            "phase": "create_backup",
            "backup_pending": None,
            "copy_pending": None,
            "moved_to_backup": [],
            "copied_from_snapshot": [],
            "open_validated": False,
            "error": None,
        }
        try:
            backup_dir.mkdir(parents=True, exist_ok=False)
            fsync_directory(backup_dir)
            fsync_directory(self._quarantine_root)
            backup_payload = backup_dir / _PAYLOAD_DIRECTORY
            backup_wal = backup_payload / "wal"
            backup_wal.mkdir(parents=True, exist_ok=False)
            fsync_directory(backup_wal)
            fsync_directory(backup_payload)
            fsync_directory(backup_dir)
            _write_json_atomic(operation_path, state)
        except BaseException as failure:  # noqa: BLE001
            self._raise_partial(state, operation_path, failure, step="create_backup")

        backup_files: list[dict[str, object]] = []
        moved: list[str] = []
        for live in self._live_wal_segments(board_dir):
            relative = live.relative_to(board_dir).as_posix()
            backup_target = backup_dir / _PAYLOAD_DIRECTORY / Path(relative)
            try:
                self._revalidate_fence(plan.board_id, "quarantine_restore_backup")
                _reject_symlink_components(board_dir, relative)
                if _is_link(live) or not live.is_file():
                    raise OSError(f"live WAL is not a regular file: {relative!r}")
                live_size = live.stat().st_size
                live_digest = _sha256_file(live)
                pending = {
                    "relative_path": relative,
                    "source_path": str(live),
                    "backup_path": str(backup_target),
                    "size_bytes": live_size,
                    "sha256": live_digest,
                }
                state["phase"] = "backup_pending"
                state["backup_pending"] = pending
                _write_json_atomic(operation_path, state)
                _atomic_move(live, backup_target)
                if (
                    backup_target.stat().st_size != live_size
                    or _sha256_file(backup_target) != live_digest
                ):
                    raise OSError(f"backup WAL failed integrity: {relative!r}")
                backup_files.append(
                    {
                        "name": live.name,
                        "relative_path": relative,
                        "size_bytes": live_size,
                        "sha256": live_digest,
                    }
                )
                moved.append(relative)
                state["moved_to_backup"] = list(moved)
                state["backup_pending"] = None
                state["phase"] = "backup_done"
                _write_json_atomic(operation_path, state)
            except BaseException as failure:  # noqa: BLE001
                self._raise_partial(
                    state,
                    operation_path,
                    failure,
                    step=f"backup_swap:{relative}",
                )

        backup_manifest: dict[str, object] = {
            "format": _MANIFEST_FORMAT,
            "kind": "grafx_restore_backup",
            "quarantine_id": backup_id,
            "board_id": plan.board_id,
            "database_path": plan.board_dir,
            "created_at": _utc_now().isoformat(),
            "main_untouched": True,
            "complete": True,
            "phase": "backup_complete",
            "source_quarantine_id": plan.quarantine_id,
            "files": backup_files,
            "files_moved": list(moved),
            "error": None,
        }
        try:
            _write_json_atomic(backup_dir / _MANIFEST_FILE, backup_manifest)
            state["phase"] = "copy_snapshot"
            _write_json_atomic(operation_path, state)
        except BaseException as failure:  # noqa: BLE001
            self._raise_partial(
                state, operation_path, failure, step="write_backup_manifest"
            )

        copied: list[str] = []
        live_wal = board_dir / "wal"
        try:
            if _is_link(live_wal):
                raise OSError("Grafx live WAL root is a symlink/reparse point")
            live_wal.mkdir(parents=True, exist_ok=True)
            fsync_directory(live_wal)
            fsync_directory(board_dir)
        except BaseException as failure:  # noqa: BLE001
            self._raise_partial(
                state,
                operation_path,
                failure,
                step="prepare_live_wal_namespace",
            )
        for entry in plan.files:
            relative = entry.name
            source = Path(entry.source_path)
            destination = board_dir / Path(relative)
            temporary = destination.with_name(
                f".{destination.name}.{secrets.token_hex(8)}.restore.tmp"
            )
            expected_digest = expected_digests[relative]
            pending = {
                "relative_path": relative,
                "source_path": str(source),
                "temporary_path": str(temporary),
                "destination_path": str(destination),
                "size_bytes": entry.size_bytes,
                "sha256": expected_digest,
            }
            try:
                self._revalidate_fence(plan.board_id, "quarantine_restore_copy")
                _reject_symlink_components(board_dir, relative)
                current_destination = _contained_path(board_dir, relative)
                if current_destination != destination.resolve(strict=False):
                    raise OSError(f"Grafx WAL destination changed: {relative!r}")
                if destination.exists() or _is_link(destination):
                    raise FileExistsError(
                        f"Grafx WAL destination appeared after backup: {relative!r}"
                    )
                state["phase"] = "copy_pending"
                state["copy_pending"] = pending
                _write_json_atomic(operation_path, state)
                copied_size, copied_digest = _copy_file_durable(source, temporary)
                if (
                    copied_size != entry.size_bytes
                    or copied_digest != expected_digest
                    or temporary.stat().st_size != entry.size_bytes
                    or _sha256_file(temporary) != expected_digest
                ):
                    raise OSError(
                        f"restored Grafx WAL temp failed manifest integrity: {relative!r}"
                    )
                if destination.exists() or _is_link(destination):
                    raise FileExistsError(
                        f"Grafx WAL destination appeared before publish: {relative!r}"
                    )
                os.replace(temporary, destination)
                fsync_directory(destination.parent)
                if (
                    destination.stat().st_size != entry.size_bytes
                    or _sha256_file(destination) != expected_digest
                ):
                    raise OSError(
                        f"published Grafx WAL failed manifest integrity: {relative!r}"
                    )
                copied.append(relative)
                state["copied_from_snapshot"] = list(copied)
                state["copy_pending"] = None
                state["phase"] = "copy_done"
                _write_json_atomic(operation_path, state)
            except BaseException as failure:  # noqa: BLE001
                self._raise_partial(
                    state,
                    operation_path,
                    failure,
                    step=f"copy_snapshot:{relative}",
                )

        try:
            state["phase"] = "validate_open"
            _write_json_atomic(operation_path, state)
            self._revalidate_fence(plan.board_id, "quarantine_restore_reopen")
            _probe(self._open_database, board_dir)
            self._revalidate_fence(plan.board_id, "quarantine_restore_complete")
            state["phase"] = "done"
            state["open_validated"] = True
            state["finished_at"] = _utc_now().isoformat()
            _write_json_atomic(operation_path, state)
        except BaseException as failure:  # noqa: BLE001
            self._raise_partial(state, operation_path, failure, step="validate_open")

        report = RestoreReport(
            quarantine_id=plan.quarantine_id,
            board_id=plan.board_id,
            applied=True,
            backup_quarantine_id=backup_id,
            restored_files=tuple(copied),
            open_validated=True,
        )
        logger.info(
            "kg.quarantine.restored quarantine_id=%s board=%s "
            "backup_quarantine_id=%s files=%d open_validated=%s",
            report.quarantine_id,
            report.board_id,
            report.backup_quarantine_id,
            len(report.restored_files),
            report.open_validated,
            extra={
                "event": "kg.quarantine.restored",
                "quarantine_id": report.quarantine_id,
                "board_id": report.board_id,
                "backup_quarantine_id": report.backup_quarantine_id,
                "restored_files": list(report.restored_files),
                "open_validated": report.open_validated,
                "operation_manifest": str(operation_path),
            },
        )
        return report

    def _manifest_digests(self, quarantine_id: str) -> dict[str, str]:
        manifest = _read_manifest(self._quarantine_dir(quarantine_id) / _MANIFEST_FILE)
        raw_files = manifest.get("files")
        if type(raw_files) is not list:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx quarantine has no digest inventory",
            )
        digests: dict[str, str] = {}
        for raw in raw_files:
            if type(raw) is not dict:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason="Grafx quarantine digest entry is not an object",
                )
            relative = _required_text(raw, "relative_path")
            digest = _required_text(raw, "sha256")
            if relative in digests:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=f"Grafx quarantine repeats {relative!r}",
                )
            digests[relative] = digest
        return digests

    def _validate_snapshot_inputs(
        self,
        plan: RestorePlan,
        expected_digests: dict[str, str],
    ) -> None:
        if set(expected_digests) != {entry.name for entry in plan.files}:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx quarantine digest inventory changed before apply",
            )
        for entry in plan.files:
            source = Path(entry.source_path)
            expected = expected_digests[entry.name]
            if (
                _is_link(source)
                or not source.is_file()
                or source.stat().st_size != entry.size_bytes
                or _sha256_file(source) != expected
            ):
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=(
                        f"Grafx quarantine payload changed before apply: {entry.name!r}"
                    ),
                )

    def _require_same_filesystem(self, plan: RestorePlan) -> None:
        try:
            board_device = os.stat(plan.board_dir).st_dev
            quarantine_device = os.stat(self._quarantine_root).st_dev
        except OSError as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason="Grafx restore could not prove an atomic backup filesystem",
                details={"board_id": plan.board_id},
            ) from failure
        if board_device != quarantine_device:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason=(
                    "Grafx restore requires quarantine and board on the same "
                    "filesystem for atomic backup renames"
                ),
                details={"board_id": plan.board_id},
            )

    @staticmethod
    def _live_wal_segments(board_dir: Path) -> tuple[Path, ...]:
        wal_root = board_dir / "wal"
        if not wal_root.exists():
            return ()
        if _is_link(wal_root) or not wal_root.is_dir():
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason="Grafx live WAL root is not a real directory",
            )
        files: list[Path] = []
        for candidate in sorted(wal_root.iterdir(), key=lambda path: path.name):
            if (
                _is_link(candidate)
                or not candidate.is_file()
                or _WAL_SEGMENT.fullmatch(candidate.name) is None
            ):
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.BOARD_LOCKED,
                    reason=(
                        "Grafx live WAL namespace contains an unsafe entry: "
                        f"{candidate.name!r}"
                    ),
                )
            files.append(candidate)
        return tuple(files)

    def _quarantine_dir(self, quarantine_id: str) -> Path:
        if (
            type(quarantine_id) is not str
            or _QUARANTINE_ID.fullmatch(quarantine_id) is None
        ):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason=f"invalid Grafx quarantine_id: {quarantine_id!r}",
            )
        path = (self._quarantine_root / quarantine_id).resolve(strict=False)
        try:
            path.relative_to(self._quarantine_root)
        except ValueError as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx quarantine path escapes its root",
            ) from failure
        if not path.is_dir() or _is_link(path):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason=f"Grafx quarantine {quarantine_id!r} not found",
            )
        return path

    def _require_unlocked(self, board_id: str) -> None:
        try:
            locked = self._board_is_locked(board_id)
        except BaseException as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason=f"Grafx board lock could not be proven free: {type(failure).__name__}",
                details={"board_id": board_id},
            ) from failure
        if locked is not False:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason="Grafx board is live; stop it before restoring quarantine",
                details={"board_id": board_id},
            )

    @staticmethod
    def _reconcile_pending(state: dict[str, object]) -> None:
        """Record physical truth when failure lands between rename and journal."""

        reconciliation: dict[str, object] = {}

        def matches(path: Path, size: int, digest: str) -> bool:
            try:
                return (
                    not _is_link(path)
                    and path.is_file()
                    and path.stat().st_size == size
                    and _sha256_file(path) == digest
                )
            except OSError:
                return False

        backup_pending = state.get("backup_pending")
        if type(backup_pending) is dict:
            relative = str(backup_pending.get("relative_path", ""))
            source = Path(str(backup_pending.get("source_path", "")))
            backup = Path(str(backup_pending.get("backup_path", "")))
            size = int(backup_pending.get("size_bytes", -1))
            digest = str(backup_pending.get("sha256", ""))
            source_ok = matches(source, size, digest)
            backup_ok = matches(backup, size, digest)
            if backup_ok and not source.exists():
                moved = list(state.get("moved_to_backup", []))
                if relative not in moved:
                    moved.append(relative)
                state["moved_to_backup"] = moved
                reconciliation["backup_pending"] = "rename_completed"
            elif source_ok and not backup.exists():
                reconciliation["backup_pending"] = "rename_not_started"
            else:
                reconciliation["backup_pending"] = {
                    "state": "ambiguous",
                    "source_exists": source.exists(),
                    "source_matches": source_ok,
                    "backup_exists": backup.exists(),
                    "backup_matches": backup_ok,
                }

        copy_pending = state.get("copy_pending")
        if type(copy_pending) is dict:
            relative = str(copy_pending.get("relative_path", ""))
            temporary = Path(str(copy_pending.get("temporary_path", "")))
            destination = Path(str(copy_pending.get("destination_path", "")))
            size = int(copy_pending.get("size_bytes", -1))
            digest = str(copy_pending.get("sha256", ""))
            temporary_ok = matches(temporary, size, digest)
            destination_ok = matches(destination, size, digest)
            if destination_ok and not temporary.exists():
                copied = list(state.get("copied_from_snapshot", []))
                if relative not in copied:
                    copied.append(relative)
                state["copied_from_snapshot"] = copied
                reconciliation["copy_pending"] = "publish_completed"
            elif temporary_ok and not destination.exists():
                reconciliation["copy_pending"] = "durable_temp_not_published"
            elif not temporary.exists() and not destination.exists():
                reconciliation["copy_pending"] = "copy_not_published"
            else:
                reconciliation["copy_pending"] = {
                    "state": "ambiguous",
                    "temporary_exists": temporary.exists(),
                    "temporary_matches": temporary_ok,
                    "destination_exists": destination.exists(),
                    "destination_matches": destination_ok,
                }

        state["reconciliation"] = reconciliation

    @staticmethod
    def _raise_partial(
        state: dict[str, object],
        operation_path: Path,
        failure: BaseException,
        *,
        step: str,
    ) -> NoReturn:
        CommunityGrafxQuarantineRestore._reconcile_pending(state)
        state["phase"] = "failed"
        state["error"] = {
            "step": step,
            "type": type(failure).__name__,
            "message": str(failure),
        }
        state["finished_at"] = _utc_now().isoformat()
        state["rollback_instruction"] = (
            "Keep the board stopped and the maintenance guard held; inspect reconciliation; "
            "remove only files listed in copied_from_snapshot, then atomically restore every "
            "file listed in moved_to_backup from backup_quarantine_id. Inspect any pending "
            "temp/ambiguous path before retrying; the source quarantine remains immutable."
        )
        manifest_failure: BaseException | None = None
        try:
            _write_json_atomic(operation_path, state)
        except BaseException as secondary:  # noqa: BLE001
            manifest_failure = secondary
        details: dict[str, object] = {
            "operation_manifest": str(operation_path),
            "step": step,
            "backup_quarantine_id": state.get("backup_quarantine_id"),
            "moved_to_backup": list(state.get("moved_to_backup", [])),
            "copied_from_snapshot": list(state.get("copied_from_snapshot", [])),
            "backup_pending": state.get("backup_pending"),
            "copy_pending": state.get("copy_pending"),
            "reconciliation": state.get("reconciliation", {}),
            "rollback_instruction": state["rollback_instruction"],
        }
        if manifest_failure is not None:
            details["manifest_error"] = (
                f"{type(manifest_failure).__name__}: {manifest_failure}"
            )
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.PARTIAL_RESTORE,
            reason=f"Grafx quarantine restore failed at {step}: {type(failure).__name__}",
            details=details,
        ) from failure


__all__ = ["CommunityGrafxQuarantineRestore"]
