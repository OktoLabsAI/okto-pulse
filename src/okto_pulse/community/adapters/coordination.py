"""Community local-first coordination adapters."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from okto_pulse.core.models.db import (
    ConsolidationQueue,
    DomainEventHandlerExecution,
    DomainEventRow,
    GlobalUpdateOutbox,
)
from okto_pulse.core.ports.coordination import (
    ClaimRepository,
    ConfigValidationPort,
    LeaseHandle,
    LeaseProvider,
    RuntimeSettingsProvider,
    WriteLockHandle,
    WriteLockPort,
    register_coordination_providers,
)


class CommunityLocalLeaseProvider(LeaseProvider):
    """In-process lease provider for the local-first Community runtime."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._handles: dict[str, LeaseHandle] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, resource: str) -> asyncio.Lock:
        with self._registry_lock:
            lock = self._locks.get(resource)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[resource] = lock
            return lock

    async def try_acquire(
        self,
        resource: str,
        *,
        ttl_seconds: int | None = None,
        owner_token: str | None = None,
    ) -> LeaseHandle | None:
        lock = self._lock_for(resource)
        if lock.locked():
            return None
        await lock.acquire()
        now = datetime.now(timezone.utc)
        handle = LeaseHandle(
            resource=resource,
            owner_token=owner_token or uuid.uuid4().hex,
            fencing_token=uuid.uuid4().hex,
            expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
        )
        self._handles[resource] = handle
        return handle

    async def release(self, handle: LeaseHandle) -> None:
        current = self._handles.get(handle.resource)
        if current != handle:
            return
        self._handles.pop(handle.resource, None)
        lock = self._lock_for(handle.resource)
        if lock.locked():
            lock.release()

    def is_held(self, resource: str) -> bool:
        return self._lock_for(resource).locked()


class CommunityLocalWriteLockPort(WriteLockPort):
    """Process-local board/artifact write locks for Community."""

    def __init__(self) -> None:
        self._async_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._sync_locks: dict[tuple[str, str], threading.Lock] = {}
        self._async_handles: dict[tuple[str, str], WriteLockHandle] = {}
        self._sync_handles: dict[tuple[str, str], WriteLockHandle] = {}
        self._registry_lock = threading.Lock()

    @staticmethod
    def _key(board_id: str, artifact_id: str) -> tuple[str, str]:
        return (board_id, artifact_id)

    def _async_lock_for(self, board_id: str, artifact_id: str) -> asyncio.Lock:
        key = self._key(board_id, artifact_id)
        with self._registry_lock:
            lock = self._async_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._async_locks[key] = lock
            return lock

    def _sync_lock_for(self, board_id: str, artifact_id: str) -> threading.Lock:
        key = self._key(board_id, artifact_id)
        with self._registry_lock:
            lock = self._sync_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._sync_locks[key] = lock
            return lock

    async def acquire(
        self,
        board_id: str,
        artifact_id: str,
        *,
        owner_token: str | None = None,
    ) -> WriteLockHandle:
        lock = self._async_lock_for(board_id, artifact_id)
        await lock.acquire()
        handle = WriteLockHandle(
            board_id=board_id,
            artifact_id=artifact_id,
            owner_token=owner_token or uuid.uuid4().hex,
            fencing_token=uuid.uuid4().hex,
        )
        self._async_handles[self._key(board_id, artifact_id)] = handle
        return handle

    async def release(self, handle: WriteLockHandle) -> None:
        key = self._key(handle.board_id, handle.artifact_id)
        if self._async_handles.get(key) != handle:
            return
        self._async_handles.pop(key, None)
        lock = self._async_lock_for(handle.board_id, handle.artifact_id)
        if lock.locked():
            lock.release()

    def acquire_sync(
        self,
        board_id: str,
        artifact_id: str,
        *,
        owner_token: str | None = None,
    ) -> WriteLockHandle:
        lock = self._sync_lock_for(board_id, artifact_id)
        lock.acquire()
        handle = WriteLockHandle(
            board_id=board_id,
            artifact_id=artifact_id,
            owner_token=owner_token or uuid.uuid4().hex,
            fencing_token=uuid.uuid4().hex,
        )
        self._sync_handles[self._key(board_id, artifact_id)] = handle
        return handle

    def release_sync(self, handle: WriteLockHandle) -> None:
        key = self._key(handle.board_id, handle.artifact_id)
        if self._sync_handles.get(key) != handle:
            return
        self._sync_handles.pop(key, None)
        lock = self._sync_lock_for(handle.board_id, handle.artifact_id)
        if lock.locked():
            lock.release()

    def is_locked(self, board_id: str, artifact_id: str) -> bool:
        return (
            self._async_lock_for(board_id, artifact_id).locked()
            or self._sync_lock_for(board_id, artifact_id).locked()
        )

    def reset_for_tests(self) -> None:
        with self._registry_lock:
            self._async_locks.clear()
            self._sync_locks.clear()
            self._async_handles.clear()
            self._sync_handles.clear()


class CommunitySqlAlchemyClaimRepository(ClaimRepository):
    """SQLite/SQLAlchemy claim adapter for local-first workers."""

    async def claim_global_outbox(
        self,
        session: Any,
        *,
        limit: int,
    ) -> Sequence[Any]:
        result = await session.execute(
            select(GlobalUpdateOutbox)
            .where(
                GlobalUpdateOutbox.processed_at.is_(None),
                GlobalUpdateOutbox.retry_count >= 0,
                GlobalUpdateOutbox.retry_count < 5,
            )
            .order_by(GlobalUpdateOutbox.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def claim_domain_event_executions(
        self,
        session: Any,
        *,
        limit: int,
        now: datetime,
    ) -> Sequence[tuple[str, str]]:
        result = await session.execute(
            select(
                DomainEventHandlerExecution.id,
                DomainEventHandlerExecution.event_id,
            )
            .join(
                DomainEventRow,
                DomainEventRow.id == DomainEventHandlerExecution.event_id,
            )
            .where(DomainEventHandlerExecution.status == "pending")
            .where(
                (DomainEventHandlerExecution.next_attempt_at.is_(None))
                | (DomainEventHandlerExecution.next_attempt_at <= now)
            )
            .order_by(
                DomainEventRow.occurred_at.asc(),
                DomainEventRow.id.asc(),
            )
            .limit(limit)
        )
        return list(result.all())

    async def claim_consolidation_queue(
        self,
        session: Any,
        *,
        board_id: str | None,
        limit: int,
    ) -> Sequence[Any]:
        query = (
            select(ConsolidationQueue)
            .where(ConsolidationQueue.status == "pending")
            .order_by(ConsolidationQueue.created_at.asc())
            .limit(limit)
        )
        if board_id:
            query = query.where(ConsolidationQueue.board_id == board_id)
        result = await session.execute(query)
        return list(result.scalars().all())


class CommunityRuntimeSettingsProvider(RuntimeSettingsProvider, ConfigValidationPort):
    """Community runtime settings reader/validator."""

    async def read_runtime_settings(self, scope: str = "global") -> Mapping[str, Any]:
        from okto_pulse.core.infra.config import get_settings

        settings = get_settings()
        return settings.model_dump()

    def validate_runtime_settings(self, values: Mapping[str, Any]) -> None:
        from okto_pulse.core.infra.config import CoreSettings, get_settings

        current = get_settings().model_dump()
        current.update(dict(values))
        CoreSettings(**current)


_lease_provider = CommunityLocalLeaseProvider()
_write_lock_port = CommunityLocalWriteLockPort()
_claim_repository = CommunitySqlAlchemyClaimRepository()
_runtime_settings_provider = CommunityRuntimeSettingsProvider()


def register_community_coordination_providers() -> None:
    """Register Community local-first coordination providers in core ports."""

    register_coordination_providers(
        lease_provider=_lease_provider,
        write_lock_port=_write_lock_port,
        claim_repository=_claim_repository,
        runtime_settings_provider=_runtime_settings_provider,
        config_validation_port=_runtime_settings_provider,
    )


__all__ = [
    "CommunityLocalLeaseProvider",
    "CommunityLocalWriteLockPort",
    "CommunityRuntimeSettingsProvider",
    "CommunitySqlAlchemyClaimRepository",
    "register_community_coordination_providers",
]
