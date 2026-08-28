"""Operator-driven restore for Pulse Grafx quarantine snapshots.

The adapter preserves the existing whole-segment WAL restore and also restores
authenticated complete-generation snapshots. A dry-run derives every
destination from the current binding and validates recursive sizes and digests
without mutation. Apply holds an injected maintenance guard, retains the source
snapshot, journals publication, creates a complete live backup, and proves a
cold Grafx open before reporting success.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
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

from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory,
    is_filesystem_alias,
    reject_filesystem_alias_ancestry,
    remove_contained_tree,
)
from okto_pulse.community.adapters.grafx_board_storage import (
    GRAFX_DIRECTORY_PAYLOAD,
    GRAFX_DIRECTORY_QUARANTINE_FORMAT,
    GRAFX_DIRECTORY_QUARANTINE_KIND,
    GrafxDirectoryInventory,
    _authenticated_manifest,
    _canonical_sha256,
    _capture_grafx_board_storage,
    _copy_plain_directory,
    _read_directory_manifest,
    _write_directory_json_atomic,
    grafx_directory_inventory,
)
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
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)

_QUARANTINE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_MAX_MANIFEST_BYTES = 1024 * 1024
_OPERATION_FILE = "restore_operation.json"
_DIRECTORY_OPERATION_ROOT = ".grafx_directory_restore_operations"
_DIRECTORY_OPERATION_FORMAT = "pulse_grafx_directory_restore/1"

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
        reject_filesystem_alias_ancestry(path.parent)
        metadata = path.lstat()
        if is_filesystem_alias(path) or not stat.S_ISREG(metadata.st_mode):
            raise OSError("manifest is not a plain file")
        size = metadata.st_size
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
        if (
            manifest.get("format") == GRAFX_DIRECTORY_QUARANTINE_FORMAT
            and manifest.get("kind") == GRAFX_DIRECTORY_QUARANTINE_KIND
        ) or "manifest_sha256" in manifest:
            return self._plan_directory(quarantine_id, quarantine_dir)
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

    def _plan_directory(
        self,
        quarantine_id: str,
        quarantine_dir: Path,
    ) -> RestorePlan:
        try:
            manifest = _read_directory_manifest(quarantine_dir / _MANIFEST_FILE)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason=(
                    "Grafx directory quarantine manifest failed authentication: "
                    f"{type(failure).__name__}"
                ),
                details={"quarantine_id": quarantine_id},
            ) from failure
        if (
            manifest.get("format") != GRAFX_DIRECTORY_QUARANTINE_FORMAT
            or manifest.get("kind") != GRAFX_DIRECTORY_QUARANTINE_KIND
            or manifest.get("complete") is not True
            or manifest.get("phase") != "captured"
            or manifest.get("quarantine_id") != quarantine_id
        ):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine is incomplete or has wrong identity",
            )
        board_id = _required_text(manifest, "board_id")
        board_dir = _database_path(self._database_path_resolver(board_id))
        _require_disjoint(board_dir, self._quarantine_root)
        try:
            expected_root = board_dir.parents[3] / "quarantine"
        except IndexError as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx bound generation path is not canonical",
            ) from failure
        if expected_root != self._quarantine_root:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine is outside the bound KG root",
            )
        recorded_path = _database_path(_required_text(manifest, "database_path"))
        if recorded_path != board_dir:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine belongs to another generation",
                details={
                    "recorded_database_path": str(recorded_path),
                    "bound_database_path": str(board_dir),
                },
            )
        try:
            binding = CommunityGraphBackendBindingStore(
                self._quarantine_root.parent
            ).inspect_board_binding(board_id)
        except BaseException as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine binding is unavailable",
            ) from failure
        if (
            binding.backend != "grafx"
            or binding.physical_path != board_dir
            or manifest.get("generation") != binding.generation
            or manifest.get("binding_sha256") != binding.binding_sha256
        ):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine binding evidence is stale",
            )

        raw_directories = manifest.get("directories")
        raw_files = manifest.get("files")
        if type(raw_directories) is not list or type(raw_files) is not list:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine has no recursive inventory",
            )
        directories: list[str] = []
        for raw in raw_directories:
            if type(raw) is not str:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason="Grafx directory quarantine has an invalid directory entry",
                )
            relative = Path(raw)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or relative.as_posix() != raw
            ):
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=f"unsafe Grafx directory inventory path: {raw!r}",
                )
            directories.append(raw)
        if directories != sorted(set(directories)):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory inventory directories are not canonical",
            )

        files: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in raw_files:
            if type(raw) is not dict or set(raw) != {
                "relative_path",
                "size_bytes",
                "sha256",
            }:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason="Grafx directory quarantine has an invalid file entry",
                )
            relative = _required_text(raw, "relative_path")
            normalized = Path(relative)
            digest = _required_text(raw, "sha256")
            size = _required_nonnegative_int(raw, "size_bytes")
            if (
                normalized.is_absolute()
                or not normalized.parts
                or ".." in normalized.parts
                or normalized.as_posix() != relative
                or relative in seen
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=f"unsafe Grafx directory file inventory: {relative!r}",
                )
            seen.add(relative)
            files.append(
                {
                    "relative_path": relative,
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        if files != sorted(files, key=lambda item: str(item["relative_path"])):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory inventory files are not canonical",
            )
        inventory = GrafxDirectoryInventory(
            directories=tuple(directories),
            files=tuple(files),
            sha256=_required_text(manifest, "inventory_sha256"),
        )
        expected_inventory_sha = _canonical_sha256(
            {
                "directories": list(inventory.directories),
                "files": [dict(item) for item in inventory.files],
            }
        )
        if inventory.sha256 != expected_inventory_sha:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine inventory hash mismatch",
            )
        payload_relative = _required_text(manifest, "payload_relative")
        if payload_relative != GRAFX_DIRECTORY_PAYLOAD:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine payload root is not canonical",
            )
        _reject_symlink_components(quarantine_dir, payload_relative)
        payload_root = _contained_path(quarantine_dir, payload_relative)
        try:
            observed = grafx_directory_inventory(payload_root)
        except OSError as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine payload is unsafe",
            ) from failure
        if observed != inventory:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine payload failed integrity",
            )

        entries: list[RestoreFileEntry] = []
        conflicts: list[str] = []
        total_bytes = 0
        for raw in inventory.files:
            relative = str(raw["relative_path"])
            _reject_symlink_components(payload_root, relative)
            _reject_symlink_components(board_dir, relative)
            source = _contained_path(payload_root, relative)
            destination = _contained_path(board_dir, relative)
            conflict = destination.is_file()
            if destination.exists() and not conflict:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                    reason=f"Grafx live destination is not a file: {relative!r}",
                )
            size = int(raw["size_bytes"])
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
                    live_size_bytes=(destination.stat().st_size if conflict else None),
                )
            )
        if not entries:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine contains no restorable files",
            )
        plan = RestorePlan(
            quarantine_id=quarantine_id,
            board_id=board_id,
            board_dir=str(board_dir),
            manifest_format=GRAFX_DIRECTORY_QUARANTINE_FORMAT,
            files=tuple(entries),
            conflicts=tuple(conflicts),
            total_bytes=total_bytes,
        )
        logger.info(
            "kg.quarantine.restore_dry_run quarantine_id=%s board=%s "
            "kind=%s files=%d conflicts=%d total_bytes=%d",
            plan.quarantine_id,
            plan.board_id,
            GRAFX_DIRECTORY_QUARANTINE_KIND,
            len(plan.files),
            len(plan.conflicts),
            plan.total_bytes,
            extra={
                "event": "kg.quarantine.restore_dry_run",
                "quarantine_id": plan.quarantine_id,
                "board_id": plan.board_id,
                "kind": GRAFX_DIRECTORY_QUARANTINE_KIND,
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
                if self._is_directory_quarantine(quarantine_id):
                    return self._apply_directory_guarded(plan)
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

    def _is_directory_quarantine(self, quarantine_id: str) -> bool:
        quarantine_dir = self._quarantine_dir(quarantine_id)
        raw = _read_manifest(quarantine_dir / _MANIFEST_FILE)
        if "manifest_sha256" not in raw:
            return False
        try:
            manifest = _read_directory_manifest(quarantine_dir / _MANIFEST_FILE)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine manifest is not authenticated",
            ) from failure
        return (
            manifest.get("format") == GRAFX_DIRECTORY_QUARANTINE_FORMAT
            and manifest.get("kind") == GRAFX_DIRECTORY_QUARANTINE_KIND
        )

    @staticmethod
    def _rebuild_owner(board_id: str, owner_token: str) -> bool:
        from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

        return bool(KGSingleWriterLock().is_owner(board_id, owner_token))

    def apply_rebuild_compensation(
        self,
        quarantine_id: str,
        *,
        expected_board_id: str,
        run_id: str,
        owner_token: str | None,
    ) -> RestoreReport:
        """Restore a directory snapshot under the rebuild's existing fence."""

        if not expected_board_id or not run_id or not owner_token:
            raise ValueError("rebuild_compensation_restore_identity_invalid")
        plan = self.plan(quarantine_id)
        if plan.board_id != expected_board_id:
            raise ValueError("rebuild_compensation_restore_board_mismatch")
        if not self._is_directory_quarantine(quarantine_id):
            raise ValueError("rebuild_compensation_restore_kind_invalid")
        if not self._rebuild_owner(plan.board_id, owner_token):
            raise ValueError("rebuild_compensation_restore_fence_lost")
        with self._mutation_guard(plan.board_id):
            if not self._rebuild_owner(plan.board_id, owner_token):
                raise ValueError("rebuild_compensation_restore_fence_lost")
            guarded_plan = self.plan(quarantine_id)
            if guarded_plan.board_id != plan.board_id:
                raise ValueError("rebuild_compensation_restore_board_mismatch")
            self._require_same_filesystem(guarded_plan)
            report = self._apply_directory_guarded(
                guarded_plan,
                compensation_run_id=run_id,
                mutation_fence=lambda: self._rebuild_owner(
                    guarded_plan.board_id,
                    owner_token,
                ),
            )
            if not self._rebuild_owner(plan.board_id, owner_token):
                raise ValueError("rebuild_compensation_restore_fence_lost")
            return report

    def discard_rebuild_candidate(
        self,
        *,
        expected_board_id: str,
        run_id: str,
        owner_token: str | None,
    ) -> dict[str, object]:
        """Fenced, retry-safe quarantine of one failed Grafx generation."""

        if not expected_board_id or not run_id or not owner_token:
            raise ValueError("rebuild_candidate_discard_identity_invalid")
        if not self._rebuild_owner(expected_board_id, owner_token):
            raise ValueError("rebuild_candidate_discard_fence_lost")
        path = _database_path(self._database_path_resolver(expected_board_id))
        with self._mutation_guard(expected_board_id):
            if not self._rebuild_owner(expected_board_id, owner_token):
                raise ValueError("rebuild_candidate_discard_fence_lost")
            self._close_board(expected_board_id)
            self._require_unlocked(expected_board_id)
            self._revalidate_fence(
                expected_board_id,
                "rebuild_candidate_discard_begin",
            )
            affected, quarantine_id = _capture_grafx_board_storage(
                expected_board_id,
                path,
                reason=f"failed_rebuild_candidate:{run_id}",
                remove_source=True,
                compensation_run_id=run_id,
                before_source_mutation=lambda: self._require_rebuild_discard_fence(
                    expected_board_id,
                    owner_token,
                ),
            )
            try:
                path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("rebuild_candidate_discard_unverified")
            self._revalidate_fence(
                expected_board_id,
                "rebuild_candidate_discard_complete",
            )
            if not self._rebuild_owner(expected_board_id, owner_token):
                raise ValueError("rebuild_candidate_discard_fence_lost")
        return {
            "status": "discarded" if affected else "already_absent",
            "discarded_files": affected,
            "quarantine_id": quarantine_id,
            "live_absent": True,
        }

    def _require_rebuild_discard_fence(
        self,
        board_id: str,
        owner_token: str,
    ) -> None:
        if not self._rebuild_owner(board_id, owner_token):
            raise ValueError("rebuild_candidate_discard_fence_lost")
        self._revalidate_fence(board_id, "rebuild_candidate_discard_move")

    def _directory_snapshot(
        self,
        quarantine_id: str,
    ) -> tuple[Path, GrafxDirectoryInventory]:
        quarantine_dir = self._quarantine_dir(quarantine_id)
        try:
            manifest = _read_directory_manifest(quarantine_dir / _MANIFEST_FILE)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine manifest is not authenticated",
            ) from failure
        raw_directories = manifest.get("directories")
        raw_files = manifest.get("files")
        if type(raw_directories) is not list or type(raw_files) is not list:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine inventory is missing",
            )
        inventory = GrafxDirectoryInventory(
            directories=tuple(str(item) for item in raw_directories),
            files=tuple(dict(item) for item in raw_files if type(item) is dict),
            sha256=str(manifest.get("inventory_sha256") or ""),
        )
        if len(inventory.files) != len(raw_files):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine inventory changed",
            )
        payload_root = quarantine_dir.joinpath(*Path(GRAFX_DIRECTORY_PAYLOAD).parts)
        try:
            if grafx_directory_inventory(payload_root) != inventory:
                raise OSError("directory inventory mismatch")
        except OSError as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx directory quarantine payload changed before apply",
            ) from failure
        return payload_root, inventory

    def _directory_operation_path(
        self,
        plan: RestorePlan,
        compensation_run_id: str | None,
    ) -> Path:
        operation_root = Path(plan.board_dir).parent / _DIRECTORY_OPERATION_ROOT
        reject_filesystem_alias_ancestry(operation_root.parent)
        if is_filesystem_alias(operation_root):
            raise OSError("Grafx directory restore journal root is an alias")
        operation_root.mkdir(parents=True, exist_ok=True)
        fsync_directory(operation_root)
        fsync_directory(operation_root.parent)
        operation_id = _canonical_sha256(
            {
                "quarantine_id": plan.quarantine_id,
                "board_id": plan.board_id,
                "compensation_run_id": compensation_run_id,
            }
        )
        return operation_root / f"{operation_id}.json"

    @staticmethod
    def _read_optional_directory_operation(path: Path) -> dict[str, object] | None:
        try:
            return _read_directory_manifest(path)
        except FileNotFoundError:
            return None

    @staticmethod
    def _published_operation_inventory(
        state: Mapping[str, object],
    ) -> GrafxDirectoryInventory:
        raw_directories = state.get("published_directories")
        raw_files = state.get("published_files")
        if type(raw_directories) is not list or type(raw_files) is not list:
            raise OSError("Grafx directory restore journal has no published inventory")
        inventory = GrafxDirectoryInventory(
            directories=tuple(str(item) for item in raw_directories),
            files=tuple(dict(item) for item in raw_files if type(item) is dict),
            sha256=str(state.get("published_inventory_sha256") or ""),
        )
        if len(inventory.files) != len(raw_files):
            raise OSError("Grafx directory restore journal file inventory is invalid")
        expected = _canonical_sha256(
            {
                "directories": list(inventory.directories),
                "files": [dict(item) for item in inventory.files],
            }
        )
        if inventory.sha256 != expected:
            raise OSError("Grafx directory restore journal inventory hash mismatch")
        return inventory

    @staticmethod
    def _inventory_document(
        inventory: GrafxDirectoryInventory | None,
    ) -> dict[str, object] | None:
        if inventory is None:
            return None
        return {
            "directories": list(inventory.directories),
            "files": [dict(item) for item in inventory.files],
            "sha256": inventory.sha256,
        }

    @staticmethod
    def _operation_original_inventory(
        state: Mapping[str, object],
    ) -> GrafxDirectoryInventory | None:
        raw = state.get("original_inventory")
        if raw is None:
            return None
        if type(raw) is not dict or set(raw) != {"directories", "files", "sha256"}:
            raise OSError("Grafx directory restore original inventory is invalid")
        raw_directories = raw.get("directories")
        raw_files = raw.get("files")
        if type(raw_directories) is not list or type(raw_files) is not list:
            raise OSError("Grafx directory restore original inventory is invalid")
        if any(type(item) is not str for item in raw_directories) or any(
            type(item) is not dict for item in raw_files
        ):
            raise OSError("Grafx directory restore original inventory is invalid")
        inventory = GrafxDirectoryInventory(
            directories=tuple(raw_directories),
            files=tuple(dict(item) for item in raw_files),
            sha256=str(raw.get("sha256") or ""),
        )
        expected = _canonical_sha256(
            {
                "directories": list(inventory.directories),
                "files": [dict(item) for item in inventory.files],
            }
        )
        if inventory.sha256 != expected:
            raise OSError("Grafx directory restore original inventory hash mismatch")
        return inventory

    @staticmethod
    def _operation_restore_paths(
        plan: RestorePlan,
        state: Mapping[str, object],
    ) -> tuple[Path, Path]:
        board_dir = Path(plan.board_dir)
        candidate = Path(str(state.get("candidate_path") or ""))
        displaced = Path(str(state.get("displaced_path") or ""))
        candidate_pattern = re.compile(
            rf"\.{re.escape(board_dir.name)}\.[0-9a-f]{{16}}\.restore\.pending\Z"
        )
        displaced_pattern = re.compile(
            rf"\.{re.escape(board_dir.name)}\.[0-9a-f]{{16}}\.restore\.displaced\Z"
        )
        if (
            not candidate.is_absolute()
            or not displaced.is_absolute()
            or candidate.parent != board_dir.parent
            or displaced.parent != board_dir.parent
            or candidate_pattern.fullmatch(candidate.name) is None
            or displaced_pattern.fullmatch(displaced.name) is None
            or candidate == displaced
        ):
            raise OSError("Grafx directory restore journal paths are not canonical")
        reject_filesystem_alias_ancestry(board_dir.parent)
        return candidate, displaced

    @staticmethod
    def _plain_restore_directory_present(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        if is_filesystem_alias(path) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"Grafx restore artifact is unsafe: {path}")
        return True

    @staticmethod
    def _validate_directory_operation_identity(
        plan: RestorePlan,
        state: Mapping[str, object],
        inventory: GrafxDirectoryInventory,
        compensation_run_id: str | None,
    ) -> None:
        if (
            state.get("format") != _DIRECTORY_OPERATION_FORMAT
            or state.get("operation") != "grafx_directory_quarantine_restore"
            or state.get("source_quarantine_id") != plan.quarantine_id
            or state.get("board_id") != plan.board_id
            or state.get("database_path") != plan.board_dir
            or state.get("compensation_run_id") != compensation_run_id
            or state.get("inventory_sha256") != inventory.sha256
        ):
            raise OSError("Grafx directory restore journal identity mismatch")

    def _completed_directory_operation(
        self,
        plan: RestorePlan,
        operation_path: Path,
        inventory: GrafxDirectoryInventory,
        compensation_run_id: str | None,
    ) -> RestoreReport | None:
        try:
            state = self._read_optional_directory_operation(operation_path)
        except (OSError, ValueError, json.JSONDecodeError) as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.PARTIAL_RESTORE,
                reason="Grafx directory restore journal is unreadable",
                details={"operation_manifest": str(operation_path)},
            ) from failure
        if state is None or state.get("phase") != "done":
            return None
        try:
            self._validate_directory_operation_identity(
                plan,
                state,
                inventory,
                compensation_run_id,
            )
            candidate, displaced = self._operation_restore_paths(plan, state)
            if self._plain_restore_directory_present(
                candidate
            ) or self._plain_restore_directory_present(displaced):
                raise OSError("completed Grafx directory restore left an orphan")
        except OSError as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.PARTIAL_RESTORE,
                reason="Grafx directory restore journal identity mismatch",
                details={"operation_manifest": str(operation_path)},
            ) from failure
        board_dir = Path(plan.board_dir)
        try:
            self._published_operation_inventory(state)
            self._probe_directory(board_dir)
        except BaseException as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.PARTIAL_RESTORE,
                reason="completed Grafx directory restore no longer validates",
                details={"operation_manifest": str(operation_path)},
            ) from failure
        return RestoreReport(
            quarantine_id=plan.quarantine_id,
            board_id=plan.board_id,
            applied=True,
            backup_quarantine_id=(
                str(state["backup_quarantine_id"])
                if state.get("backup_quarantine_id")
                else None
            ),
            restored_files=tuple(entry.name for entry in plan.files),
            open_validated=True,
        )

    def _reconcile_interrupted_directory_operation(
        self,
        plan: RestorePlan,
        operation_path: Path,
        inventory: GrafxDirectoryInventory,
        compensation_run_id: str | None,
        *,
        fenced: Callable[[str], None],
    ) -> RestoreReport | None:
        try:
            state = self._read_optional_directory_operation(operation_path)
            if state is None or state.get("phase") == "done":
                return None
            self._validate_directory_operation_identity(
                plan,
                state,
                inventory,
                compensation_run_id,
            )
            candidate, displaced = self._operation_restore_paths(plan, state)
            original_inventory = self._operation_original_inventory(state)
            phase = str(state.get("phase") or "")
            if phase not in {
                "copy_candidate",
                "publish_pending",
                "failed",
                "reconciling",
                "reconciled",
            }:
                raise OSError(f"unsupported interrupted Grafx restore phase: {phase!r}")
            interrupted_phase = (
                str(state.get("interrupted_phase") or "")
                if phase in {"reconciling", "reconciled"}
                else phase
            )
            board_dir = Path(plan.board_dir)
            board_parent = board_dir.parent
            candidate_present = self._plain_restore_directory_present(candidate)
            displaced_present = self._plain_restore_directory_present(displaced)
            live_present = self._plain_restore_directory_present(board_dir)

            if phase == "reconciled":
                if candidate_present or displaced_present:
                    raise OSError("reconciled Grafx restore still has an orphan")
                if (original_inventory is None) == live_present:
                    raise OSError("reconciled Grafx restore live state is inconsistent")
                if live_present:
                    self._probe_directory(board_dir)
                return None

            state.update(
                {
                    "phase": "reconciling",
                    "interrupted_phase": interrupted_phase,
                    "reconciliation_started_at": _utc_now().isoformat(),
                    "reconciliation_observed": {
                        "live_present": live_present,
                        "candidate_present": candidate_present,
                        "displaced_present": displaced_present,
                    },
                }
            )
            fenced("quarantine_restore_directory_reconcile_journal")
            _write_directory_json_atomic(
                operation_path,
                _authenticated_manifest(state),
            )

            if displaced_present:
                if original_inventory is None:
                    raise OSError(
                        "interrupted Grafx restore displaced an unexpected directory"
                    )
                if live_present and candidate_present:
                    raise OSError(
                        "interrupted Grafx restore has three live directory copies"
                    )
                if live_present:
                    fenced("quarantine_restore_directory_reconcile_failed_publish")
                    os.replace(board_dir, candidate)
                    fsync_directory(board_parent)
                    candidate_present = True
                    live_present = False
                fenced("quarantine_restore_directory_reconcile_original")
                os.replace(displaced, board_dir)
                fsync_directory(board_parent)
                displaced_present = False
                live_present = True
                if grafx_directory_inventory(board_dir) != original_inventory:
                    raise OSError("interrupted Grafx restore original failed integrity")
            elif original_inventory is not None:
                if not live_present:
                    raise OSError(
                        "interrupted Grafx restore cannot locate its original directory"
                    )
                rolled_back = (
                    phase == "failed" and state.get("rollback") == "rolled_back"
                )
                live_is_original = (
                    grafx_directory_inventory(board_dir) == original_inventory
                )
                completion_possible = (
                    interrupted_phase == "publish_pending"
                    or state.get("published_before_failure") is True
                )
                if not rolled_back and not live_is_original:
                    if candidate_present or not completion_possible:
                        raise OSError(
                            "interrupted Grafx restore live directory is ambiguous"
                        )
                    self._published_operation_inventory(state)
                    fenced("quarantine_restore_directory_reconcile_cold_open")
                    self._probe_directory(board_dir)
                    fenced("quarantine_restore_directory_reconcile_complete")
                    state.update(
                        {
                            "phase": "done",
                            "open_validated": True,
                            "reconciled_as": "published",
                            "finished_at": _utc_now().isoformat(),
                        }
                    )
                    _write_directory_json_atomic(
                        operation_path,
                        _authenticated_manifest(state),
                    )
                    return RestoreReport(
                        quarantine_id=plan.quarantine_id,
                        board_id=plan.board_id,
                        applied=True,
                        backup_quarantine_id=(
                            str(state["backup_quarantine_id"])
                            if state.get("backup_quarantine_id")
                            else None
                        ),
                        restored_files=tuple(entry.name for entry in plan.files),
                        open_validated=True,
                    )
            elif live_present:
                if candidate_present or interrupted_phase != "publish_pending":
                    raise OSError(
                        "interrupted fresh Grafx restore live directory is ambiguous"
                    )
                self._published_operation_inventory(state)
                fenced("quarantine_restore_directory_reconcile_cold_open")
                self._probe_directory(board_dir)
                fenced("quarantine_restore_directory_reconcile_complete")
                state.update(
                    {
                        "phase": "done",
                        "open_validated": True,
                        "reconciled_as": "published",
                        "finished_at": _utc_now().isoformat(),
                    }
                )
                _write_directory_json_atomic(
                    operation_path,
                    _authenticated_manifest(state),
                )
                return RestoreReport(
                    quarantine_id=plan.quarantine_id,
                    board_id=plan.board_id,
                    applied=True,
                    backup_quarantine_id=None,
                    restored_files=tuple(entry.name for entry in plan.files),
                    open_validated=True,
                )

            if candidate_present:
                self._remove_restore_tree(
                    candidate,
                    base_dir=board_parent,
                    before_mutation=lambda: fenced(
                        "quarantine_restore_directory_reconcile_cleanup"
                    ),
                )
            if self._plain_restore_directory_present(displaced):
                raise OSError("Grafx restore reconciliation left displaced data")
            if self._plain_restore_directory_present(candidate):
                raise OSError("Grafx restore reconciliation left candidate data")
            if original_inventory is None:
                if self._plain_restore_directory_present(board_dir):
                    raise OSError("Grafx restore reconciliation created live data")
            elif not self._plain_restore_directory_present(board_dir):
                raise OSError("Grafx restore reconciliation lost original data")
            state.update(
                {
                    "phase": "reconciled",
                    "reconciled_as": "original",
                    "reconciled_at": _utc_now().isoformat(),
                }
            )
            fenced("quarantine_restore_directory_reconcile_complete")
            _write_directory_json_atomic(
                operation_path,
                _authenticated_manifest(state),
            )
            return None
        except QuarantineRestoreError:
            raise
        except BaseException as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.PARTIAL_RESTORE,
                reason=(
                    "interrupted Grafx directory restore could not be reconciled: "
                    f"{type(failure).__name__}"
                ),
                details={
                    "operation_manifest": str(operation_path),
                    "source_preserved": True,
                },
            ) from failure

    def _probe_directory(self, path: Path) -> None:
        reject_filesystem_alias_ancestry(path.parent)
        if is_filesystem_alias(path):
            raise OSError("Grafx cold-open candidate is a filesystem alias")
        _probe(self._open_database, path)

    @staticmethod
    def _remove_restore_tree(
        path: Path,
        *,
        base_dir: Path,
        before_mutation: Callable[[], None] | None = None,
    ) -> None:
        remove_contained_tree(
            path,
            base_dir=base_dir,
            before_mutation=before_mutation,
        )
        try:
            base_dir.lstat()
        except FileNotFoundError:
            return
        fsync_directory(base_dir)

    def _rollback_directory_publication(
        self,
        *,
        board_dir: Path,
        candidate: Path,
        displaced: Path,
        original_inventory: GrafxDirectoryInventory | None,
        before_mutation: Callable[[], None],
    ) -> str:
        failed_publish = board_dir.with_name(
            f".{board_dir.name}.{secrets.token_hex(8)}.restore.failed"
        )
        try:
            if displaced.exists():
                if board_dir.exists():
                    before_mutation()
                    os.replace(board_dir, failed_publish)
                    fsync_directory(board_dir.parent)
                before_mutation()
                os.replace(displaced, board_dir)
                fsync_directory(board_dir.parent)
                if (
                    original_inventory is None
                    or grafx_directory_inventory(board_dir) != original_inventory
                ):
                    raise OSError("rolled-back Grafx directory failed integrity")
            elif board_dir.exists() and original_inventory is None:
                before_mutation()
                os.replace(board_dir, failed_publish)
                fsync_directory(board_dir.parent)
            elif board_dir.exists() and (
                grafx_directory_inventory(board_dir) != original_inventory
            ):
                raise OSError("unchanged Grafx live directory failed integrity")
            if candidate.exists():
                self._remove_restore_tree(
                    candidate,
                    base_dir=candidate.parent,
                    before_mutation=before_mutation,
                )
            if failed_publish.exists():
                self._remove_restore_tree(
                    failed_publish,
                    base_dir=failed_publish.parent,
                    before_mutation=before_mutation,
                )
            return "rolled_back"
        except BaseException as failure:  # noqa: BLE001 - returned as evidence
            return f"rollback_failed:{type(failure).__name__}:{failure}"

    def _apply_directory_guarded(
        self,
        plan: RestorePlan,
        *,
        compensation_run_id: str | None = None,
        mutation_fence: Callable[[], bool] | None = None,
    ) -> RestoreReport:
        def fenced(phase: str) -> None:
            if mutation_fence is not None and not mutation_fence():
                raise ValueError("rebuild_compensation_restore_fence_lost")
            self._revalidate_fence(plan.board_id, phase)

        operation_path = self._directory_operation_path(plan, compensation_run_id)
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
        fenced("quarantine_restore_directory_begin")
        payload_root, inventory = self._directory_snapshot(plan.quarantine_id)
        completed = self._completed_directory_operation(
            plan,
            operation_path,
            inventory,
            compensation_run_id,
        )
        if completed is not None:
            fenced("quarantine_restore_directory_complete_retry")
            return completed
        reconciled = self._reconcile_interrupted_directory_operation(
            plan,
            operation_path,
            inventory,
            compensation_run_id,
            fenced=fenced,
        )
        if reconciled is not None:
            return reconciled
        payload_root, inventory = self._directory_snapshot(plan.quarantine_id)

        board_dir = Path(plan.board_dir)
        board_parent = board_dir.parent
        reject_filesystem_alias_ancestry(board_parent)
        if is_filesystem_alias(board_dir):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason="Grafx live generation is a filesystem alias",
            )
        original_inventory: GrafxDirectoryInventory | None = None
        backup_quarantine_id: str | None = None
        if board_dir.exists():
            try:
                original_inventory = grafx_directory_inventory(board_dir)
            except OSError as failure:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.BOARD_LOCKED,
                    reason="Grafx live generation is unsafe",
                ) from failure
            fenced("quarantine_restore_directory_backup")
            affected, backup_quarantine_id = _capture_grafx_board_storage(
                plan.board_id,
                board_dir,
                reason=f"restore_backup_swap:{plan.quarantine_id}",
                remove_source=False,
                source_quarantine_id=plan.quarantine_id,
                compensation_run_id=compensation_run_id,
            )
            if affected <= 0 or not backup_quarantine_id:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.PARTIAL_RESTORE,
                    reason="Grafx directory restore could not publish its live backup",
                )

        candidate = board_parent / (
            f".{board_dir.name}.{secrets.token_hex(8)}.restore.pending"
        )
        displaced = board_parent / (
            f".{board_dir.name}.{secrets.token_hex(8)}.restore.displaced"
        )
        state: dict[str, object] = {
            "format": _DIRECTORY_OPERATION_FORMAT,
            "operation": "grafx_directory_quarantine_restore",
            "source_quarantine_id": plan.quarantine_id,
            "backup_quarantine_id": backup_quarantine_id,
            "board_id": plan.board_id,
            "database_path": plan.board_dir,
            "compensation_run_id": compensation_run_id,
            "candidate_path": str(candidate),
            "displaced_path": str(displaced),
            "inventory_sha256": inventory.sha256,
            "original_inventory": self._inventory_document(original_inventory),
            "phase": "copy_candidate",
            "started_at": _utc_now().isoformat(),
            "open_validated": False,
            "error": None,
        }
        published = False
        published_inventory = inventory
        try:
            _write_directory_json_atomic(
                operation_path,
                _authenticated_manifest(state),
            )
            fenced("quarantine_restore_directory_copy")
            _copy_plain_directory(payload_root, candidate, inventory)
            fenced("quarantine_restore_directory_candidate_open")
            self._probe_directory(candidate)
            published_inventory = grafx_directory_inventory(candidate)
            state.update(
                {
                    "published_directories": list(published_inventory.directories),
                    "published_files": [
                        dict(item) for item in published_inventory.files
                    ],
                    "published_inventory_sha256": published_inventory.sha256,
                }
            )
            state["phase"] = "publish_pending"
            _write_directory_json_atomic(
                operation_path,
                _authenticated_manifest(state),
            )
            fenced("quarantine_restore_directory_publish")
            if board_dir.exists():
                if displaced.exists() or is_filesystem_alias(displaced):
                    raise FileExistsError(displaced)
                os.replace(board_dir, displaced)
                fsync_directory(board_parent)
            os.replace(candidate, board_dir)
            published = True
            fsync_directory(board_parent)
            if grafx_directory_inventory(board_dir) != published_inventory:
                raise OSError("published Grafx directory inventory mismatch")
            fenced("quarantine_restore_directory_cold_open")
            self._probe_directory(board_dir)
            if displaced.exists():
                self._remove_restore_tree(
                    displaced,
                    base_dir=board_parent,
                    before_mutation=lambda: fenced(
                        "quarantine_restore_directory_cleanup"
                    ),
                )
            fenced("quarantine_restore_directory_complete")
            state.update(
                {
                    "phase": "done",
                    "open_validated": True,
                    "finished_at": _utc_now().isoformat(),
                }
            )
            _write_directory_json_atomic(
                operation_path,
                _authenticated_manifest(state),
            )
        except BaseException as failure:
            rollback = self._rollback_directory_publication(
                board_dir=board_dir,
                candidate=candidate,
                displaced=displaced,
                original_inventory=original_inventory,
                before_mutation=lambda: fenced("quarantine_restore_directory_rollback"),
            )
            state.update(
                {
                    "phase": "failed",
                    "published_before_failure": published,
                    "rollback": rollback,
                    "error": {
                        "type": type(failure).__name__,
                        "message": str(failure),
                    },
                    "finished_at": _utc_now().isoformat(),
                }
            )
            manifest_failure: BaseException | None = None
            try:
                _write_directory_json_atomic(
                    operation_path,
                    _authenticated_manifest(state),
                )
            except BaseException as secondary:  # noqa: BLE001
                manifest_failure = secondary
            details: dict[str, object] = {
                "operation_manifest": str(operation_path),
                "backup_quarantine_id": backup_quarantine_id,
                "source_preserved": True,
                "rollback": rollback,
            }
            if manifest_failure is not None:
                details["manifest_error"] = (
                    f"{type(manifest_failure).__name__}: {manifest_failure}"
                )
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.PARTIAL_RESTORE,
                reason=(
                    "Grafx directory restore failed without consuming its source: "
                    f"{type(failure).__name__}"
                ),
                details=details,
            ) from failure

        report = RestoreReport(
            quarantine_id=plan.quarantine_id,
            board_id=plan.board_id,
            applied=True,
            backup_quarantine_id=backup_quarantine_id,
            restored_files=tuple(entry.name for entry in plan.files),
            open_validated=True,
        )
        logger.info(
            "kg.quarantine.restored quarantine_id=%s board=%s kind=%s "
            "backup_quarantine_id=%s files=%d open_validated=true",
            report.quarantine_id,
            report.board_id,
            GRAFX_DIRECTORY_QUARANTINE_KIND,
            report.backup_quarantine_id,
            len(report.restored_files),
            extra={
                "event": "kg.quarantine.restored",
                "quarantine_id": report.quarantine_id,
                "board_id": report.board_id,
                "kind": GRAFX_DIRECTORY_QUARANTINE_KIND,
                "backup_quarantine_id": report.backup_quarantine_id,
                "restored_files": list(report.restored_files),
                "open_validated": True,
                "operation_manifest": str(operation_path),
            },
        )
        return report

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
            board_path = Path(plan.board_dir)
            board_probe = board_path if board_path.exists() else board_path.parent
            reject_filesystem_alias_ancestry(board_probe)
            reject_filesystem_alias_ancestry(self._quarantine_root)
            board_device = os.stat(board_probe).st_dev
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
        reject_filesystem_alias_ancestry(self._quarantine_root)
        path = Path(os.path.abspath(self._quarantine_root / quarantine_id))
        try:
            path.relative_to(Path(os.path.abspath(self._quarantine_root)))
        except ValueError as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason="Grafx quarantine path escapes its root",
            ) from failure
        try:
            metadata = path.lstat()
        except FileNotFoundError as failure:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason=f"Grafx quarantine {quarantine_id!r} not found",
            ) from failure
        if is_filesystem_alias(path) or not stat.S_ISDIR(metadata.st_mode):
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
