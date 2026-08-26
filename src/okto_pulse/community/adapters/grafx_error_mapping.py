"""Translate Okto Grafx failures into the backend-neutral Core taxonomy."""

from __future__ import annotations

from okto_grafx.errors import (
    GrafxBufferBudgetExceeded,
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxEmbeddingSpaceMismatch,
    GrafxError,
    GrafxIndexError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxPortNotConfigured,
    GrafxQueryBudgetExceeded,
    GrafxRecoveryRefused,
    GrafxSchemaVersionMismatch,
    GrafxSpaceRetired,
    GrafxStaleEpoch,
    GrafxStorageError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxVectorValidationError,
    GrafxWriteConflict,
)
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphError,
    GraphIndexUnavailable,
    GraphLockContention,
    GraphUnavailable,
)

from okto_pulse.community.adapters.graph_memory_pressure import GraphMemoryPressure

_CONTENTION_FAILURES = (
    GrafxWriteConflict,
    GrafxLeaseTimeout,
    GrafxLeaseStolen,
    GrafxStaleEpoch,
)
_CORRUPTION_FAILURES = (GrafxCorruptionDetected,)
_INDEX_FAILURES = (
    GrafxIndexError,
    GrafxEmbeddingSpaceMismatch,
    GrafxSpaceRetired,
    GrafxVectorValidationError,
)
_CAPABILITY_FAILURES = (
    GrafxConfigurationError,
    GrafxPortNotConfigured,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
_MEMORY_FAILURES = (GrafxBufferBudgetExceeded,)
_UNAVAILABLE_FAILURES = (
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxQueryBudgetExceeded,
    GrafxRecoveryRefused,
    GrafxStorageError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
)


def _preserve_retryability(mapped: GraphError, exc: BaseException) -> GraphError:
    """Keep the source retry policy even when the Core class default differs."""

    if isinstance(exc, GrafxError):
        mapped.retryable = bool(exc.retryable)
    return mapped


def map_grafx_error(exc: BaseException, *, operation: str) -> GraphError:
    """Return a stable Core error without message-pattern classification."""

    if isinstance(exc, GraphError):
        return exc

    details: dict[str, object] = {
        "backend": "okto_grafx",
        "operation": operation,
        "backend_error_type": type(exc).__name__,
    }
    if isinstance(exc, GrafxError):
        details.update(
            {
                "backend_error_code": exc.code,
                "backend_retryable": exc.retryable,
            }
        )
        message = f"{operation} failed in Okto Grafx ({exc.code})."
    else:
        message = f"{operation} failed in Okto Grafx ({type(exc).__name__})."

    if isinstance(exc, _CONTENTION_FAILURES):
        mapped = GraphLockContention(message, details=details)
    elif isinstance(exc, _CORRUPTION_FAILURES):
        mapped = GraphCorruption(message, details=details)
    elif isinstance(exc, _INDEX_FAILURES):
        mapped = GraphIndexUnavailable(message, details=details)
    elif isinstance(exc, _CAPABILITY_FAILURES):
        mapped = GraphCapabilityUnavailable(message, details=details)
    elif isinstance(exc, _MEMORY_FAILURES):
        mapped = GraphMemoryPressure(message, details=details)
    elif isinstance(exc, _UNAVAILABLE_FAILURES):
        mapped = GraphUnavailable(message, details=details)
    else:
        mapped = GraphError(message, details=details)
    return _preserve_retryability(mapped, exc)


__all__ = ["map_grafx_error"]
