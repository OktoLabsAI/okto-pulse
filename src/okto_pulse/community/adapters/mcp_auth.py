"""Community adapter for the ``McpAuthenticator`` port (spec R08-A).

``CommunityMcpAuthenticator`` is the Community-edition concrete implementation of
``okto_pulse.core.ports.McpAuthenticator``. It reproduces the current MCP
authentication behaviour by delegating to the canonical
``AgentService.get_agent_by_key`` (SHA-256 of the key -> ``Agent.api_key_hash``
lookup, ``is_active`` filter, ``last_used_at`` touch, ``None`` for absent/invalid
/ inactive) and mapping the resulting ``Agent`` onto the port's canonical
``AgentAuthSession`` DTO.

STRICT scope (R08-A): preserves Community behaviour. It does NOT introduce a
CredentialStore / JWT / realms-scopes / user-auth, does NOT touch
``Agent.api_key`` / ``Agent.api_key_hash`` or ``AgentResponse``, and uses ONLY the
port's canonical DTOs (no parallel DTOs).

Fail-closed: an absent/invalid/inactive credential — or any backend error —
yields ``None``. The raw secret is never logged and never placed in the session
metadata.

Layering (tr_cc34376c): ``core`` never imports ``community``. The adapter imports
``core.ports`` (the contract) at module top and lazy-imports the concrete
``AgentService`` inside ``authenticate``; the composition factory lazy-imports the
session factory.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from okto_pulse.core.ports import (
    AgentAuthSession,
    AuthSession,
    McpCredential,
)

__all__ = ["CommunityMcpAuthenticator", "make_community_mcp_authenticator"]

logger = logging.getLogger("okto_pulse.community.mcp_auth")


class CommunityMcpAuthenticator:
    """``McpAuthenticator`` backed by ``AgentService.get_agent_by_key``.

    ``session_factory`` is an async session factory (e.g. the registered MCP
    session factory or ``get_session_factory()``); ``session_factory()`` opens an
    ``AsyncSession`` context manager.
    """

    def __init__(self, *, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def authenticate(self, credential: McpCredential | None) -> AuthSession | None:
        # Fail-closed: no credential / empty value -> unauthenticated (no raise).
        if credential is None or not getattr(credential, "value", ""):
            return None
        try:
            async with self._session_factory() as db:
                # Lazy import keeps the adapter import-light and avoids any
                # import cycle (core never imports community).
                from okto_pulse.core.services.main import AgentService

                agent = await AgentService(db).get_agent_by_key(credential.value)
                await db.commit()
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

        if agent is None:
            return None
        # get_agent_by_key already filters is_active=True, so a returned agent is
        # active. metadata carries only the (secret-free) transport source.
        return AgentAuthSession(
            agent_id=agent.id,
            agent_name=agent.name,
            is_active=bool(getattr(agent, "is_active", True)),
            metadata={"credential_source": credential.source},
        )


def make_community_mcp_authenticator(
    *,
    session_factory: Callable[[], Any] | None = None,
) -> CommunityMcpAuthenticator:
    """Composition factory — binds the canonical ``McpAuthenticator`` to the
    Community async session factory.

    ``core.infra.database`` is imported HERE (lazily), never at module top, so
    ``core`` never imports ``community``. When ``session_factory`` is omitted, the
    process-global ``get_session_factory()`` is used.
    """
    if session_factory is None:
        from okto_pulse.core.infra.database import get_session_factory

        session_factory = get_session_factory()
    return CommunityMcpAuthenticator(session_factory=session_factory)
