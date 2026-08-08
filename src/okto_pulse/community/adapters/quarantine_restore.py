"""Community QuarantineRestore adapter (spec KGD-01 FR4/BR4/TR6, card d9414790).

Concrete implementation of the ``QuarantineRestore`` port
(``okto_pulse.core.kg.interfaces.quarantine_restore``) over the local KG
storage layout:

    <kg_base>/
        boards/<board_id>/graph.lbug[.wal|.shadow|...]
        quarantine/<quarantine_id>/{manifest.json|manifest.txt} + files

``plan`` is a pure read (dry-run): it tolerates every manifest format found
on disk today — ``manifest.json`` of the ``q_*`` entries (canonical
KGQuarantineService schema), ``manifest.json`` of the legacy ``kg-wal-*``
entries (``kind: kg_wal_quarantine``) and the free-text ``manifest.txt`` of
the ``interrupted-checkpoint-*`` entries (plan derived from the directory's
file inventory; board_id parsed from the directory name / manifest text).

``apply`` performs the backup-swap (BR4): refuse while the board is live
(serve-lock + exclusive move/open probe -> ``board_locked``); move the
board's live files into a NEW quarantine with manifest; copy the snapshot
files back (the quarantine source stays immutable); validate the open via
the single ``_open_kuzu_db`` factory; emit ``kg.quarantine.restored``.
Windows-first (TR6): ``gc.collect()`` before moves and a short
PermissionError retry; any mid-flight failure records the exact
moved/copied state + rollback instruction in the operation manifest and
raises ``partial_restore`` — never a silent half-restored board.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import re
import secrets
import shutil
import time
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from okto_pulse.core.kg.interfaces.quarantine_restore import (
    QuarantineRestoreError,
    QuarantineRestoreErrorCode,
    RestoreFileEntry,
    RestorePlan,
    RestoreReport,
)

logger = logging.getLogger("okto_pulse.kg.quarantine.restore")

QUARANTINE_DIRNAME = "quarantine"
BOARDS_DIRNAME = "boards"
MANIFEST_JSON = "manifest.json"
MANIFEST_TXT = "manifest.txt"
OPERATION_MANIFEST = "restore_operation.json"
BACKUP_RETENTION_DAYS = 30

# Short PermissionError retry (TR6 — NTFS handles): attempts with tiny
# backoff so a transiently-held handle (AV scan, indexer) does not abort the
# swap, while a real lock fails fast enough for tests and operators.
_MOVE_ATTEMPTS = 3
_MOVE_BACKOFF_BASE_SECONDS = 0.05

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

_NON_RESTORABLE_NAMES = frozenset({MANIFEST_JSON, MANIFEST_TXT, OPERATION_MANIFEST})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _software_version() -> str:
    try:
        from importlib.metadata import version

        return version("okto-pulse")
    except Exception:
        try:
            from importlib.metadata import version

            return version("okto-pulse-core")
        except Exception:
            return "unknown"


class CommunityQuarantineRestore:
    """Filesystem adapter for the ``QuarantineRestore`` port."""

    def __init__(
        self,
        *,
        base_dir: Path | str | None = None,
        extra_serve_lock_dirs: tuple[Path, ...] = (),
        retention_days: int = BACKUP_RETENTION_DAYS,
        lock_owner_probe: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._base_dir_override = Path(base_dir) if base_dir is not None else None
        self._extra_serve_lock_dirs = tuple(Path(p) for p in extra_serve_lock_dirs)
        self._retention_days = retention_days
        self._lock_owner_probe = lock_owner_probe

    # ------------------------------------------------------------------ port

    def plan(self, quarantine_id: str) -> RestorePlan:
        plan = self._build_plan(quarantine_id)
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
        plan = self._build_plan(quarantine_id)
        return self._apply_plan(plan)

    def apply_rebuild_compensation(
        self,
        quarantine_id: str,
        *,
        expected_board_id: str,
        run_id: str,
        owner_token: str | None,
    ) -> RestoreReport:
        """Restore under the live server's governed recovery fence.

        This concrete-only capability is deliberately absent from the public
        ``QuarantineRestore`` port. Manual/operator ``apply`` keeps requiring
        a stopped server; rebuild compensation proves reader/writer quiescence
        through the Community runtime before replacing graph files.
        """
        if not expected_board_id or not run_id or not owner_token:
            raise ValueError("rebuild_compensation_restore_identity_invalid")
        plan = self._build_plan(quarantine_id)
        if plan.board_id != expected_board_id:
            raise ValueError("rebuild_compensation_restore_board_mismatch")
        owner_probe = self._lock_owner_probe
        if owner_probe is None:
            from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

            owner_probe = KGSingleWriterLock().is_owner
        if not owner_probe(plan.board_id, owner_token):
            raise ValueError("rebuild_compensation_restore_fence_lost")

        from okto_pulse.community.adapters.kg_runtime import (
            board_storage_mutation_window,
        )

        with board_storage_mutation_window(
            plan.board_id,
            phase=f"rebuild_compensation_restore:{run_id}",
        ):
            # Entering the writer window may wait for the gate/readers. The
            # lease can expire during that wait, so the pre-window proof is
            # insufficient by itself.
            if not owner_probe(plan.board_id, owner_token):
                raise ValueError("rebuild_compensation_restore_fence_lost")
            report = self._apply_plan(
                plan,
                allow_owned_serve_lock=True,
                compensation_run_id=run_id,
                mutation_fence=lambda: bool(owner_probe(plan.board_id, owner_token)),
            )
            # The open probe may be non-trivial.  Revalidate the same owner
            # fence immediately before authorizing compensation success.
            if not owner_probe(plan.board_id, owner_token):
                raise ValueError("rebuild_compensation_restore_fence_lost")
            return report

    def discard_rebuild_candidate(
        self,
        *,
        expected_board_id: str,
        run_id: str,
        owner_token: str | None,
    ) -> dict[str, object]:
        """Quarantine a failed fresh-board candidate and prove it is not live.

        Existing-board compensation uses ``apply_rebuild_compensation``: its
        backup-swap removes the candidate and restores the previous graph.
        A fresh board has no prior quarantine to restore, so it needs this
        separate, fenced physical discard.  The invalid candidate is moved to
        an auditable quarantine instead of being left at the live board path.
        """

        if not expected_board_id or not run_id or not owner_token:
            raise ValueError("rebuild_candidate_discard_identity_invalid")
        owner_probe = self._lock_owner_probe
        if owner_probe is None:
            from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

            owner_probe = KGSingleWriterLock().is_owner
        if not owner_probe(expected_board_id, owner_token):
            raise ValueError("rebuild_candidate_discard_fence_lost")

        from okto_pulse.community.adapters.kg_runtime import (
            board_kuzu_path,
            board_storage_mutation_window,
            purge_board_graph_storage_with_receipt,
        )

        with board_storage_mutation_window(
            expected_board_id,
            phase=f"rebuild_candidate_discard:{run_id}",
        ):
            if not owner_probe(expected_board_id, owner_token):
                raise ValueError("rebuild_candidate_discard_fence_lost")
            affected, quarantine_id = purge_board_graph_storage_with_receipt(
                expected_board_id,
                reason=f"failed_rebuild_candidate:{run_id}",
            )
            path = board_kuzu_path(expected_board_id)
            live_residue = path.exists() or (
                path.parent.exists()
                and any(path.parent.glob(path.name + ".*"))
            )
            if live_residue:
                raise RuntimeError("rebuild_candidate_discard_unverified")
            # Purging and the final filesystem probe can take long enough for
            # the rebuild lease to expire.  Never authorize a successful
            # compensation receipt after ownership was lost mid-operation.
            if not owner_probe(expected_board_id, owner_token):
                raise ValueError("rebuild_candidate_discard_fence_lost")
        return {
            "status": "discarded" if affected else "already_absent",
            "discarded_files": len(affected),
            "quarantine_id": quarantine_id,
            "live_absent": True,
        }

    def _apply_plan(
        self,
        plan: RestorePlan,
        *,
        allow_owned_serve_lock: bool = False,
        compensation_run_id: str | None = None,
        mutation_fence: Callable[[], bool] | None = None,
    ) -> RestoreReport:
        with self._serve_lock_fence(plan.board_id) as serve_lock_directories:
            return self._apply_plan_fenced(
                plan,
                allow_owned_serve_lock=allow_owned_serve_lock,
                compensation_run_id=compensation_run_id,
                mutation_fence=mutation_fence,
                serve_lock_directories=serve_lock_directories,
            )

    def _apply_plan_fenced(
        self,
        plan: RestorePlan,
        *,
        allow_owned_serve_lock: bool = False,
        compensation_run_id: str | None = None,
        mutation_fence: Callable[[], bool] | None = None,
        serve_lock_directories: tuple[Path, ...],
    ) -> RestoreReport:
        board_dir = Path(plan.board_dir)
        quarantine_dir = self._quarantine_dir(plan.quarantine_id)

        live_files = self._live_board_files(board_dir)
        self._ensure_board_not_live(
            plan.board_id,
            board_dir,
            live_files,
            allow_owned_serve_lock=allow_owned_serve_lock,
            serve_lock_mutex_held=True,
            serve_lock_directories=serve_lock_directories,
        )
        if mutation_fence is not None and not mutation_fence():
            raise ValueError("rebuild_compensation_restore_fence_lost")

        backup_quarantine_id = f"q_{secrets.token_urlsafe(16)}"
        backup_dir = self._quarantine_root() / backup_quarantine_id
        try:
            backup_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.PARTIAL_RESTORE,
                reason=f"backup quarantine dir could not be created: {exc}",
                details={"backup_quarantine_id": backup_quarantine_id},
            ) from exc

        op_path = backup_dir / OPERATION_MANIFEST
        state: dict[str, Any] = {
            "operation": "quarantine_restore",
            "compensation_run_id": compensation_run_id,
            "source_quarantine_id": plan.quarantine_id,
            "source_quarantine_dir": str(quarantine_dir),
            "backup_quarantine_id": backup_quarantine_id,
            "backup_quarantine_dir": str(backup_dir),
            "board_id": plan.board_id,
            "board_dir": str(board_dir),
            "phase": "backup_swap",
            "started_at": _utc_now().isoformat(),
            "finished_at": None,
            "moved_to_backup": [],
            "pending_backup": [f.name for f in live_files],
            "copied_from_snapshot": [],
            "pending_copy": [entry.name for entry in plan.files],
            "open_validated": None,
            "error": None,
            "rollback_instruction": None,
        }
        self._write_json(op_path, state)

        # -- backup-swap: move the board's live files into the NEW quarantine.
        gc.collect()  # TR6: drop lingering C++ handles before NTFS moves.
        for live in live_files:
            if mutation_fence is not None and not mutation_fence():
                self._fail_partial(
                    state,
                    op_path,
                    RuntimeError("rebuild_compensation_restore_fence_lost"),
                    step=f"fence_lost:backup_swap:{live.name}",
                    board_dir=board_dir,
                    backup_dir=backup_dir,
                )
            try:
                self._move_with_retry(live, backup_dir / live.name)
            except OSError as exc:
                self._fail_partial(
                    state,
                    op_path,
                    exc,
                    step=f"backup_swap:{live.name}",
                    board_dir=board_dir,
                    backup_dir=backup_dir,
                )
            state["moved_to_backup"].append(live.name)
            state["pending_backup"].remove(live.name)
            self._write_json(op_path, state)

        now = _utc_now()
        backup_manifest = {
            "quarantine_id": backup_quarantine_id,
            "board_id": plan.board_id,
            "graph_type": "board_graph",
            "reason": f"restore_backup_swap:{plan.quarantine_id}",
            "reason_bucket": (
                "rebuild_compensation"
                if compensation_run_id is not None
                else "operator_manual"
            ),
            "correlation_ids": [
                value
                for value in (plan.quarantine_id, compensation_run_id)
                if value is not None
            ],
            "affected_paths_relative": list(state["moved_to_backup"]),
            "kg_generation_id": None,
            "software_version": _software_version(),
            "quarantined_at": now.isoformat(),
            "retention_until": (now + timedelta(days=self._retention_days)).isoformat(),
            "files_moved": len(state["moved_to_backup"]),
        }
        try:
            self._write_json(backup_dir / MANIFEST_JSON, backup_manifest)
        except OSError as exc:
            self._fail_partial(
                state,
                op_path,
                exc,
                step="backup_manifest_write",
                board_dir=board_dir,
                backup_dir=backup_dir,
            )

        # -- restore: COPY the snapshot files back (quarantine stays intact).
        state["phase"] = "copy_snapshot"
        self._write_json(op_path, state)
        board_dir.mkdir(parents=True, exist_ok=True)
        for entry in plan.files:
            if mutation_fence is not None and not mutation_fence():
                self._fail_partial(
                    state,
                    op_path,
                    RuntimeError("rebuild_compensation_restore_fence_lost"),
                    step=f"fence_lost:copy_snapshot:{entry.name}",
                    board_dir=board_dir,
                    backup_dir=backup_dir,
                )
            try:
                shutil.copy2(entry.source_path, entry.destination_path)
            except OSError as exc:
                self._fail_partial(
                    state,
                    op_path,
                    exc,
                    step=f"copy_snapshot:{entry.name}",
                    board_dir=board_dir,
                    backup_dir=backup_dir,
                )
            state["copied_from_snapshot"].append(entry.name)
            state["pending_copy"].remove(entry.name)
            self._write_json(op_path, state)

        # -- validate the restored board open via the single factory.
        state["phase"] = "validate_open"
        self._write_json(op_path, state)
        open_validated, open_error = self._validate_open(board_dir)

        state["phase"] = "done"
        state["open_validated"] = open_validated
        state["finished_at"] = _utc_now().isoformat()
        if open_error:
            state["error"] = {"step": "validate_open", "message": open_error}
        self._write_json(op_path, state)

        logger.info(
            "kg.quarantine.restored quarantine_id=%s board=%s "
            "backup_quarantine_id=%s files=%d open_validated=%s",
            plan.quarantine_id,
            plan.board_id,
            backup_quarantine_id,
            len(state["copied_from_snapshot"]),
            open_validated,
            extra={
                "event": "kg.quarantine.restored",
                "quarantine_id": plan.quarantine_id,
                "board_id": plan.board_id,
                "backup_quarantine_id": backup_quarantine_id,
                "restored_files": list(state["copied_from_snapshot"]),
                "open_validated": open_validated,
                "operation_manifest": str(op_path),
            },
        )
        return RestoreReport(
            quarantine_id=plan.quarantine_id,
            board_id=plan.board_id,
            applied=True,
            backup_quarantine_id=backup_quarantine_id,
            restored_files=tuple(state["copied_from_snapshot"]),
            open_validated=open_validated,
            errors=(open_error,) if open_error else (),
        )

    # ------------------------------------------------------------- plan bits

    def _build_plan(self, quarantine_id: str) -> RestorePlan:
        quarantine_dir = self._quarantine_dir(quarantine_id)
        board_id, manifest_format = self._resolve_board_id(
            quarantine_id, quarantine_dir
        )
        board_dir = self._boards_root() / board_id

        entries: list[RestoreFileEntry] = []
        total_bytes = 0
        conflicts: list[str] = []
        for src in self._snapshot_files(quarantine_dir):
            dest = board_dir / src.name
            size = src.stat().st_size
            conflict = dest.is_file()
            live_size = dest.stat().st_size if conflict else None
            if conflict:
                conflicts.append(src.name)
            total_bytes += size
            entries.append(
                RestoreFileEntry(
                    name=src.name,
                    source_path=str(src),
                    destination_path=str(dest),
                    size_bytes=size,
                    conflict=conflict,
                    live_size_bytes=live_size,
                )
            )
        if not entries:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason=(
                    f"quarantine {quarantine_id!r} contains no restorable "
                    "files (manifest-only directory)"
                ),
                details={"quarantine_dir": str(quarantine_dir)},
            )
        return RestorePlan(
            quarantine_id=quarantine_id,
            board_id=board_id,
            board_dir=str(board_dir),
            manifest_format=manifest_format,
            files=tuple(entries),
            conflicts=tuple(conflicts),
            total_bytes=total_bytes,
        )

    def _quarantine_dir(self, quarantine_id: str) -> Path:
        if (
            not quarantine_id
            or "/" in quarantine_id
            or "\\" in quarantine_id
            or ".." in quarantine_id
        ):
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason=f"invalid quarantine_id: {quarantine_id!r}",
            )
        quarantine_dir = self._quarantine_root() / quarantine_id
        if not quarantine_dir.is_dir():
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
                reason=f"quarantine {quarantine_id!r} not found",
                details={"quarantine_dir": str(quarantine_dir)},
            )
        return quarantine_dir

    def _resolve_board_id(
        self, quarantine_id: str, quarantine_dir: Path
    ) -> tuple[str, str]:
        """Resolve the destination board tolerating every on-disk format."""
        manifest_json = quarantine_dir / MANIFEST_JSON
        if manifest_json.is_file():
            try:
                payload = json.loads(manifest_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                board_id = payload.get("board_id")
                if isinstance(board_id, str) and board_id:
                    return board_id, MANIFEST_JSON
                # kg-wal legacy fallback: derive from original_board_dir.
                original = payload.get("original_board_dir")
                if isinstance(original, str) and original:
                    match = _UUID_RE.search(original)
                    if match:
                        return match.group(0), MANIFEST_JSON

        manifest_format = (
            MANIFEST_TXT if (quarantine_dir / MANIFEST_TXT).is_file() else "inventory"
        )
        # interrupted-checkpoint-<board_id>-<ts> / kg-wal-<board_id>-<ts>
        match = _UUID_RE.search(quarantine_dir.name)
        if match:
            return match.group(0), manifest_format
        if manifest_format == MANIFEST_TXT:
            try:
                text = (quarantine_dir / MANIFEST_TXT).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                text = ""
            match = _UUID_RE.search(text)
            if match:
                return match.group(0), manifest_format
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
            reason=(
                f"quarantine {quarantine_id!r}: destination board_id could "
                "not be resolved from manifest or directory name"
            ),
            details={"quarantine_dir": str(quarantine_dir)},
        )

    @staticmethod
    def _snapshot_files(quarantine_dir: Path) -> list[Path]:
        return sorted(
            (
                p
                for p in quarantine_dir.iterdir()
                if p.is_file() and p.name not in _NON_RESTORABLE_NAMES
            ),
            key=lambda p: p.name,
        )

    @staticmethod
    def _live_board_files(board_dir: Path) -> list[Path]:
        if not board_dir.is_dir():
            return []
        return sorted(
            (p for p in board_dir.iterdir() if p.is_file()),
            key=lambda p: p.name,
        )

    # ------------------------------------------------------------ apply bits

    def _serve_lock_directories(self) -> tuple[Path, ...]:
        candidates = {self._resolved_base_dir(), *self._extra_serve_lock_dirs}
        return tuple(
            sorted(
                {Path(directory).expanduser().resolve() for directory in candidates},
                key=str,
            )
        )

    @contextmanager
    def _serve_lock_fence(self, board_id: str) -> Iterator[tuple[Path, ...]]:
        """Fence server startup for the full destructive restore operation."""
        from okto_pulse.community import serve_lock as _serve_lock

        stack = ExitStack()
        directories: tuple[Path, ...] = ()
        try:
            directories = self._serve_lock_directories()
            for directory in directories:
                directory.mkdir(parents=True, exist_ok=True)
                stack.enter_context(_serve_lock._acquisition_mutex(directory))
        except OSError as exc:
            stack.close()
            state = (
                "acquisition_in_progress"
                if isinstance(exc, _serve_lock.FileLockTimeout)
                else "acquisition_unavailable"
            )
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason=(
                    "the serve-lock startup fence could not be acquired; "
                    "another server may be starting or the lock path is "
                    "unavailable"
                ),
                details={
                    "board_id": board_id,
                    "serve_lock": {
                        "state": state,
                        "error_type": type(exc).__name__,
                    },
                },
            ) from exc
        try:
            yield directories
        finally:
            stack.close()

    def _ensure_board_not_live(
        self,
        board_id: str,
        board_dir: Path,
        live_files: list[Path],
        *,
        allow_owned_serve_lock: bool = False,
        serve_lock_mutex_held: bool = False,
        serve_lock_directories: tuple[Path, ...] | None = None,
    ) -> None:
        """Refuse apply while a live server/process holds the board (BR4)."""
        lock_owner = self._live_serve_lock(
            allow_owned_serve_lock=allow_owned_serve_lock,
            serve_lock_mutex_held=serve_lock_mutex_held,
            serve_lock_directories=serve_lock_directories,
        )
        if lock_owner is not None:
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason=(
                    "an okto-pulse server is running against this data "
                    "directory; stop the server (maintenance window) before "
                    "applying a quarantine restore"
                ),
                details={
                    "board_id": board_id,
                    "serve_lock": lock_owner,
                },
            )
        gc.collect()  # release our own lingering handles before probing
        for live in live_files:
            self._probe_exclusive(board_id, live)

    def _live_serve_lock(
        self,
        *,
        allow_owned_serve_lock: bool = False,
        serve_lock_mutex_held: bool = False,
        serve_lock_directories: tuple[Path, ...] | None = None,
    ) -> dict[str, Any] | None:
        from okto_pulse.community import serve_lock as _serve_lock

        active_lock = _serve_lock.get_active_lock() if allow_owned_serve_lock else None
        active_path = (
            active_lock.lock_path.resolve() if active_lock is not None else None
        )
        try:
            directories = (
                serve_lock_directories
                if serve_lock_directories is not None
                else self._serve_lock_directories()
            )
        except OSError as exc:
            return {
                "lock_path": None,
                "pid": None,
                "heartbeat_at": None,
                "state": "acquisition_unavailable",
                "error_type": type(exc).__name__,
            }
        for resolved_directory in directories:
            lock_path = resolved_directory / _serve_lock.LOCK_FILENAME
            try:
                mutex = (
                    nullcontext()
                    if serve_lock_mutex_held
                    else _serve_lock._acquisition_mutex(resolved_directory)
                )
                with mutex:
                    if not lock_path.is_file():
                        continue
                    try:
                        payload = _serve_lock._read_lock_payload(lock_path)
                    except _serve_lock._UnreadableServeLock:
                        # The same fail-closed contract as the CLI guard: a
                        # present lock whose owner cannot be verified must
                        # block a destructive restore while preserving the
                        # public BOARD_LOCKED envelope.
                        return {
                            "lock_path": str(lock_path),
                            "pid": None,
                            "heartbeat_at": None,
                            "state": "unreadable",
                        }
                    if payload is None:
                        # The owner released between is_file() and the bounded
                        # read. With no lock left there is nothing to fence.
                        continue
                    if not _serve_lock._owner_is_live(payload):
                        continue
                    try:
                        lock_pid = int(payload.get("pid") or 0)
                    except (TypeError, ValueError):
                        lock_pid = 0
                    if (
                        active_path is not None
                        and lock_path.resolve() == active_path
                        and lock_pid == os.getpid()
                    ):
                        # Governed rebuild compensation owns the process lock
                        # and is already inside the writer+reader mutation
                        # window. Continue scanning: any second live lock still
                        # blocks.
                        continue
                    return {
                        "lock_path": str(lock_path),
                        "pid": payload.get("pid"),
                        "heartbeat_at": payload.get("heartbeat_at"),
                    }
            except OSError as exc:
                return {
                    "lock_path": str(lock_path),
                    "pid": None,
                    "heartbeat_at": None,
                    "state": (
                        "acquisition_in_progress"
                        if isinstance(exc, _serve_lock.FileLockTimeout)
                        else "acquisition_unavailable"
                    ),
                    "error_type": type(exc).__name__,
                }
        return None

    @staticmethod
    def _probe_exclusive(board_id: str, path: Path) -> None:
        """Exclusivity probe: open + move-in-place must both succeed.

        On Windows an engine that holds ``graph.lbug`` denies sharing, so
        either the RW open or the rename raises PermissionError — the
        structured ``board_locked`` tells the operator to use a maintenance
        window instead of leaving a mid-swap failure.
        """

        def _locked(exc: OSError) -> QuarantineRestoreError:
            return QuarantineRestoreError(
                QuarantineRestoreErrorCode.BOARD_LOCKED,
                reason=(
                    f"live board file {path.name!r} is held by another "
                    "process; stop the server / wait for the maintenance "
                    "window before applying the restore"
                ),
                details={"board_id": board_id, "path": str(path), "os_error": str(exc)},
            )

        try:
            fd = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
            os.close(fd)
        except PermissionError as exc:
            raise _locked(exc) from exc
        except OSError:
            return  # vanished/odd file: the move step will surface it
        probe = path.with_name(path.name + ".restore-probe")
        try:
            os.rename(path, probe)
        except PermissionError as exc:
            raise _locked(exc) from exc
        except OSError:
            return
        try:
            os.rename(probe, path)
        except OSError as exc:  # pragma: no cover - restore-name must succeed
            raise QuarantineRestoreError(
                QuarantineRestoreErrorCode.PARTIAL_RESTORE,
                reason=(
                    f"lock probe could not rename {probe.name!r} back to "
                    f"{path.name!r}: {exc}"
                ),
                details={"board_id": board_id, "probe_path": str(probe)},
            ) from exc

    @staticmethod
    def _move_with_retry(src: Path, dst: Path) -> None:
        """shutil.move with a short PermissionError retry (TR6)."""
        last_exc: OSError | None = None
        for attempt in range(_MOVE_ATTEMPTS):
            try:
                shutil.move(str(src), str(dst))
                return
            except PermissionError as exc:
                last_exc = exc
                gc.collect()
                time.sleep(_MOVE_BACKOFF_BASE_SECONDS * (2**attempt))
        assert last_exc is not None
        raise last_exc

    def _fail_partial(
        self,
        state: dict[str, Any],
        op_path: Path,
        exc: BaseException,
        *,
        step: str,
        board_dir: Path,
        backup_dir: Path,
    ) -> None:
        """Record the exact mid-flight state + rollback and raise (BR4/TR6)."""
        rollback = (
            "Rollback manual: (1) mova os arquivos listados em "
            f"'moved_to_backup' de {backup_dir} de volta para {board_dir}; "
            "(2) remova do board os arquivos listados em "
            "'copied_from_snapshot'; (3) o snapshot original em "
            f"{state['source_quarantine_dir']} permanece intacto e pode ser "
            "re-aplicado após resolver a causa (handle NTFS/permissão)."
        )
        state["error"] = {
            "step": step,
            "type": type(exc).__name__,
            "message": str(exc),
        }
        state["rollback_instruction"] = rollback
        state["finished_at"] = _utc_now().isoformat()
        try:
            self._write_json(op_path, state)
        except OSError:
            logger.error(
                "kg.quarantine.restore_operation_manifest_write_failed path=%s",
                op_path,
                extra={
                    "event": "kg.quarantine.restore_operation_manifest_write_failed",
                    "operation_manifest": str(op_path),
                },
            )
        logger.error(
            "kg.quarantine.restore_partial quarantine_id=%s board=%s step=%s err=%s",
            state["source_quarantine_id"],
            state["board_id"],
            step,
            exc,
            extra={
                "event": "kg.quarantine.restore_partial",
                "quarantine_id": state["source_quarantine_id"],
                "board_id": state["board_id"],
                "backup_quarantine_id": state["backup_quarantine_id"],
                "step": step,
                "operation_manifest": str(op_path),
            },
        )
        raise QuarantineRestoreError(
            QuarantineRestoreErrorCode.PARTIAL_RESTORE,
            reason=(
                f"restore failed mid-flight at {step}: {exc}. The operation "
                "manifest records the exact state and rollback instruction."
            ),
            details={
                "operation_manifest": str(op_path),
                "backup_quarantine_id": state["backup_quarantine_id"],
                "moved_to_backup": list(state["moved_to_backup"]),
                "pending_backup": list(state["pending_backup"]),
                "copied_from_snapshot": list(state["copied_from_snapshot"]),
                "pending_copy": list(state["pending_copy"]),
                "rollback_instruction": rollback,
            },
        ) from exc

    def _validate_open(self, board_dir: Path) -> tuple[bool, str | None]:
        """Open the restored board through the single ``_open_kuzu_db`` factory."""
        from okto_pulse.community.adapters import kg_runtime

        graph_path = board_dir / kg_runtime.GRAPH_DB_FILENAME
        if not graph_path.is_file():
            return False, (
                f"open_validation_failed: {kg_runtime.GRAPH_DB_FILENAME} "
                "missing after restore"
            )
        gc.collect()
        try:
            db = kg_runtime._open_kuzu_db(graph_path)
        except Exception as exc:
            gc.collect()
            return False, f"open_validation_failed: {exc}"
        try:
            close = getattr(db, "close", None)
            if callable(close):
                close()
        finally:
            del db
            gc.collect()
        return True, None

    # ------------------------------------------------------------ fs helpers

    def _resolved_base_dir(self) -> Path:
        if self._base_dir_override is not None:
            return self._base_dir_override.expanduser().resolve()
        raw: Any = None
        try:
            from okto_pulse.core.services.application_kg import (
                get_current_provider_registry,
            )

            raw = get_current_provider_registry().config.kg_base_dir
        except Exception:
            raw = None
        if not raw:
            from okto_pulse.core import get_settings

            raw = get_settings().kg_base_dir
        return Path(os.path.expanduser(str(raw))).resolve()

    def _quarantine_root(self) -> Path:
        return self._resolved_base_dir() / QUARANTINE_DIRNAME

    def _boards_root(self) -> Path:
        return self._resolved_base_dir() / BOARDS_DIRNAME

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)


__all__ = ["CommunityQuarantineRestore"]
