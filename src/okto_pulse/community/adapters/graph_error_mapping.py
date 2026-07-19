"""Translate embedded graph-driver failures into Core semantic errors."""

from __future__ import annotations

from typing import NoReturn

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
_MEMORY_MARKERS = (
    "buffer manager",
    "buffer pool",
    "unable to allocate memory",
    "power of 2",
    "power-of-2",
)


def _operator_details(normalized: str) -> dict[str, object]:
    if not any(marker in normalized for marker in _MEMORY_MARKERS):
        return {}
    try:
        from okto_pulse.core import get_settings

        settings = get_settings()
        buffer_pool_mb = int(settings.kg_kuzu_buffer_pool_mb)
        max_db_size_gb = int(settings.kg_kuzu_max_db_size_gb)
    except Exception:
        buffer_pool_mb = 512
        max_db_size_gb = 2
    if "power of 2" in normalized or "power-of-2" in normalized:
        remediation = (
            "Set the local graph max database size to one of "
            "2, 4, 8, 16, 32 or 64 GB and restart."
        )
    else:
        remediation = (
            "Set the local graph buffer pool to 512 MB, restart, retry the "
            "consolidation, then reprocess dead letters."
        )
    return {
        "graph_buffer_pool_mb": buffer_pool_mb,
        "graph_max_db_size_gb": max_db_size_gb,
        "remediation": remediation,
    }


def map_graph_error(exc: BaseException, *, operation: str) -> GraphError:
    if isinstance(exc, GraphError):
        return exc
    message = str(exc)
    normalized = message.lower()
    detail = f"{operation}: {message}"
    operator_details = _operator_details(normalized)
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


__all__ = ["map_graph_error", "raise_mapped_graph_error"]
