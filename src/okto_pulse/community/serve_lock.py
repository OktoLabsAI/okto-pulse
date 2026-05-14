"""Single-instance guard for the local Community server."""

from __future__ import annotations

import ctypes
import json
import os
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCK_FILENAME = ".okto-pulse-serve.lock"

_ACTIVE_LOCK: "ServeInstanceLock | None" = None


class ServeAlreadyRunningError(RuntimeError):
    """Raised when another local server owns the same data directory."""


class _ReentrantServeLock(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class ServeInstanceLock(AbstractContextManager["ServeInstanceLock"]):
    """Filesystem PID lock scoped to one resolved Community data directory."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.lock_path = self.data_dir / LOCK_FILENAME
        self._fd: int | None = None

    def acquire(self) -> "ServeInstanceLock":
        self.data_dir.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
            except FileExistsError:
                payload = _read_lock_payload(self.lock_path)
                pid = _payload_pid(payload)
                if pid and _pid_is_running(pid):
                    raise ServeAlreadyRunningError(
                        _format_lock_error(self.data_dir, self.lock_path, payload)
                    )
                _remove_stale_lock(self.lock_path)
                continue
            _write_lock_payload(self._fd, self.data_dir)
            _remember_active_lock(self)
            return self

    def release(self) -> None:
        global _ACTIVE_LOCK

        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        finally:
            if _ACTIVE_LOCK is self:
                _ACTIVE_LOCK = None

    def __enter__(self) -> "ServeInstanceLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
        return None


def acquire_serve_lock(settings_or_data_dir: Any) -> AbstractContextManager[Any]:
    """Acquire the process-wide Community serve lock.

    ``cmd_serve`` and ``main.run`` can both call this safely; the second call
    from the same process is a no-op context manager.
    """
    global _ACTIVE_LOCK

    data_dir = Path(
        getattr(settings_or_data_dir, "data_dir", settings_or_data_dir)
    ).expanduser().resolve()
    if _ACTIVE_LOCK and _ACTIVE_LOCK.data_dir == data_dir:
        return _ReentrantServeLock()
    return ServeInstanceLock(data_dir).acquire()


def _remember_active_lock(lock: ServeInstanceLock) -> None:
    global _ACTIVE_LOCK

    _ACTIVE_LOCK = lock


def _write_lock_payload(fd: int, data_dir: Path) -> None:
    payload = {
        "pid": os.getpid(),
        "data_dir": str(data_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
    os.fsync(fd)


def _read_lock_payload(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _payload_pid(payload: dict[str, Any]) -> int | None:
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_is_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_pid_is_running(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _remove_stale_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _format_lock_error(data_dir: Path, lock_path: Path, payload: dict[str, Any]) -> str:
    pid = payload.get("pid", "unknown")
    created_at = payload.get("created_at", "unknown")
    return (
        "Another okto-pulse server is already using this data directory.\n"
        f"  Data dir: {data_dir}\n"
        f"  PID: {pid}\n"
        f"  Started at: {created_at}\n"
        f"  Lock file: {lock_path}\n"
        "Stop the existing server before starting a second one, otherwise the "
        "local Knowledge Graph can be read as empty or lose semantic links."
    )


def reset_serve_lock_for_tests() -> None:
    global _ACTIVE_LOCK

    if _ACTIVE_LOCK is not None:
        _ACTIVE_LOCK.release()
    _ACTIVE_LOCK = None
