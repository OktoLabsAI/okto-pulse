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
    GrafxParseError,
    GrafxPlanError,
    GrafxPortNotConfigured,
    GrafxQueryBudgetExceeded,
    GrafxQueryDeadlineExceeded,
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
    GraphInvalidQuery,
    GraphLockContention,
    GraphQueryTimeout,
    GraphQueryResourceLimit,
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
    GrafxRecoveryRefused,
    GrafxStorageError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
)


def _query_limit_details(exc: BaseException) -> dict[str, object] | None:
    resources = {
        'max_result_rows': 'result_rows',
        'max_intermediate_rows': 'intermediate_rows',
        'max_traversal_expansions': 'traversal_expansions',
        'max_traversal_paths': 'traversal_paths',
        'query_memory_budget_bytes': 'query_memory_bytes',
    }
    source = exc
    resource = None
    if isinstance(source, GrafxQueryBudgetExceeded):
        resource = resources.get(source.details.get('field'), 'native_query_budget')
    elif isinstance(source, GrafxPlanError) and isinstance(source.__cause__, GrafxConfigurationError):
        source = source.__cause__
        field = source.details.get('field')
        if type(field) is str and field.startswith('query.result.'):
            # Grafx wraps bounded output-value refusals in a plan error. Keep
            # the typed limit without parsing messages or disclosing result data.
            resource = 'result_value'
    if resource is None:
        return None
    limit = source.details.get('limit')
    observed = source.details.get('observed', source.details.get('value'))
    if type(limit) is not int or limit < 1 or type(observed) is not int or observed <= limit:
        return None
    return {'resource': resource, 'limit': limit, 'observed': observed}


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

    limit_details = _query_limit_details(exc)
    if limit_details is not None:
        return GraphQueryResourceLimit('Query exceeded an explicit resource limit.', details=limit_details)
    if isinstance(exc, GrafxQueryBudgetExceeded):
        # A native typed refusal must not be treated as a transient outage,
        # even when an engine version omits numeric detail.
        return GraphQueryResourceLimit('Query exceeded a native resource limit.',
                                       details={'resource': 'native_query_budget'})
    if isinstance(exc, GrafxQueryDeadlineExceeded):
        mapped = GraphQueryTimeout(message, details=details)
    elif isinstance(exc, (GrafxParseError, GrafxPlanError)):
        mapped = GraphInvalidQuery(message, details=details)
    elif isinstance(exc, _CONTENTION_FAILURES):
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
