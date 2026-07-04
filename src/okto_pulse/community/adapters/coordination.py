"""Community local-first coordination adapters."""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    """Community board/artifact write locks.

    Generic ``WriteLockPort`` calls remain process-local for existing runtime
    coordination. KG single-writer calls use the local filesystem adapter
    surface below so cross-process Community processes preserve the legacy
    local-first semantics without leaking them into core.
    """

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

    def acquire_single_writer_sync(
        self,
        *,
        board_id: str,
        artifact_id: str,
        operation: str,
        owner_id: str,
        ttl_seconds: int,
        admin_lane: bool = False,
        base_dir_hint: str | None = None,
        board_dir_resolver: Any | None = None,
    ):
        from okto_pulse.core.kg.single_writer_lock import (
            LockAcquisition,
            RECOVERY_LOCK_FILENAME,
            RECOVERY_LOCK_TTL_SECONDS,
            SingleWriterLockError,
            SingleWriterLockErrorCode,
        )

        board_dir = self._single_writer_board_dir(
            board_id,
            base_dir_hint=base_dir_hint,
            board_dir_resolver=board_dir_resolver,
        )
        board_dir.mkdir(parents=True, exist_ok=True)
        path = self._single_writer_path(board_dir, artifact_id)
        created = self._create_single_writer_manifest(
            path=path,
            board_id=board_id,
            operation=operation,
            owner_id=owner_id,
            ttl_seconds=ttl_seconds,
            admin_lane=admin_lane,
        )
        if created is not None:
            return LockAcquisition(
                acquired=True,
                owner_token=created.owner_token,
                expires_at=self._iso(created.expires_at_epoch),
                current_owner=owner_id,
                admin_lane=admin_lane,
            )

        manifest = self._read_single_writer_manifest(path)
        if manifest is None:
            created = self._create_single_writer_manifest(
                path=path,
                board_id=board_id,
                operation=operation,
                owner_id=owner_id,
                ttl_seconds=ttl_seconds,
                admin_lane=admin_lane,
            )
            if created is not None:
                return LockAcquisition(
                    acquired=True,
                    owner_token=created.owner_token,
                    expires_at=self._iso(created.expires_at_epoch),
                    current_owner=owner_id,
                    admin_lane=admin_lane,
                )
            raise SingleWriterLockError(
                SingleWriterLockErrorCode.STALE_LOCK_RECOVERY_FAILED,
                retryable=True,
                reason="manifest_unreadable_and_recreate_failed",
            )

        if manifest.expires_at_epoch > time.time():
            return LockAcquisition(
                acquired=False,
                owner_token=None,
                expires_at=self._iso(manifest.expires_at_epoch),
                current_owner=manifest.owner_id,
                admin_lane=manifest.admin_lane,
            )

        recovery_path = board_dir / RECOVERY_LOCK_FILENAME
        recovery_manifest = {
            "owner_id": owner_id,
            "operation": operation,
            "board_id": board_id,
            "acquired_at_epoch": time.time(),
            "expires_at_epoch": time.time() + RECOVERY_LOCK_TTL_SECONDS,
        }
        try:
            with recovery_path.open("x", encoding="utf-8") as fh:
                json.dump(recovery_manifest, fh, indent=2)
        except FileExistsError:
            current_recovery = self._read_json_file(recovery_path)
            expires = float((current_recovery or {}).get("expires_at_epoch", 0))
            if current_recovery is not None and expires <= time.time():
                raise SingleWriterLockError(
                    SingleWriterLockErrorCode.STALE_LOCK_RECOVERY_FAILED,
                    retryable=False,
                    reason=(
                        "recovery_lock_stale_manual_intervention_required: "
                        f"former_owner={current_recovery.get('owner_id')} "
                        f"expired_at={self._iso(expires)}"
                    ),
                )
            current = self._read_single_writer_manifest(path)
            return LockAcquisition(
                acquired=False,
                owner_token=None,
                expires_at=(
                    self._iso(current.expires_at_epoch) if current else None
                ),
                current_owner=current.owner_id if current else None,
                admin_lane=current.admin_lane if current else False,
            )

        try:
            revalidated = self._read_single_writer_manifest(path)
            if (
                revalidated is not None
                and (
                    revalidated.owner_token != manifest.owner_token
                    or revalidated.expires_at_epoch != manifest.expires_at_epoch
                    or revalidated.owner_id != manifest.owner_id
                )
            ):
                return LockAcquisition(
                    acquired=False,
                    owner_token=None,
                    expires_at=self._iso(revalidated.expires_at_epoch),
                    current_owner=revalidated.owner_id,
                    admin_lane=revalidated.admin_lane,
                )

            try:
                path.unlink()
            except FileNotFoundError:
                pass

            created = self._create_single_writer_manifest(
                path=path,
                board_id=board_id,
                operation=operation,
                owner_id=owner_id,
                ttl_seconds=ttl_seconds,
                admin_lane=admin_lane,
            )
            if created is None:
                current = self._read_single_writer_manifest(path)
                if current is None:
                    raise SingleWriterLockError(
                        SingleWriterLockErrorCode.STALE_LOCK_RECOVERY_FAILED,
                        retryable=True,
                        reason="race_during_recovery_and_manifest_gone",
                    )
                return LockAcquisition(
                    acquired=False,
                    owner_token=None,
                    expires_at=self._iso(current.expires_at_epoch),
                    current_owner=current.owner_id,
                    admin_lane=current.admin_lane,
                )
            return LockAcquisition(
                acquired=True,
                owner_token=created.owner_token,
                expires_at=self._iso(created.expires_at_epoch),
                current_owner=owner_id,
                admin_lane=admin_lane,
                stale_recovered=True,
            )
        finally:
            try:
                recovery_path.unlink()
            except FileNotFoundError:
                pass

    def release_single_writer_sync(
        self,
        *,
        board_id: str,
        artifact_id: str,
        owner_token: str,
        base_dir_hint: str | None = None,
        board_dir_resolver: Any | None = None,
    ) -> bool:
        board_dir = self._single_writer_board_dir(
            board_id,
            base_dir_hint=base_dir_hint,
            board_dir_resolver=board_dir_resolver,
        )
        path = self._single_writer_path(board_dir, artifact_id)
        manifest = self._read_single_writer_manifest(path)
        if manifest is None or manifest.owner_token != owner_token:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def inspect_single_writer_sync(
        self,
        *,
        board_id: str,
        artifact_id: str,
        base_dir_hint: str | None = None,
        board_dir_resolver: Any | None = None,
    ):
        board_dir = self._single_writer_board_dir(
            board_id,
            base_dir_hint=base_dir_hint,
            board_dir_resolver=board_dir_resolver,
        )
        return self._read_single_writer_manifest(
            self._single_writer_path(board_dir, artifact_id)
        )

    @staticmethod
    def _iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _single_writer_board_dir(
        self,
        board_id: str,
        *,
        base_dir_hint: str | None,
        board_dir_resolver: Any | None,
    ) -> Path:
        if board_dir_resolver is not None:
            return Path(board_dir_resolver(board_id))
        if base_dir_hint:
            return Path(base_dir_hint) / board_id
        from okto_pulse.community.adapters.rebuild_audit_storage import (
            default_community_rebuild_base_dir,
        )

        return default_community_rebuild_base_dir() / "locks" / board_id

    @staticmethod
    def _single_writer_path(board_dir: Path, artifact_id: str) -> Path:
        from okto_pulse.core.kg.single_writer_lock import LOCK_FILENAME

        if artifact_id == "kg_single_writer":
            return board_dir / LOCK_FILENAME
        return board_dir / f".{artifact_id}.lock"

    def _read_single_writer_manifest(self, path: Path):
        from okto_pulse.core.kg.single_writer_lock import LockManifest

        data = self._read_json_file(path)
        if data is None:
            return None
        try:
            return LockManifest.from_disk_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def _create_single_writer_manifest(
        self,
        *,
        path: Path,
        board_id: str,
        operation: str,
        owner_id: str,
        ttl_seconds: int,
        admin_lane: bool,
    ):
        from okto_pulse.core.kg.single_writer_lock import LockManifest

        acquired_at = time.time()
        manifest = LockManifest(
            owner_token=secrets.token_urlsafe(24),
            owner_id=owner_id,
            operation=operation,
            acquired_at_epoch=acquired_at,
            expires_at_epoch=acquired_at + ttl_seconds,
            admin_lane=admin_lane,
        )
        try:
            with path.open("x", encoding="utf-8") as fh:
                json.dump(manifest.to_disk_dict(), fh, indent=2)
        except FileExistsError:
            return None
        return manifest


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
