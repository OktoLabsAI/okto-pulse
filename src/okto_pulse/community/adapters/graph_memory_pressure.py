"""Typed memory-pressure failures for the embedded Community graph runtime.

Ladybug exposes allocation failures through a mix of Python ``MemoryError``
instances and backend strings (for example ``std::bad_alloc``).  Keep the
classification in the adapter: callers only need the backend-neutral
``GraphUnavailable`` contract while operators get a stable, retryable reason
that is explicitly *not* corruption.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from okto_pulse.core.kg.interfaces.graph_errors import GraphUnavailable


logger = logging.getLogger("okto_pulse.kg.memory_pressure")

GRAPH_OPEN_MEMORY_COOLDOWN_S = 60.0

# Ladybug's native allocator is process-wide.  Board and Global Discovery
# Database constructors therefore share both the serialization gate and the
# allocation-pressure memory.  Keeping this state here prevents either
# adapter from bypassing the other adapter's cooldown.
_GRAPH_DATABASE_OPEN_GATE = threading.RLock()
_graph_database_open_latch: tuple[float, Path, str, str] | None = None

_T = TypeVar("_T")


MEMORY_PRESSURE_MARKERS: tuple[str, ...] = (
    "bad allocation",
    "std::bad_alloc",
    "memoryerror",
    "cannot allocate memory",
    "unable to allocate memory",
    "out of memory",
    "no more frame groups can be added to the allocator",
)


class GraphMemoryPressure(GraphUnavailable):
    """Retryable graph unavailability caused by native allocation pressure."""

    code = "graph_memory_pressure"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        normalized_details = dict(details or {})
        normalized_details.update(
            {
                "error_code": self.code,
                "reason_code": self.code,
                "retryable": True,
                "corruption": False,
            }
        )
        super().__init__(message, details=normalized_details)


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_graph_memory_pressure_error(exc: BaseException) -> bool:
    """Recognize native/Python allocation failures without marking corruption."""

    for item in _exception_chain(exc):
        if isinstance(item, (MemoryError, GraphMemoryPressure)):
            return True
        normalized = f"{type(item).__name__}: {item}".lower()
        if any(marker in normalized for marker in MEMORY_PRESSURE_MARKERS):
            return True
    return False


def reset_graph_open_memory_circuit_for_tests() -> None:
    """Clear the process-wide constructor breaker without touching storage."""

    global _graph_database_open_latch

    with _GRAPH_DATABASE_OPEN_GATE:
        _graph_database_open_latch = None


def raise_if_graph_open_memory_cooldown(
    *,
    path: Path,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> None:
    """Fail fast while any Ladybug Database open is cooling down."""

    global _graph_database_open_latch

    resolved = path.resolve()
    now = monotonic_clock()
    with _GRAPH_DATABASE_OPEN_GATE:
        latch = _graph_database_open_latch
        if latch is None:
            return
        retry_at, failed_path, failure, failed_scope = latch
        remaining_s = retry_at - now
        if remaining_s <= 0:
            _graph_database_open_latch = None
            return
    raise GraphMemoryPressure(
        "LadybugDB open is cooling down after native allocation pressure",
        details={
            "cooldown_active": True,
            "retry_after_ms": max(1, int(remaining_s * 1000)),
            "path": str(resolved),
            "failed_path": str(failed_path),
            "failed_scope": failed_scope,
            "last_failure": failure,
        },
    )


def record_graph_open_memory_failure(
    *,
    path: Path,
    exc: BaseException,
    scope: str,
    cooldown_s: float = GRAPH_OPEN_MEMORY_COOLDOWN_S,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> None:
    """Arm the shared breaker when a native Database allocation fails."""

    global _graph_database_open_latch

    if not is_graph_memory_pressure_error(exc):
        return
    if cooldown_s <= 0:
        raise ValueError("cooldown_s must be positive")
    resolved = path.resolve()
    retry_at = monotonic_clock() + cooldown_s
    with _GRAPH_DATABASE_OPEN_GATE:
        _graph_database_open_latch = (
            retry_at,
            resolved,
            str(exc),
            scope,
        )
    logger.warning(
        "kg.memory_open_cooldown scope=%s path=%s cooldown_ms=%d",
        scope,
        resolved,
        int(cooldown_s * 1000),
        extra={
            "event": "kg.memory_open_cooldown",
            "scope": scope,
            "path": str(resolved),
            "cooldown_ms": int(cooldown_s * 1000),
            "reason_code": GraphMemoryPressure.code,
        },
    )


def clear_graph_open_memory_failure() -> None:
    """Clear an expired/previous allocation failure after a successful open."""

    global _graph_database_open_latch

    with _GRAPH_DATABASE_OPEN_GATE:
        _graph_database_open_latch = None


def run_graph_database_open(
    *,
    path: Path,
    opener: Callable[[], _T],
    scope: str,
    cooldown_s: float = GRAPH_OPEN_MEMORY_COOLDOWN_S,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> _T:
    """Serialize a native Database constructor and share its OOM breaker.

    The lock deliberately spans the constructor call.  Ladybug can reserve
    large native/virtual-memory regions while constructing a ``Database``;
    allowing a board and Global Discovery to do that concurrently is enough
    to make the native allocator abort before Python can recover.
    """

    if cooldown_s <= 0:
        raise ValueError("cooldown_s must be positive")
    with _GRAPH_DATABASE_OPEN_GATE:
        raise_if_graph_open_memory_cooldown(
            path=path,
            monotonic_clock=monotonic_clock,
        )
        try:
            opened = opener()
        except BaseException as exc:
            record_graph_open_memory_failure(
                path=path,
                exc=exc,
                scope=scope,
                cooldown_s=cooldown_s,
                monotonic_clock=monotonic_clock,
            )
            raise
        clear_graph_open_memory_failure()
        return opened


__all__ = [
    "GRAPH_OPEN_MEMORY_COOLDOWN_S",
    "GraphMemoryPressure",
    "MEMORY_PRESSURE_MARKERS",
    "clear_graph_open_memory_failure",
    "is_graph_memory_pressure_error",
    "raise_if_graph_open_memory_cooldown",
    "record_graph_open_memory_failure",
    "reset_graph_open_memory_circuit_for_tests",
    "run_graph_database_open",
]
