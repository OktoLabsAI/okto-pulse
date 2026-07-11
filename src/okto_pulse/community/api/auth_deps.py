"""FastAPI authentication dependencies for the REST inbound adapter.

Credential extraction and HTTP error rendering belong at this edge.  The
registered edition provider receives only the pure Core ``Credential`` and
returns a pure ``Principal``.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from okto_pulse.core.infra.auth import get_auth_provider
from okto_pulse.core.ports.authentication import (
    AuthenticationError,
    AuthorizationDenied,
    Credential,
    InvalidCredential,
    MissingCredential,
    Principal,
)

security = HTTPBearer(auto_error=False)


def _as_http_error(error: AuthenticationError) -> HTTPException:
    if isinstance(error, AuthorizationDenied):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized",
        )
    if isinstance(error, (MissingCredential, InvalidCredential)):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication failed",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _credential_from_http(
    credentials: HTTPAuthorizationCredentials | None,
) -> Credential | None:
    if credentials is None or not credentials.credentials:
        return None
    return Credential(
        value=credentials.credentials,
        source="http_bearer",
        metadata={"scheme": credentials.scheme},
    )


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> Principal:
    """Resolve a normalized principal through the registered edition port."""
    credential = _credential_from_http(credentials)
    try:
        principal = await get_auth_provider().authenticate(credential)
    except AuthenticationError as error:
        raise _as_http_error(error) from error

    if principal is None:
        error = MissingCredential() if credential is None else InvalidCredential()
        raise _as_http_error(error)
    return principal


async def get_current_user(
    principal: Principal = Depends(get_current_principal),
) -> dict[str, object]:
    """Project the normalized principal to the historical REST user mapping."""
    return principal.legacy_user()


async def get_current_user_id(
    principal: Principal = Depends(get_current_principal),
) -> str:
    """Return the stable application actor identifier."""
    return principal.subject


def require_principal(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """Dependency marker for endpoints that require an authenticated principal."""
    return principal


def require_user(user_id: str = Depends(get_current_user_id)) -> str:
    """Backward-compatible actor dependency used by REST handlers."""
    return user_id


async def get_realm_id(
    principal: Principal = Depends(get_current_principal),
) -> str | None:
    """Return the optional tenant/realm selected by the authentication port."""
    return principal.realm_id


__all__ = [
    "get_current_principal",
    "get_current_user",
    "get_current_user_id",
    "get_realm_id",
    "require_principal",
    "require_user",
    "security",
]
