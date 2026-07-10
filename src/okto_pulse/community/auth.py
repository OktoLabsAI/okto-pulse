"""Local-first authentication adapter for the Community edition."""

from okto_pulse.core.ports import (
    AuthenticationPort,
    Credential,
    Principal,
)

LOCAL_USER = {
    "sub": "local-user",
    "email": "local@okto-pulse.dev",
    "name": "Local User",
    "roles": ["admin"],
}

class LocalAuthProvider(AuthenticationPort):
    """Always resolves the Community single-user principal.

    The local-first policy intentionally accepts an absent credential; HTTP and
    MCP extraction are handled by their own inbound adapters.
    """

    async def authenticate(self, credential: Credential | None) -> Principal:
        del credential
        return Principal(subject="local-user", realm_id=None, claims=LOCAL_USER)
