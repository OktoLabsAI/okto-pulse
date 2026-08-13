"""Community SQLAlchemy UnitOfWork adapter + factory (R01B REPLAN-IMP1).

Mirrors the core ``okto_pulse.core.repositories.sqlalchemy.unit_of_work``
concretes that implement the ``PulseUnitOfWork`` / ``UnitOfWorkFactory`` PORTS
(``okto_pulse.core.repositories.interfaces.unit_of_work``).

``CommunityUnitOfWork`` wraps an ``AsyncSession`` by composition, owns the
transaction boundary (commit/rollback/close) and exposes the repository catalog
(boards/ideations/specs). Its teardown rolls back any transaction still active,
including one left poisoned after a consumer caught a database exception, and
ALWAYS closes the session in a ``finally``. An explicitly committed happy path
has no active transaction and receives no redundant rollback. ``__aexit__``
returns ``None`` so it never suppresses an exception. The same path is reached
whether the consumer enters via the factory or via
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

import asyncio
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
import logging
import sys
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError, OperationalError
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
from okto_pulse.core.repositories.interfaces.unit_of_work import (
    ConsistentReadContractError,
)
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    is_knowledge_creation_race_error,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    bind_semantic_subject_actor,
    unbind_semantic_subject_actor,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import (
    CommunitySqlAlchemySemanticGuidelineAssessmentV2,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_subject_projection import (
    CommunitySqlAlchemySemanticSubjectProjection,
)
from okto_pulse.community.adapters.semantic_assessment_v2_capabilities import (
    CommunitySemanticAssessmentV2Capabilities,
)
from okto_pulse.core.ports.knowledge_propagation import KnowledgeTargetKey

if TYPE_CHECKING:
    from okto_pulse.core.application.use_cases.base import ActorContext


logger = logging.getLogger(__name__)
_UOW_CLEANUP_DRAIN_TIMEOUT_S = 5.0
_pending_uow_cleanups: set[asyncio.Task[Any]] = set()
_CONSISTENT_READ_INFO_KEY = "okto_pulse_consistent_read"
_REPEATABLE_READ = "REPEATABLE READ"


def _knowledge_creation_race_target(
    conflict_error: Exception,
) -> KnowledgeTargetKey | None:
    """Return the verified target carried by a Core creation-race envelope."""

    if getattr(conflict_error, "code", None) != "knowledge_creation_race":
        return None
    target = getattr(conflict_error, "target", None)
    if not isinstance(target, KnowledgeTargetKey):
        return None
    details = getattr(conflict_error, "details", None)
    if not isinstance(details, Mapping):
        return None
    projected_target = details.get("target")
    if not isinstance(projected_target, Mapping):
        return None
    if dict(projected_target) != target.to_dict():
        return None
    return target


def _log_background_uow_cleanup_failure(task: asyncio.Future[Any]) -> None:
    """Consume a detached UoW teardown failure with structured evidence."""

    if task.cancelled():
        logger.error(
            "db.uow.background_cleanup_cancelled",
            extra={"event": "db.uow.background_cleanup_cancelled"},
        )
        return
    try:
        task.result()
    except BaseException as exc:  # cleanup failure must never escape callback
        logger.error(
            "db.uow.background_cleanup_failed error_type=%s",
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "event": "db.uow.background_cleanup_failed",
                "error_type": type(exc).__name__,
            },
        )


async def drain_pending_uow_cleanups(
    *,
    timeout_s: float | None = None,
) -> None:
    """Wait boundedly for detached UoW rollback/close tasks.

    The Community database runtime invokes this before disposing its engine.
    The deadline covers all cleanup tasks observed during that close and never
    cancels a stuck driver task; those tasks retain their structured terminal
    observer and may still release their connection after the deadline.
    """

    timeout = _UOW_CLEANUP_DRAIN_TIMEOUT_S if timeout_s is None else float(timeout_s)
    if timeout <= 0:
        raise ValueError("timeout_s must be positive")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    current = asyncio.current_task()
    while True:
        tasks = {
            task
            for task in _pending_uow_cleanups
            if not task.done() and task is not current
        }
        if not tasks:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning(
                "db.uow.cleanup_drain_timeout pending=%d timeout_s=%.3f",
                len(tasks),
                timeout,
                extra={
                    "event": "db.uow.cleanup_drain_timeout",
                    "pending_count": len(tasks),
                    "timeout_s": timeout,
                },
            )
            return
        _done, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending:
            logger.warning(
                "db.uow.cleanup_drain_timeout pending=%d timeout_s=%.3f",
                len(pending),
                timeout,
                extra={
                    "event": "db.uow.cleanup_drain_timeout",
                    "pending_count": len(pending),
                    "timeout_s": timeout,
                },
            )
            return


def _community_realm_scope(
    realm_scope: RealmScope | None,
    realm_id: str | None,
) -> RealmScope:
    if realm_scope is not None:
        return require_realm_scope(realm_scope)
    if realm_id:
        return (
            RealmScope.local() if realm_id == "local" else RealmScope.tenant(realm_id)
        )
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
        bind_semantic_subject_actor(self._session, actor)
        self._application_persistence = (
            application_persistence or CommunitySqlAlchemyApplicationPersistence()
        )
        if application_persistence is None:
            register_application_persistence_port(self._application_persistence)
        self.boards = CommunityBoardRepository(session, self.realm_scope)
        self.ideations = CommunityIdeationRepository(session, self.realm_scope)
        self.specs = CommunitySpecRepository(session, self.realm_scope)
        self.services = build_application_service_catalog(session)
        self.semantic_subject_projection = (
            CommunitySqlAlchemySemanticSubjectProjection(session)
        )
        self.semantic_assessment_v2 = (
            CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
        )
        self.semantic_assessment_v2_reader = self.semantic_assessment_v2
        self.semantic_assessment_v2_capability = (
            CommunitySemanticAssessmentV2Capabilities(session)
        )

    async def __aenter__(self) -> "CommunityUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Single, entry-style-independent teardown: roll back any still-active
        # transaction and ALWAYS close the session. This also covers a database
        # error caught inside the context, while an explicit successful commit
        # leaves no transaction and therefore receives no extra rollback.
        async def teardown() -> None:
            try:
                in_transaction = getattr(self._session, "in_transaction", None)
                active_transaction = (
                    bool(in_transaction())
                    if callable(in_transaction)
                    else exc is not None
                )
                if active_transaction:
                    await self.rollback()
            finally:
                await self.close()

        cleanup = asyncio.create_task(teardown(), name="community.uow.teardown")
        _pending_uow_cleanups.add(cleanup)
        cleanup.add_done_callback(_pending_uow_cleanups.discard)
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cleanup.add_done_callback(_log_background_uow_cleanup_failure)
            raise
        return None

    async def commit(self) -> None:
        try:
            await self._application_persistence.commit(self._session)
        finally:
            self._session.info.pop(_CONSISTENT_READ_INFO_KEY, None)

    async def rollback(self) -> None:
        try:
            await self._application_persistence.rollback(self._session)
        finally:
            self._session.info.pop(_CONSISTENT_READ_INFO_KEY, None)

    async def begin_consistent_read(self) -> None:
        """Pin one snapshot before a composite Core read performs any lookup.

        SQLite receives a deferred ``BEGIN``: its first subsequent SELECT pins
        a WAL snapshot while concurrent writers remain able to commit.
        PostgreSQL receives ``REPEATABLE READ`` as a connection execution
        option before its first physical statement.  An existing PostgreSQL
        transaction is accepted only when it already has that isolation;
        ``READ COMMITTED`` is never upgraded after the fact.
        """

        marker = self._session.info.get(_CONSISTENT_READ_INFO_KEY)
        if marker is not None:
            if not self._session.in_transaction():
                raise ConsistentReadContractError(
                    "consistent_read_snapshot_no_longer_active"
                )
            return

        bind = self._session.get_bind()
        dialect = bind.dialect.name if bind is not None else ""
        transaction_already_active = bool(self._session.in_transaction())

        if dialect == "sqlite":
            connection = await self._session.connection()

            def physical_transaction_active(sync_connection: Any) -> bool:
                driver_connection = getattr(
                    sync_connection.connection,
                    "driver_connection",
                    None,
                )
                return bool(getattr(driver_connection, "in_transaction", False))

            physical_active = await connection.run_sync(
                physical_transaction_active
            )
            if not physical_active:
                await connection.exec_driver_sql("BEGIN")
            self._session.info[_CONSISTENT_READ_INFO_KEY] = "sqlite_deferred"
            return

        if dialect == "postgresql":
            if transaction_already_active:
                connection = await self._session.connection()
            else:
                connection = await self._session.connection(
                    execution_options={"isolation_level": _REPEATABLE_READ}
                )
            isolation = (await connection.get_isolation_level()).replace(
                "_", " "
            ).upper()
            if isolation != _REPEATABLE_READ:
                raise ConsistentReadContractError(
                    "consistent_read_incompatible_active_transaction"
                )
            self._session.info[_CONSISTENT_READ_INFO_KEY] = "postgresql_repeatable_read"
            return

        raise ConsistentReadContractError("consistent_read_dialect_unsupported")

    async def synchronize(
        self,
        *,
        conflict_error: Exception | None = None,
    ) -> None:
        try:
            await self._application_persistence.flush(self._session)
        except (IntegrityError, OperationalError) as exc:
            if conflict_error is None:
                raise
            target = _knowledge_creation_race_target(conflict_error)
            if target is None or not is_knowledge_creation_race_error(
                exc,
                target_type=target.target_type,
                target_id=target.target_id,
            ):
                raise
            # Translate only a verified deterministic-target collision. Leave
            # rollback/close to __aexit__; this adapter never commits or
            # repairs a poisoned UoW.
            raise conflict_error from exc

    async def reload(self, entity: object, *, fields: tuple[str, ...] = ()) -> None:
        if isinstance(entity, ApplicationRecord):
            await self._application_persistence.refresh(self._session, entity)
            return
        await self._session.refresh(
            entity,
            attribute_names=list(fields) if fields else None,
        )

    async def close(self) -> None:
        try:
            await self._session.close()
        finally:
            self._session.info.pop(_CONSISTENT_READ_INFO_KEY, None)
            unbind_semantic_subject_actor(self._session)


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
        self._checkout_owner_scope: Any | None = None

    async def __aenter__(self) -> CommunityUnitOfWork:
        from okto_pulse.community.adapters.sqlalchemy_database import (
            community_database_checkout_owner,
        )

        owner_scope = community_database_checkout_owner("community.uow")
        owner_scope.__enter__()
        self._checkout_owner_scope = owner_scope
        try:
            session = self._session_factory()
            self._uow = CommunityUnitOfWork(
                session,
                realm_scope=self._realm_scope,
                actor=self._actor,
                application_persistence=self._application_persistence,
            )
            return self._uow
        except BaseException:
            owner_scope.__exit__(*sys.exc_info())
            self._checkout_owner_scope = None
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if self._uow is not None:
                await self._uow.__aexit__(exc_type, exc, tb)
        finally:
            if self._checkout_owner_scope is not None:
                self._checkout_owner_scope.__exit__(None, None, None)
                self._checkout_owner_scope = None
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
    "drain_pending_uow_cleanups",
]
