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

# Heartbeat schedule. The owner refreshes `heartbeat_at` every
# HEARTBEAT_INTERVAL_SECONDS. A peer that finds a lock with a heartbeat older
# than HEARTBEAT_TTL_SECONDS treats it as orphaned (regardless of whether the
# stored PID is alive — that PID may have been recycled by an unrelated
# process after a crash or reboot). The TTL is intentionally a few multiples
# of the interval so a sleeping laptop doesn't trigger a false takeover.
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_TTL_SECONDS = 120

_ACTIVE_LOCK: "ServeInstanceLock | None" = None


class ServeAlreadyRunningError(RuntimeError):
    """Raised when another local server owns the same data directory."""


class _ReentrantServeLock(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def refresh_heartbeat(self) -> None:
        """No-op — the owning lock in the same process refreshes for us."""
        return None


class ServeInstanceLock(AbstractContextManager["ServeInstanceLock"]):
    """Filesystem PID lock scoped to one resolved Community data directory."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.lock_path = self.data_dir / LOCK_FILENAME
        self._fd: int | None = None

    def acquire(self) -> "ServeInstanceLock":
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # On Windows, os.open defaults to text mode and translates "\n" to
        # "\r\n" on write. With heartbeat refresh we write+truncate the same
        # fd repeatedly; without O_BINARY the truncate length disagrees with
        # the file's on-disk size and readers see corrupt JSON.
        open_flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0)
        while True:
            try:
                self._fd = os.open(str(self.lock_path), open_flags)
            except FileExistsError:
                payload = _read_lock_payload(self.lock_path)
                if _owner_is_live(payload):
                    raise ServeAlreadyRunningError(
                        _format_lock_error(self.data_dir, self.lock_path, payload)
                    )
                _remove_stale_lock(self.lock_path)
                continue
            _write_lock_payload(self._fd, self.data_dir)
            _remember_active_lock(self)
            return self

    def refresh_heartbeat(self) -> None:
        """Stamp a fresh `heartbeat_at` on the open lock file.

        Called periodically by the serve runtime so peers can tell a live
        owner from an orphaned lock left by a crash. No-op if the fd was
        already released.
        """
        if self._fd is None:
            return
        _rewrite_lock_payload(self._fd, self.data_dir)

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


def get_active_lock() -> "ServeInstanceLock | None":
    """Return the lock owned by this process, if any.

    The serve runtime uses this to find the lock object it needs to refresh
    from the background heartbeat task — `acquire_serve_lock` may have
    returned a reentrant no-op manager for a nested caller.
    """
    return _ACTIVE_LOCK


def _remember_active_lock(lock: ServeInstanceLock) -> None:
    global _ACTIVE_LOCK

    _ACTIVE_LOCK = lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_payload(data_dir: Path, *, created_at: str | None = None) -> dict[str, Any]:
    now = _now_iso()
    return {
        "pid": os.getpid(),
        "data_dir": str(data_dir),
        "created_at": created_at or now,
        "heartbeat_at": now,
        "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
        "heartbeat_ttl_seconds": HEARTBEAT_TTL_SECONDS,
    }


def _write_lock_payload(fd: int, data_dir: Path) -> None:
    payload = _build_payload(data_dir)
    os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
    os.fsync(fd)


def _rewrite_lock_payload(fd: int, data_dir: Path) -> None:
    """Rewrite the lock file content with a fresh `heartbeat_at`.

    Uses the same fd opened in acquire(). Preserves `created_at` so the
    operator-facing error message still shows when the server first started.
    """
    existing_created_at: str | None = None
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        existing_bytes = os.read(fd, 8192)
        if existing_bytes:
            try:
                existing_payload = json.loads(existing_bytes.decode("utf-8"))
                if isinstance(existing_payload, dict):
                    raw = existing_payload.get("created_at")
                    if isinstance(raw, str):
                        existing_created_at = raw
            except (UnicodeDecodeError, json.JSONDecodeError):
                existing_created_at = None
    except OSError:
        existing_created_at = None

    payload = _build_payload(data_dir, created_at=existing_created_at)
    encoded = json.dumps(payload, indent=2).encode("utf-8")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, encoded)
        os.ftruncate(fd, len(encoded))
        os.fsync(fd)
    except OSError:
        # Disk full, FS gone read-only, etc — the next acquire from a peer
        # will then see the heartbeat as stale and take over. We refuse to
        # mask the error by swallowing it silently in higher layers.
        raise


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


def _payload_heartbeat_age_seconds(payload: dict[str, Any]) -> float | None:
    """Return seconds since the last `heartbeat_at`, or None if absent/bogus.

    Returning None lets the caller fall back to the legacy PID-only check,
    so lock files written by older versions don't immediately look stale to
    a newer reader."""
    raw = payload.get("heartbeat_at")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - stamp
    return delta.total_seconds()


def _payload_ttl(payload: dict[str, Any]) -> float:
    raw = payload.get("heartbeat_ttl_seconds")
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return HEARTBEAT_TTL_SECONDS
    return ttl if ttl > 0 else HEARTBEAT_TTL_SECONDS


def _owner_is_live(payload: dict[str, Any]) -> bool:
    """Decide whether the lock payload represents a real running owner.

    A payload is treated as live ONLY if both signals agree:
    - the recorded PID still belongs to a running process, AND
    - the `heartbeat_at` stamp is within the configured TTL.

    If `heartbeat_at` is missing (legacy lock written by a pre-heartbeat
    version) we fall back to the PID-only liveness check — the operator
    can still recover by deleting the file manually, same as today.

    A stale heartbeat is sufficient to declare the lock orphaned even when
    the PID is alive: that's how a recycled PID (chrome.exe inherited the
    old number after a reboot) stops blocking startup."""
    pid = _payload_pid(payload)
    if not pid:
        return False
    if not _pid_is_running(pid):
        return False
    age = _payload_heartbeat_age_seconds(payload)
    if age is None:
        return True
    return age <= _payload_ttl(payload)


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
    heartbeat_at = payload.get("heartbeat_at", "unknown")
    return (
        "Another okto-pulse server is already using this data directory.\n"
        f"  Data dir: {data_dir}\n"
        f"  PID: {pid}\n"
        f"  Started at: {created_at}\n"
        f"  Last heartbeat: {heartbeat_at}\n"
        f"  Lock file: {lock_path}\n"
        "Stop the existing server before starting a second one, otherwise the "
        "local Knowledge Graph can be read as empty or lose semantic links.\n"
        f"If you are sure no server is running, wait at least {HEARTBEAT_TTL_SECONDS}s "
        "for the heartbeat to expire (the next start will take the lock over "
        "automatically) or remove the lock file manually."
    )


def reset_serve_lock_for_tests() -> None:
    global _ACTIVE_LOCK

    if _ACTIVE_LOCK is not None:
        _ACTIVE_LOCK.release()
    _ACTIVE_LOCK = None
