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
from okto_pulse.core.application.service_catalog import (
    build_application_service_catalog,
)
from okto_pulse.core.ports.application_persistence import (
    ApplicationRecord,
    ApplicationPersistencePort,
    register_application_persistence_port,
)
from okto_pulse.core.domain.realm import RealmScope, require_realm_scope
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)

if TYPE_CHECKING:
    from okto_pulse.core.application.use_cases.base import ActorContext


def _community_realm_scope(
    realm_scope: RealmScope | None,
    realm_id: str | None,
) -> RealmScope:
    if realm_scope is not None:
        return require_realm_scope(realm_scope)
    if realm_id:
        return RealmScope.local() if realm_id == "local" else RealmScope.tenant(realm_id)
    return RealmScope.local()


class CommunityUnitOfWork:
    """PulseUnitOfWork backed by a SQLAlchemy AsyncSession (Community)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        realm_scope: RealmScope | None = None,
        realm_id: str | None = None,
        actor: "ActorContext | None" = None,
        application_persistence: ApplicationPersistencePort | None = None,
    ) -> None:
        self._session = session
        # realm-ready, NOT enforced this phase (fr_cbfcb1aa).
        self.realm_scope = _community_realm_scope(realm_scope, realm_id)
        self.realm_id = self.realm_scope.realm_id
        self.actor = actor
        self._session.info["realm_scope"] = self.realm_scope
        self._application_persistence = (
            application_persistence or CommunitySqlAlchemyApplicationPersistence()
        )
        if application_persistence is None:
            register_application_persistence_port(self._application_persistence)
        self.boards = CommunityBoardRepository(session, self.realm_scope)
        self.ideations = CommunityIdeationRepository(session, self.realm_scope)
        self.specs = CommunitySpecRepository(session, self.realm_scope)
        self.services = build_application_service_catalog(session)

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
        await self._application_persistence.commit(self._session)

    async def rollback(self) -> None:
        await self._application_persistence.rollback(self._session)

    async def synchronize(self) -> None:
        await self._application_persistence.flush(self._session)

    async def reload(
        self, entity: object, *, fields: tuple[str, ...] = ()
    ) -> None:
        if isinstance(entity, ApplicationRecord):
            await self._application_persistence.refresh(self._session, entity)
            return
        await self._session.refresh(
            entity,
            attribute_names=list(fields) if fields else None,
        )

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
        realm_scope: RealmScope,
        actor: "ActorContext | None",
        application_persistence: ApplicationPersistencePort,
    ) -> None:
        self._session_factory = session_factory
        self._realm_scope = require_realm_scope(realm_scope)
        self._actor = actor
        self._application_persistence = application_persistence
        self._uow: CommunityUnitOfWork | None = None

    async def __aenter__(self) -> CommunityUnitOfWork:
        session = self._session_factory()
        self._uow = CommunityUnitOfWork(
            session,
            realm_scope=self._realm_scope,
            actor=self._actor,
            application_persistence=self._application_persistence,
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
        self._application_persistence = CommunitySqlAlchemyApplicationPersistence()
        register_application_persistence_port(self._application_persistence)

    def resolve_realm_scope(self) -> RealmScope:
        """Community is a single local realm by product definition."""

        return RealmScope.local()

    def __call__(
        self,
        *,
        realm_scope: RealmScope | None = None,
        realm_id: str | None = None,
        actor: "ActorContext | None" = None,
    ) -> AbstractAsyncContextManager["CommunityUnitOfWork"]:
        resolved_scope = _community_realm_scope(realm_scope, realm_id)
        return _CommunityUnitOfWorkContext(
            self._session_factory,
            realm_scope=resolved_scope,
            actor=actor,
            application_persistence=self._application_persistence,
        )

    def wrap(
        self,
        session: AsyncSession,
        *,
        realm_scope: RealmScope | None = None,
        realm_id: str | None = None,
        actor: "ActorContext | None" = None,
    ) -> "CommunityUnitOfWork":
        """Request-scoped bridge (R01B FR3): wrap an EXTERNALLY-owned session
        (the REST ``Depends(get_db)`` session) in a unit of work WITHOUT taking
        over its lifecycle. The caller (``get_db``) still closes the session; the
        returned UoW is used as a plain object (the use case commits/rolls back),
        NOT entered as an ``async with`` context. Byte-for-byte the same
        request-scoped semantics the core ``SQLAlchemyUnitOfWork(db)`` had."""
        return CommunityUnitOfWork(
            session,
            realm_scope=_community_realm_scope(realm_scope, realm_id),
            actor=actor,
            application_persistence=self._application_persistence,
        )


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
