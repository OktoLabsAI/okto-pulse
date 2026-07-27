"""Translate embedded graph-driver failures into Core semantic errors."""

from __future__ import annotations

from typing import NoReturn

from okto_pulse.community.adapters.graph_memory_pressure import (
    GraphMemoryPressure,
    MEMORY_PRESSURE_MARKERS,
    is_graph_memory_pressure_error,
)
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCorruption,
    GraphError,
    GraphIndexUnavailable,
    GraphLockContention,
    GraphUnavailable,
)

_LOCK_MARKERS = (
    "could not set lock on file",
    "database is locked",
    "only one write transaction",
)
_CORRUPTION_MARKERS = (
    "checksum verification failed",
    "corrupted wal file",
    "wal file is corrupted",
    "invalid wal record",
    "wal_record.cpp",
    "not a valid lbug database file",
    "unreachable_code",
)
_INDEX_MARKERS = (
    "query_vector_index",
    "index does not exist",
    "no such index",
    "cannot set property",
    "used in one or more indexes",
)
_UNAVAILABLE_MARKERS = (
    "failed to open",
    "could not open",
    "connection is closed",
    "database is closed",
)
_MEMORY_MARKERS = MEMORY_PRESSURE_MARKERS + (
    "buffer manager",
    "buffer pool",
    "power of 2",
    "power-of-2",
)


def _operator_details(
    normalized: str,
    *,
    memory_pressure: bool = False,
    operation: str = "",
) -> dict[str, object]:
    if not memory_pressure and not any(
        marker in normalized for marker in _MEMORY_MARKERS
    ):
        return {}
    is_global = "global" in operation.lower()
    try:
        from okto_pulse.core import get_settings
        from okto_pulse.community.adapters import kg_runtime

        settings = get_settings()
        configured_buffer_pool_mb = int(
            getattr(settings, "kg_global_kuzu_buffer_pool_mb", 128)
            if is_global
            else settings.kg_kuzu_buffer_pool_mb
        )
        buffer_pool_mb = (
            configured_buffer_pool_mb
            if is_global
            else min(
                configured_buffer_pool_mb,
                kg_runtime._board_buffer_pool_operational_cap_mb(),
            )
        )
        configured_max_db_size_gb = int(settings.kg_kuzu_max_db_size_gb)
        max_db_size_gb, _max_db_cap_gb = (
            kg_runtime._effective_graph_max_db_size_gb(
                configured_max_db_size_gb
            )
        )
    except Exception:
        configured_buffer_pool_mb = 128 if is_global else 256
        buffer_pool_mb = configured_buffer_pool_mb
        configured_max_db_size_gb = 2
        max_db_size_gb = 2
    if "power of 2" in normalized or "power-of-2" in normalized:
        remediation = (
            "Set the local graph max database size to one of "
            "2, 4, 8, 16, 32 or 64 GB and restart."
        )
    else:
        remediation = (
            "Reduce the local graph buffer pool and simultaneous open-board "
            "cache, restart, then retry. Do not rebuild or quarantine the graph "
            "solely because of an allocation failure."
        )
    return {
        "graph_buffer_pool_mb": buffer_pool_mb,
        "graph_buffer_pool_configured_mb": configured_buffer_pool_mb,
        "graph_buffer_pool_effective_mb": buffer_pool_mb,
        "graph_buffer_scope": "global_discovery" if is_global else "board",
        "graph_max_db_size_gb": max_db_size_gb,
        "graph_max_db_size_configured_gb": configured_max_db_size_gb,
        "graph_max_db_size_effective_gb": max_db_size_gb,
        "remediation": remediation,
    }


def map_graph_error(exc: BaseException, *, operation: str) -> GraphError:
    if isinstance(exc, GraphError):
        return exc
    message = str(exc)
    normalized = message.lower()
    detail = f"{operation}: {message}"
    memory_pressure = is_graph_memory_pressure_error(exc)
    operator_details = _operator_details(
        normalized,
        memory_pressure=memory_pressure,
        operation=operation,
    )
    if memory_pressure:
        operator_details.update(
            {
                "error_code": GraphMemoryPressure.code,
                "reason_code": GraphMemoryPressure.code,
                "retryable": True,
                "corruption": False,
            }
        )
        return GraphMemoryPressure(detail, details=operator_details)
    if any(marker in normalized for marker in _LOCK_MARKERS):
        return GraphLockContention(detail, details=operator_details)
    if any(marker in normalized for marker in _CORRUPTION_MARKERS):
        return GraphCorruption(detail, details=operator_details)
    if any(marker in normalized for marker in _INDEX_MARKERS):
        return GraphIndexUnavailable(detail, details=operator_details)
    if any(marker in normalized for marker in _UNAVAILABLE_MARKERS):
        return GraphUnavailable(detail, details=operator_details)
    return GraphError(detail, details=operator_details)


def raise_mapped_graph_error(exc: BaseException, *, operation: str) -> NoReturn:
    raise map_graph_error(exc, operation=operation) from exc


__all__ = [
    "GraphMemoryPressure",
    "map_graph_error",
    "raise_mapped_graph_error",
]
