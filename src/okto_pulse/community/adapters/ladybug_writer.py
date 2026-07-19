"""Process-wide writer serialization for the embedded Ladybug runtime.

Ladybug 0.16 allows one write transaction *in the process*, including writes
to different Database instances.  Board graph commits therefore must also be
serialized with Global Discovery bootstrap and connection-local VECTOR
extension initialization.  This adapter-owned gate models that concrete
engine constraint without leaking it into Core policy.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention

logger = logging.getLogger("okto_pulse.community.ladybug_writer")

DEFAULT_WRITER_TIMEOUT_S = 30.0

_writer_lock = threading.Lock()
_writer_owner_guard = threading.Lock()
_writer_owner: tuple[str, str, int, float] | None = None
_writer_lease_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "community_ladybug_writer_lease_active",
    default=False,
)


def writer_lease_is_active() -> bool:
    """Return whether this logical call path already owns the global lease."""

    return _writer_lease_active.get()


def _owner_snapshot() -> tuple[str, str, int, int] | None:
    with _writer_owner_guard:
        owner = _writer_owner
    if owner is None:
        return None
    scope, phase, thread_id, acquired_at = owner
    return scope, phase, thread_id, int((time.monotonic() - acquired_at) * 1000)


def _set_owner(*, scope: str, phase: str) -> None:
    global _writer_owner
    with _writer_owner_guard:
        _writer_owner = (scope, phase, threading.get_ident(), time.monotonic())


def _clear_owner() -> None:
    global _writer_owner
    with _writer_owner_guard:
        _writer_owner = None


def _running_event_loop_on_current_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _timeout_error(*, scope: str, phase: str, timeout_s: float) -> GraphLockContention:
    timeout_ms = int(timeout_s * 1000)
    owner = _owner_snapshot()
    owner_scope, owner_phase, owner_thread, held_ms = owner or (
        "none",
        "none",
        0,
        0,
    )
    logger.warning(
        "kg.ladybug_writer.timeout scope=%s phase=%s statement_kind=none "
        "timeout_ms=%d owner_scope=%s owner_phase=%s owner_thread=%d held_ms=%d",
        scope,
        phase,
        timeout_ms,
        owner_scope,
        owner_phase,
        owner_thread,
        held_ms,
        extra={
            "event": "kg.ladybug_writer.timeout",
            "scope": scope,
            "phase": phase,
            "statement_kind": "none",
            "timeout_ms": timeout_ms,
            "owner_scope": owner_scope,
            "owner_phase": owner_phase,
            "owner_thread": owner_thread,
            "held_ms": held_ms,
        },
    )
    return GraphLockContention(
        "ladybug_writer: timed out waiting for the process writer lease",
        details={
            "scope": scope,
            "phase": phase,
            "statement_kind": "none",
            "timeout_ms": timeout_ms,
            "owner_scope": owner_scope,
            "owner_phase": owner_phase,
            "owner_thread": owner_thread,
            "held_ms": held_ms,
            "error_code": GraphLockContention.code,
            "retryable": GraphLockContention.retryable,
        },
    )


@dataclass
class LadybugWriterLease:
    """One releasable process-wide writer lease."""

    scope: str
    phase: str
    wait_ms: int
    _lock: threading.Lock
    _released: bool = False
    _release_guard: threading.Lock = field(default_factory=threading.Lock)

    def release(self) -> None:
        """Release exactly once, including cross-thread hand-off."""

        with self._release_guard:
            if self._released:
                return
            self._released = True
            _clear_owner()
            self._lock.release()
        logger.debug(
            "kg.ladybug_writer.released scope=%s phase=%s "
            "statement_kind=none wait_ms=%d",
            self.scope,
            self.phase,
            self.wait_ms,
            extra={
                "event": "kg.ladybug_writer.released",
                "scope": self.scope,
                "phase": self.phase,
                "statement_kind": "none",
                "wait_ms": self.wait_ms,
            },
        )


def _lease_after_acquire(
    *, scope: str, phase: str, started: float
) -> LadybugWriterLease:
    wait_ms = int((time.monotonic() - started) * 1000)
    _set_owner(scope=scope, phase=phase)
    logger.debug(
        "kg.ladybug_writer.acquired scope=%s phase=%s statement_kind=none wait_ms=%d",
        scope,
        phase,
        wait_ms,
        extra={
            "event": "kg.ladybug_writer.acquired",
            "scope": scope,
            "phase": phase,
            "statement_kind": "none",
            "wait_ms": wait_ms,
        },
    )
    return LadybugWriterLease(
        scope=scope,
        phase=phase,
        wait_ms=wait_ms,
        _lock=_writer_lock,
    )


def acquire_ladybug_writer(
    *,
    scope: str,
    phase: str,
    timeout_s: float = DEFAULT_WRITER_TIMEOUT_S,
) -> LadybugWriterLease:
    """Acquire the process writer lease from a synchronous adapter boundary.

    The production graph transaction factory uses the asynchronous counterpart
    so it never blocks an event loop.  This synchronous entry point preserves
    the legacy/testable ``_KuzuTransactionScope(board_id)`` construction path,
    which is invoked from Core's graph I/O worker thread.
    """

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if writer_lease_is_active():
        raise RuntimeError("nested synchronous Ladybug writer acquisition")

    started = time.monotonic()
    if not _writer_lock.acquire(timeout=timeout_s):
        raise _timeout_error(scope=scope, phase=phase, timeout_s=timeout_s)
    return _lease_after_acquire(scope=scope, phase=phase, started=started)


def _release_cancelled_async_acquire(
    acquire_task: asyncio.Task[bool],
    *,
    writer_lock: threading.Lock,
    scope: str,
    phase: str,
) -> None:
    """Release a lease acquired after its async caller was cancelled.

    This callback deliberately contains no cancellation point.  The protected
    ``to_thread`` acquisition must be allowed to finish, and its result must be
    consumed even when the caller receives multiple cancellation requests.
    """

    try:
        acquired = acquire_task.result()
    except asyncio.CancelledError:
        logger.error(
            "kg.ladybug_writer.cancel_cleanup_cancelled scope=%s phase=%s "
            "statement_kind=none",
            scope,
            phase,
            extra={
                "event": "kg.ladybug_writer.cancel_cleanup_cancelled",
                "scope": scope,
                "phase": phase,
                "statement_kind": "none",
            },
        )
        return
    except Exception as exc:
        logger.error(
            "kg.ladybug_writer.cancel_cleanup_failed scope=%s phase=%s "
            "statement_kind=none error_type=%s",
            scope,
            phase,
            type(exc).__name__,
            extra={
                "event": "kg.ladybug_writer.cancel_cleanup_failed",
                "scope": scope,
                "phase": phase,
                "statement_kind": "none",
                "error_type": type(exc).__name__,
            },
        )
        return

    if not acquired:
        return
    try:
        writer_lock.release()
    except Exception as exc:
        logger.error(
            "kg.ladybug_writer.cancel_release_failed scope=%s phase=%s "
            "statement_kind=none error_type=%s",
            scope,
            phase,
            type(exc).__name__,
            extra={
                "event": "kg.ladybug_writer.cancel_release_failed",
                "scope": scope,
                "phase": phase,
                "statement_kind": "none",
                "error_type": type(exc).__name__,
            },
        )


async def acquire_ladybug_writer_async(
    *,
    scope: str,
    phase: str,
    timeout_s: float = DEFAULT_WRITER_TIMEOUT_S,
) -> LadybugWriterLease:
    """Acquire without blocking an asyncio loop, with cancellation cleanup."""

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if writer_lease_is_active():
        raise RuntimeError("nested async Ladybug writer acquisition")

    started = time.monotonic()
    writer_lock = _writer_lock
    acquire_task = asyncio.create_task(
        asyncio.to_thread(writer_lock.acquire, timeout=timeout_s)
    )
    try:
        acquired = await asyncio.shield(acquire_task)
    except asyncio.CancelledError:
        acquire_task.add_done_callback(
            lambda task: _release_cancelled_async_acquire(
                task,
                writer_lock=writer_lock,
                scope=scope,
                phase=phase,
            )
        )
        logger.info(
            "kg.ladybug_writer.cancelled scope=%s phase=%s statement_kind=none",
            scope,
            phase,
            extra={
                "event": "kg.ladybug_writer.cancelled",
                "scope": scope,
                "phase": phase,
                "statement_kind": "none",
            },
        )
        raise
    if not acquired:
        raise _timeout_error(scope=scope, phase=phase, timeout_s=timeout_s)
    return _lease_after_acquire(scope=scope, phase=phase, started=started)


@contextmanager
def activate_ladybug_writer_lease(
    lease: LadybugWriterLease,
) -> Iterator[None]:
    """Mark a previously acquired lease active for nested adapter calls."""

    token = _writer_lease_active.set(True)
    try:
        yield
    finally:
        _writer_lease_active.reset(token)


@contextmanager
def ladybug_writer_scope(
    *,
    scope: str,
    phase: str,
    timeout_s: float = DEFAULT_WRITER_TIMEOUT_S,
) -> Iterator[LadybugWriterLease | None]:
    """Synchronous, logically re-entrant process writer scope."""

    if writer_lease_is_active():
        yield None
        return
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    started = time.monotonic()
    # Synchronous graph adapters are still called by a few async processors.
    # Waiting on a threading.Lock from their event-loop thread freezes health,
    # MCP cancellation and shutdown. Contention there must be retryable and
    # immediate; worker-thread callers retain the bounded wait.
    on_event_loop = _running_event_loop_on_current_thread()
    acquired = (
        _writer_lock.acquire(blocking=False)
        if on_event_loop
        else _writer_lock.acquire(timeout=timeout_s)
    )
    if not acquired:
        raise _timeout_error(
            scope=scope,
            phase=phase,
            timeout_s=0.0 if on_event_loop else timeout_s,
        )
    lease = _lease_after_acquire(scope=scope, phase=phase, started=started)
    token = _writer_lease_active.set(True)
    try:
        yield lease
    finally:
        _writer_lease_active.reset(token)
        lease.release()


__all__ = [
    "DEFAULT_WRITER_TIMEOUT_S",
    "LadybugWriterLease",
    "acquire_ladybug_writer",
    "acquire_ladybug_writer_async",
    "activate_ladybug_writer_lease",
    "ladybug_writer_scope",
    "writer_lease_is_active",
]
