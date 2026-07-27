"""Single-instance guard for the local Community server."""

from __future__ import annotations

import ctypes
import json
import os
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout

LOCK_FILENAME = ".okto-pulse-serve.lock"
_ACQUIRE_MUTEX_FILENAME = f"{LOCK_FILENAME}.acquire"

# Heartbeat schedule. The owner refreshes `heartbeat_at` every
# HEARTBEAT_INTERVAL_SECONDS. A peer that finds a lock with a heartbeat older
# than HEARTBEAT_TTL_SECONDS treats it as orphaned (regardless of whether the
# stored PID is alive — that PID may have been recycled by an unrelated
# process after a crash or reboot). The TTL is intentionally a few multiples
# of the interval so a sleeping laptop doesn't trigger a false takeover.
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_TTL_SECONDS = 120
_LOCK_READ_ATTEMPTS = 4
_LOCK_READ_RETRY_SECONDS = 0.01
_ACQUIRE_MUTEX_TIMEOUT_SECONDS = 1.0

_ACTIVE_LOCK: "ServeInstanceLock | None" = None


class ServeAlreadyRunningError(RuntimeError):
    """Raised when another local server owns the same data directory."""


class _UnreadableServeLock(RuntimeError):
    """An existing lock could not be decoded after bounded retries."""


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
        try:
            with _acquisition_mutex(self.data_dir):
                return self._acquire_serialized(open_flags)
        except FileLockTimeout as exc:
            raise ServeAlreadyRunningError(
                _format_acquisition_mutex_error(self.data_dir)
            ) from exc

    def _acquire_serialized(self, open_flags: int) -> "ServeInstanceLock":
        """Create or recover the authoritative lock under a process mutex.

        Serializing the entire read/revalidate/unlink/create sequence prevents
        two stale-lock recoverers from unlinking each other's newly-created
        live lock. ``FileLock`` is kernel-backed and releases on process crash;
        its persistent sidecar file is only the mutex rendezvous path.
        """
        while True:
            try:
                self._fd = os.open(str(self.lock_path), open_flags)
            except FileExistsError:
                try:
                    payload = _read_lock_payload(self.lock_path)
                except _UnreadableServeLock as exc:
                    raise ServeAlreadyRunningError(
                        _format_unreadable_lock_error(
                            self.data_dir,
                            self.lock_path,
                        )
                    ) from exc
                if payload is None:
                    # The previous owner released between O_EXCL and our read.
                    # Retry creation rather than treating absence as corruption.
                    continue
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


def _read_lock_payload_once(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _payload_pid(payload) is None:
        raise ValueError("serve-lock payload must be an object with a positive pid")
    return payload


def _read_lock_payload(path: Path) -> dict[str, Any] | None:
    """Read one existing lock without turning corruption into an orphan.

    Heartbeat refresh rewrites the small JSON document in place through the
    owner's open descriptor. That write is durable but not an atomic pathname
    replacement, so a peer can briefly observe partial JSON. Retry that narrow
    window; if the file remains present and undecodable, fail closed.
    """

    last_error: BaseException | None = None
    for attempt in range(_LOCK_READ_ATTEMPTS):
        try:
            return _read_lock_payload_once(path)
        except FileNotFoundError as exc:
            if not path.exists():
                return None
            last_error = exc
        except (OSError, UnicodeError, ValueError) as exc:
            last_error = exc

        if attempt + 1 < _LOCK_READ_ATTEMPTS:
            time.sleep(_LOCK_READ_RETRY_SECONDS)

    raise _UnreadableServeLock(str(path)) from last_error


def _payload_pid(payload: dict[str, Any]) -> int | None:
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _acquisition_mutex(data_dir: str | Path) -> FileLock:
    resolved = Path(data_dir).expanduser().resolve()
    return FileLock(
        str(resolved / _ACQUIRE_MUTEX_FILENAME),
        timeout=_ACQUIRE_MUTEX_TIMEOUT_SECONDS,
    )


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

    KGD-01 C6 (takeover fail-closed): um lock só é considerado órfão quando
    o PID registrado está COMPROVADAMENTE morto (OpenProcess no Windows /
    kill-0 no POSIX). PID vivo — mesmo com heartbeat stale — é RECUSA: um
    servidor travado (mas vivo) ainda segura os handles do Ladybug, e um
    takeover nesse estado cria o duplo-escritor no mesmo ``graph.lbug`` (o
    "escritor stale" que zera páginas interiores do WAL — KB1/H3).

    O contrato anterior aceitava heartbeat stale como suficiente para
    takeover (cobria PID reciclado após reboot, ao custo do fail-open). No
    cenário raro de PID reciclado o operador remove o lock manualmente —
    ver a mensagem de erro em :func:`_format_lock_error`. O heartbeat segue
    no payload para diagnóstico e para o fast-fail da CLI
    (:func:`assert_no_live_server`)."""
    pid = _payload_pid(payload)
    if not pid:
        return False
    return _pid_is_running(pid)


def assert_no_live_server(
    data_dir: str | Path, *, operation: str = "cli"
) -> None:
    """Fast-fail guard da CLI (KGD-01 C6/S10) — nunca bloqueia (<5s).

    Entrypoints que abrem grafos de board (``init``, ``kg backfill --apply``,
    ``kg dedup-entities``, ``verify-pipeline``, scripts de operador) chamam
    isto ANTES de tocar em qualquer Database. Levanta
    :class:`ServeAlreadyRunningError` quando o serve-lock de ``data_dir``
    tem heartbeat fresco OU um PID comprovadamente vivo. Prossegue apenas
    quando não há lock, ou quando o heartbeat está stale E o PID está morto
    (mesma regra de takeover de :func:`_owner_is_live`, com o heartbeat
    fresco como sinal adicional de recusa — um servidor que acabou de
    morrer pode ter deixado WAL/handles em estado transitório).
    """
    resolved = Path(data_dir).expanduser().resolve()
    if not resolved.is_dir():
        return
    try:
        with _acquisition_mutex(resolved):
            _assert_no_live_server_serialized(resolved, operation=operation)
    except FileLockTimeout as exc:
        raise ServeAlreadyRunningError(
            f"serve-lock: refusing '{operation}' — "
            + _format_acquisition_mutex_error(resolved)
        ) from exc


def _assert_no_live_server_serialized(
    resolved: Path,
    *,
    operation: str,
) -> None:
    lock_path = resolved / LOCK_FILENAME
    if not lock_path.exists():
        return
    try:
        payload = _read_lock_payload(lock_path)
    except _UnreadableServeLock as exc:
        raise ServeAlreadyRunningError(
            f"serve-lock: refusing '{operation}' — "
            + _format_unreadable_lock_error(resolved, lock_path)
        ) from exc
    if payload is None:
        return
    pid = _payload_pid(payload)
    age = _payload_heartbeat_age_seconds(payload)
    heartbeat_fresh = age is not None and age <= _payload_ttl(payload)
    pid_alive = bool(pid) and _pid_is_running(pid)
    if heartbeat_fresh or pid_alive:
        raise ServeAlreadyRunningError(
            f"serve-lock: refusing '{operation}' — "
            + _format_lock_error(resolved, lock_path, payload)
        )


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
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # KGD-01 C6: ACCESS_DENIED significa que o processo EXISTE (só não
        # podemos abri-lo) — fail-closed: tratar como vivo. Qualquer outro
        # erro (INVALID_PARAMETER etc.) = PID inexistente.
        return ctypes.get_last_error() == error_access_denied
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
        "local Knowledge Graph can be read as empty, lose semantic links or "
        "corrupt the board graph WAL (two writers on the same graph.lbug).\n"
        f"Takeover is automatic only when the heartbeat is stale (>{HEARTBEAT_TTL_SECONDS}s) "
        "AND the PID above is provably dead. If the process is alive but "
        "stuck, stop it first; if you are sure no server is running, remove "
        "the lock file manually."
    )


def _format_unreadable_lock_error(data_dir: Path, lock_path: Path) -> str:
    return (
        "An existing okto-pulse serve-lock could not be read safely after "
        "bounded retries.\n"
        f"  Data dir: {data_dir}\n"
        f"  Lock file: {lock_path}\n"
        "The lock may be undergoing a heartbeat rewrite or may be corrupt. "
        "Refusing to proceed because the owner PID and liveness cannot be "
        "verified. Wait briefly and retry; if you are sure no server is "
        "running, remove the lock file manually."
    )


def _format_acquisition_mutex_error(data_dir: Path) -> str:
    return (
        "Timed out waiting for the okto-pulse serve-lock acquisition mutex. "
        "Another process is starting or recovering the server for data "
        f"directory {data_dir}. Wait briefly and retry."
    )


def reset_serve_lock_for_tests() -> None:
    global _ACTIVE_LOCK

    if _ACTIVE_LOCK is not None:
        _ACTIVE_LOCK.release()
    _ACTIVE_LOCK = None
