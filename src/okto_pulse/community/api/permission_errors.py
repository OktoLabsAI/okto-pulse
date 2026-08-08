"""REST projection for application permission denials."""

import json
from typing import Any

from fastapi import HTTPException, status

from okto_pulse.core.application.use_cases import PermissionDeniedError


def _permission_detail(message: str) -> Any:
    """Keep structured permission metadata when Core encoded it as JSON."""
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        return message


def permission_denied_http_error(exc: PermissionDeniedError) -> HTTPException:
    """Project a transport-neutral permission denial to the REST contract."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=_permission_detail(exc.message),
    )
