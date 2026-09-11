"""Backend-neutral error classification, owned by the Community boundary.

No constructor mutex or process-wide native-engine allocation breaker is used.
"""
from __future__ import annotations
from typing import Iterator
from okto_pulse.core.kg.interfaces.graph_errors import GraphUnavailable

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
