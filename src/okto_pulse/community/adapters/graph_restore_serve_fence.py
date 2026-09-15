"""Serve-lock fencing shared by governed graph storage restores."""

from __future__ import annotations
import os
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator
from okto_pulse.core.kg.interfaces.quarantine_restore import (
    QuarantineRestoreError,
    QuarantineRestoreErrorCode,
)


class GraphRestoreServeFence:
    def __init__(self, *, base_dir: Path, extra_serve_lock_dirs: tuple[Path, ...] = ()):
        self._base_dir_override = Path(base_dir)
        self._extra_serve_lock_dirs = tuple(Path(p) for p in extra_serve_lock_dirs)

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
