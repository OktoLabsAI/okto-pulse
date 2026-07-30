"""Local-first authentication adapter for the Community edition."""

from copy import deepcopy

from okto_pulse.core.ports import (
    AuthenticationPort,
    Credential,
    Principal,
)
from okto_pulse.core.domain.permissions import PERMISSION_REGISTRY
from okto_pulse.core.domain.realm import LOCAL_REALM_ID

LOCAL_USER = {
    "sub": "local-user",
    "email": "local@okto-pulse.dev",
    "name": "Local User",
    "roles": ["admin"],
    # Community is a single-user local edition. Its server-owned principal is
    # the canonical Full Control preset, including forward-propagated SK-B
    # leaves; transports must not invent or maintain a parallel capability list.
    "permissions": deepcopy(PERMISSION_REGISTRY),
}

class LocalAuthProvider(AuthenticationPort):
    """Always resolves the Community single-user principal.

    The local-first policy intentionally accepts an absent credential; HTTP and
    MCP extraction are handled by their own inbound adapters.
    """

    async def authenticate(self, credential: Credential | None) -> Principal:
        del credential
        return Principal(
            subject="local-user",
            realm_id=LOCAL_REALM_ID,
            # A principal may be enriched by an inbound adapter.  Never expose
            # the process-global Full Control template by reference.
            claims=deepcopy(LOCAL_USER),
        )
