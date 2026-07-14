"""Community adapters for the MCP auth ports (spec R08-A/R06).

``CommunityMcpAuthenticator`` is the Community-edition concrete implementation of
``okto_pulse.core.ports.McpAuthenticator``. It reproduces the current MCP
authentication behaviour by delegating to the canonical
``application_agents.authenticate_agent_by_api_key`` (SHA-256 of the key ->
``Agent.api_key_hash`` lookup, ``is_active`` filter, read-only auth with no
``last_used_at`` touch,
``None`` for absent/invalid/inactive) and returning the port's canonical
``AgentAuthSession`` DTO.

R06 also moves the concrete KG ``AuthContext`` bridge here from core. The bridge
still delegates board ACL to the public ``application_agents`` facade so
envelopes and ACL semantics remain unchanged, but the concrete runtime ownership
is now Community; core keeps only contracts and fail-closed consumers.

STRICT scope (R08-A): preserves Community behaviour. It does NOT introduce a
CredentialStore / JWT / realms-scopes / user-auth, does NOT touch
``Agent.api_key`` / ``Agent.api_key_hash`` or ``AgentResponse``, and uses ONLY the
port's canonical DTOs (no parallel DTOs).

Fail-closed: an absent/invalid/inactive credential — or any backend error —
yields ``None``. The raw secret is never logged and never placed in the session
metadata.

Layering (tr_cc34376c): ``core`` never imports ``community``. The adapter imports
``core.ports`` (the contract) at module top and lazy-imports public core
facades; the composition factory lazy-imports the public relational runtime
facade.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable

from okto_pulse.core.ports import (
    AuthSession,
    McpCredential,
    principal_from_auth_session,
)
from okto_pulse.core.domain.realm import RealmScope, require_realm_scope

__all__ = [
    "CommunityMcpAuthenticator",
    "MCPAuthContext",
    "CommunityMCPAuthContext",
    "auth_context_from_session",
    "create_mcp_auth_factory",
    "make_community_mcp_authenticator",
    "principal_from_auth_session",
]

logger = logging.getLogger("okto_pulse.community.mcp_auth")


class CommunityMcpAuthenticator:
    """``McpAuthenticator`` backed by the public application agent facade.

    ``session_factory`` is an async session factory (e.g. the registered MCP
    session factory or ``get_session_factory()``); ``session_factory()`` opens an
    ``AsyncSession`` context manager.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        session_scope_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session_scope_factory = session_scope_factory or session_factory

    async def authenticate(self, credential: McpCredential | None) -> AuthSession | None:
        # Fail-closed: no credential / empty value -> unauthenticated (no raise).
        if credential is None or not getattr(credential, "value", ""):
            return None
        try:
            async with self._session_scope_factory() as db:
                from okto_pulse.core.services.application_agents import (
                    authenticate_agent_by_api_key,
                )

                session = await authenticate_agent_by_api_key(
                    db,
                    credential.value,
                    credential_source=credential.source,
                )
        except Exception:  # noqa: BLE001 — fail-closed; never leak the raw secret
            # Secret-free: log the source only, never credential.value.
            logger.warning(
                "mcp_auth.backend_error source=%s",
                getattr(credential, "source", "unknown"),
                extra={
                    "event": "mcp_auth.backend_error",
                    "credential_source": getattr(credential, "source", "unknown"),
                },
            )
            return None

        return session


def make_community_mcp_authenticator(
    *,
    session_factory: Callable[[], Any] | None = None,
    session_scope_factory: Callable[[], Any] | None = None,
) -> CommunityMcpAuthenticator:
    """Composition factory — binds the canonical ``McpAuthenticator`` to the
    Community async session factory.

    The public relational runtime facade is imported HERE (lazily), never at
    module top, so ``core`` never imports ``community``. When ``session_factory``
    is omitted, the process-global ``get_session_factory()`` is used.
    """
    if session_factory is None:
        from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory

        session_factory = get_session_factory()
    return CommunityMcpAuthenticator(
        session_factory=session_factory,
        session_scope_factory=session_scope_factory,
    )


class MCPAuthContext:
    """Community-owned KG AuthContext bridge for MCP request identity."""

    def __init__(
        self,
        get_agent: Callable,
        get_db: Callable,
        *,
        realm_scope: RealmScope | None = None,
        session_scope_factory: Callable[[], Any] | None = None,
    ):
        self._get_agent = get_agent
        self._get_db = get_db
        self._session_scope_factory = session_scope_factory or get_db
        self._realm_scope = require_realm_scope(realm_scope or RealmScope.local())
        self._agent: Any = _UNSET
        self._boards: list[str] | None = None

    async def _resolve_agent(self):
        if self._agent is _UNSET:
            self._agent = await self._get_agent()
        return self._agent

    async def get_agent_id(self) -> str | None:
        agent = await self._resolve_agent()
        return agent.id if agent else None

    async def get_accessible_boards(self) -> list[str]:
        if self._boards is not None:
            return self._boards
        agent = await self._resolve_agent()
        if agent is None:
            self._boards = []
            return self._boards
        async with self._session_scope_factory() as db:
            from okto_pulse.core.services.application_agents import (
                list_accessible_board_ids_for_agent,
            )

            self._boards = await list_accessible_board_ids_for_agent(db, agent.id)
        return self._boards

    async def get_realm_scope(self) -> RealmScope:
        return self._realm_scope

    def has_admin_role(self) -> bool:
        return False


CommunityMCPAuthContext = MCPAuthContext
_UNSET = object()


def create_mcp_auth_factory(
    get_agent: Callable,
    get_db: Callable,
    *,
    realm_scope: RealmScope | None = None,
    session_scope_factory: Callable[[], Any] | None = None,
) -> Callable:
    """Build an auth_context_factory for the MCP server bootstrap."""

    def factory() -> MCPAuthContext:
        return MCPAuthContext(
            get_agent,
            get_db,
            realm_scope=realm_scope or RealmScope.local(),
            session_scope_factory=session_scope_factory,
        )

    return factory


def auth_context_from_session(
    session: AuthSession | None,
    get_db: Callable,
    *,
    session_scope_factory: Callable[[], Any] | None = None,
) -> MCPAuthContext:
    """Bridge a resolved auth session to the Community-owned KG AuthContext."""

    async def _get_agent():
        if session is None or not getattr(session, "is_active", False):
            return None
        return SimpleNamespace(id=session.agent_id)

    metadata = dict(getattr(session, "metadata", {}) or {}) if session else {}
    realm_id = str(metadata.get("realm_id") or "local")
    realm_scope = (
        RealmScope.local()
        if realm_id == "local"
        else RealmScope.tenant(realm_id)
    )
    return MCPAuthContext(
        _get_agent,
        get_db,
        realm_scope=realm_scope,
        session_scope_factory=session_scope_factory,
    )
