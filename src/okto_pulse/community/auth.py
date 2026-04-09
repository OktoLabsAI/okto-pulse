"""Local authentication provider for community edition — single-user, no JWT."""

from typing import Any
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from okto_pulse.core.infra.auth import AuthProvider

LOCAL_USER = {
    "sub": "local-user",
    "email": "local@okto-pulse.dev",
    "name": "Local User",
}

class LocalAuthProvider(AuthProvider):
    """Always returns a fixed local user. No JWT validation."""

    async def get_current_user(self, request: Request, credentials: HTTPAuthorizationCredentials | None) -> dict[str, Any]:
        return LOCAL_USER

    def get_user_id(self, user: dict[str, Any] | None) -> str:
        return "local-user"

    async def get_realm_id(self, request: Request, user: dict[str, Any] | None) -> str | None:
        return None
