"""REST adapter for the Core-owned Spec dependency error projection."""

from fastapi import HTTPException

from okto_pulse.core.application.use_cases import PermissionDeniedError
from okto_pulse.core.domain.spec_dependency import SpecDependencyOperationError
from okto_pulse.core.inbound.spec_dependency_error import (
    project_spec_dependency_error,
    spec_dependency_http_status,
)


def spec_dependency_http_error(
    error: SpecDependencyOperationError,
) -> HTTPException:
    """Return the canonical bounded SK-M envelope with its public HTTP status."""

    return HTTPException(
        status_code=spec_dependency_http_status(error),
        detail=project_spec_dependency_error(error),
    )


def spec_dependency_permission_denied_http_error(
    _error: PermissionDeniedError,
) -> HTTPException:
    """Do not echo authorization internals through the SK-M transport."""

    return HTTPException(
        status_code=403,
        detail={
            "code": "permission_denied",
            "message": "Permission denied for this Spec dependency operation.",
            "retryable": False,
        },
    )


__all__ = [
    "spec_dependency_http_error",
    "spec_dependency_permission_denied_http_error",
]
