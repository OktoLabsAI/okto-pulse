"""Community SQLAlchemy UnitOfWork adapter + factory (R01B REPLAN-IMP1).

Mirrors the core ``okto_pulse.core.repositories.sqlalchemy.unit_of_work``
concretes that implement the ``PulseUnitOfWork`` / ``UnitOfWorkFactory`` PORTS
(``okto_pulse.core.repositories.interfaces.unit_of_work``).

``CommunityUnitOfWork`` wraps an ``AsyncSession`` by composition, owns the
transaction boundary (commit/rollback/close) and exposes the repository catalog
(boards/ideations/specs). It preserves the core teardown invariant EXACTLY:
``__aexit__`` rolls back ONLY on error and ALWAYS closes the session in a
``finally``, returning ``None`` so it never suppresses an exception. The same
path is reached whether the consumer enters via the factory or via
``async with uow:`` directly (one teardown path, no connection leak).

``session`` is the transitional bridge the spec #09 use cases still delegate to
(``session_of``); it is preserved here for byte-parity and removed when those
flows migrate to the repositories.

``CommunityUnitOfWorkFactory`` is realm-ready: ``realm_id``/``actor`` are accepted
and carried but NO realm filter/enforcement is applied this phase (fr_cbfcb1aa) —
identical to the core factory.

Additive + register-before-remove: nothing in ``core`` imports this module
(direction core-contracts -> Community-adapters preserved, TR4). The Community
composition root registers ``build_community_unit_of_work_factory(...)`` as the
``uow_factory`` provider; re-pointing the REST/MCP consumers to it is IMP2 (FR3).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_repositories import (
    CommunityBoardRepository,
    CommunityIdeationRepository,
    CommunitySpecRepository,
)

if TYPE_CHECKING:
    from okto_pulse.core.application.use_cases.base import ActorContext


class CommunityUnitOfWork:
    """PulseUnitOfWork backed by a SQLAlchemy AsyncSession (Community)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        realm_id: str | None = None,
        actor: "ActorContext | None" = None,
    ) -> None:
        self._session = session
        # realm-ready, NOT enforced this phase (fr_cbfcb1aa).
        self.realm_id = realm_id
        self.actor = actor
        self.boards = CommunityBoardRepository(session)
        self.ideations = CommunityIdeationRepository(session)
        self.specs = CommunitySpecRepository(session)

    @property
    def session(self) -> AsyncSession:
        """Transitional bridge: the spec #09 use cases still delegate to services
        via a session (``session_of``). Removed when those flows migrate to the
        repositories."""
        return self._session

    async def __aenter__(self) -> "CommunityUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Single, entry-style-independent teardown: roll back on error and ALWAYS
        # close the session. The factory context delegates here, and a direct
        # `async with uow:` reaches the same path — so neither style leaks the
        # connection (the port docstring advertises both).
        try:
            if exc is not None:
                await self.rollback()
        finally:
            await self.close()
        return None

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def close(self) -> None:
        await self._session.close()


class _CommunityUnitOfWorkContext:
    """Async context manager that creates a session + UoW and delegates teardown
    to the UoW, so the rollback/close path is identical whether the consumer
    enters via the factory or via ``async with uow:`` directly (one path)."""

    def __init__(
        self,
        session_factory: Any,
        *,
        realm_id: str | None,
        actor: "ActorContext | None",
    ) -> None:
        self._session_factory = session_factory
        self._realm_id = realm_id
        self._actor = actor
        self._uow: CommunityUnitOfWork | None = None

    async def __aenter__(self) -> CommunityUnitOfWork:
        session = self._session_factory()
        self._uow = CommunityUnitOfWork(
            session, realm_id=self._realm_id, actor=self._actor
        )
        return self._uow

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._uow is not None:
            await self._uow.__aexit__(exc_type, exc, tb)
        return None


class CommunityUnitOfWorkFactory:
    """UnitOfWorkFactory producing SQLAlchemy-backed units of work (Community)."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def __call__(
        self,
        *,
        realm_id: str | None = None,
        actor: "ActorContext | None" = None,
    ) -> AbstractAsyncContextManager["CommunityUnitOfWork"]:
        return _CommunityUnitOfWorkContext(
            self._session_factory, realm_id=realm_id, actor=actor
        )

    def wrap(
        self,
        session: AsyncSession,
        *,
        realm_id: str | None = None,
        actor: "ActorContext | None" = None,
    ) -> "CommunityUnitOfWork":
        """Request-scoped bridge (R01B FR3): wrap an EXTERNALLY-owned session
        (the REST ``Depends(get_db)`` session) in a unit of work WITHOUT taking
        over its lifecycle. The caller (``get_db``) still closes the session; the
        returned UoW is used as a plain object (the use case commits/rolls back),
        NOT entered as an ``async with`` context. Byte-for-byte the same
        request-scoped semantics the core ``SQLAlchemyUnitOfWork(db)`` had."""
        return CommunityUnitOfWork(session, realm_id=realm_id, actor=actor)


def build_community_unit_of_work_factory(
    session_factory: Any,
) -> CommunityUnitOfWorkFactory:
    """Build the Community ``UnitOfWorkFactory`` provider from a session factory.

    The composition root passes ``get_session_factory()`` — the SAME live factory
    the REST + MCP listeners share — so the provider is registered/observable and
    bound to real connections (DORMANT, not a dead object)."""
    return CommunityUnitOfWorkFactory(session_factory)


__all__ = [
    "CommunityUnitOfWork",
    "CommunityUnitOfWorkFactory",
    "build_community_unit_of_work_factory",
]
