"""Community local-first coordination adapters."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ConsolidationQueue,
    DomainEventHandlerExecution,
    DomainEventRow,
    GlobalUpdateOutbox,
)
from okto_pulse.core.kg.interfaces import (
    GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
    GLOBAL_DISCOVERY_WRITER_SCOPE,
)
from okto_pulse.core.ports.coordination import (
    ClaimRepository,
    ConfigValidationPort,
    CoordinationProviderMissing,
    LeaseHandle,
    LeaseProvider,
    RuntimeSettingsProvider,
    WriteLockHandle,
    WriteLockPort,
    get_write_lock_port,
    register_coordination_providers,
)

# A5R: Windows can transiently deny the writer-manifest atomic os.replace with
# a sharing violation (PermissionError [WinError 5]) while an unrelated reader
# briefly holds the destination open.  Only that exact replace is retried, a
# fixed number of times with fixed backoffs; every retry restarts the full
# attempt (fresh recovery lock, fresh manifest read, fresh clock), so an
# expired, replaced or foreign-token manifest can never be resurrected.  Two
# backoffs mean three total attempts and at most 0.15s of added latency, far
# below the shortest writer-lease TTL.
_SINGLE_WRITER_RENEW_REPLACE_ATTEMPTS = 3
_SINGLE_WRITER_RENEW_REPLACE_BACKOFF_SECONDS = (0.05, 0.1)


class _SingleWriterRenewReplaceDenied(Exception):
    """PermissionError raised by the exact renewal-manifest os.replace."""

    def __init__(self, original: PermissionError) -> None:
        self.original = original
        super().__init__(str(original))


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
                expires_at=(self._iso(current.expires_at_epoch) if current else None),
                current_owner=current.owner_id if current else None,
                admin_lane=current.admin_lane if current else False,
            )

        try:
            revalidated = self._read_single_writer_manifest(path)
            if revalidated is not None and (
                revalidated.owner_token != manifest.owner_token
                or revalidated.expires_at_epoch != manifest.expires_at_epoch
                or revalidated.owner_id != manifest.owner_id
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
        from okto_pulse.core.kg.single_writer_lock import (
            RECOVERY_LOCK_FILENAME,
            RECOVERY_LOCK_TTL_SECONDS,
        )

        recovery_path = board_dir / RECOVERY_LOCK_FILENAME
        try:
            with recovery_path.open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "owner_id": "exact-token-release",
                        "operation": "release",
                        "board_id": board_id,
                        "acquired_at_epoch": time.time(),
                        "expires_at_epoch": (time.time() + RECOVERY_LOCK_TTL_SECONDS),
                    },
                    stream,
                )
        except FileExistsError:
            return False
        try:
            manifest = self._read_single_writer_manifest(path)
            if manifest is None or manifest.owner_token != owner_token:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            return True
        finally:
            try:
                recovery_path.unlink()
            except FileNotFoundError:
                pass
            try:
                board_dir.rmdir()
            except OSError:
                pass
            else:
                from okto_pulse.community.adapters.filesystem_erasure import (
                    fsync_directory,
                )

                fsync_directory(board_dir.parent)

    def renew_single_writer_sync(
        self,
        *,
        board_id: str,
        artifact_id: str,
        owner_token: str,
        ttl_seconds: int,
        base_dir_hint: str | None = None,
        board_dir_resolver: Any | None = None,
    ) -> bool:
        """Atomically extend the exact unexpired manifest token."""

        board_dir = self._single_writer_board_dir(
            board_id,
            base_dir_hint=base_dir_hint,
            board_dir_resolver=board_dir_resolver,
        )
        board_dir.mkdir(parents=True, exist_ok=True)
        path = self._single_writer_path(board_dir, artifact_id)
        from okto_pulse.core.kg.single_writer_lock import (
            MAX_TTL_SECONDS,
            RECOVERY_LOCK_FILENAME,
        )

        if ttl_seconds < 1 or ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError("ttl_seconds outside the supported writer-lease range")
        recovery_path = board_dir / RECOVERY_LOCK_FILENAME
        last_denial: PermissionError | None = None
        for attempt_index in range(_SINGLE_WRITER_RENEW_REPLACE_ATTEMPTS):
            if attempt_index:
                time.sleep(
                    _SINGLE_WRITER_RENEW_REPLACE_BACKOFF_SECONDS[attempt_index - 1]
                )
            try:
                return self._renew_single_writer_attempt(
                    board_id=board_id,
                    path=path,
                    recovery_path=recovery_path,
                    owner_token=owner_token,
                    ttl_seconds=ttl_seconds,
                )
            except _SingleWriterRenewReplaceDenied as denied:
                last_denial = denied.original
        assert last_denial is not None
        raise last_denial

    def _renew_single_writer_attempt(
        self,
        *,
        board_id: str,
        path: Path,
        recovery_path: Path,
        owner_token: str,
        ttl_seconds: int,
    ) -> bool:
        from okto_pulse.core.kg.single_writer_lock import (
            LockManifest,
            RECOVERY_LOCK_TTL_SECONDS,
        )

        now = time.time()
        try:
            with recovery_path.open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "owner_id": "exact-token-renewal",
                        "operation": "renew",
                        "board_id": board_id,
                        "acquired_at_epoch": now,
                        "expires_at_epoch": now + RECOVERY_LOCK_TTL_SECONDS,
                    },
                    stream,
                )
        except FileExistsError:
            return False

        temporary: Path | None = None
        try:
            manifest = self._read_single_writer_manifest(path)
            if (
                manifest is None
                or manifest.owner_token != owner_token
                or manifest.expires_at_epoch <= now
            ):
                return False
            renewed = LockManifest(
                owner_token=manifest.owner_token,
                owner_id=manifest.owner_id,
                operation=manifest.operation,
                acquired_at_epoch=manifest.acquired_at_epoch,
                expires_at_epoch=now + ttl_seconds,
                admin_lane=manifest.admin_lane,
            )
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.renewing")
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(renewed.to_disk_dict(), stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            revalidated = self._read_single_writer_manifest(path)
            # Fresh-clock recheck IMMEDIATELY before publication: the durable
            # temp write/fsync above can take arbitrarily long, so identity
            # equality with the snapshot is not enough — the live manifest AND
            # the expiry being published must both still be in the future at
            # replace time, or an expired lease would be resurrected.
            replace_now = time.time()
            if (
                revalidated is None
                or revalidated.owner_token != manifest.owner_token
                or revalidated.expires_at_epoch != manifest.expires_at_epoch
                or revalidated.expires_at_epoch <= replace_now
                or renewed.expires_at_epoch <= replace_now
            ):
                return False
            try:
                os.replace(temporary, path)
            except PermissionError as exc:
                raise _SingleWriterRenewReplaceDenied(exc) from exc
            temporary = None
            return True
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            try:
                recovery_path.unlink()
            except FileNotFoundError:
                pass

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
        from okto_pulse.core import get_settings

        settings = get_settings()
        return settings.model_dump()

    def validate_runtime_settings(self, values: Mapping[str, Any]) -> None:
        from okto_pulse.core import get_settings

        configured = get_settings()
        current = configured.model_dump()
        current.update(dict(values))
        type(configured)(**current)


_lease_provider = CommunityLocalLeaseProvider()
_write_lock_port = CommunityLocalWriteLockPort()
_claim_repository = CommunitySqlAlchemyClaimRepository()
_runtime_settings_provider = CommunityRuntimeSettingsProvider()


@contextmanager
def community_global_discovery_writer_fence(operation: str):
    """Use the exact filesystem fence for edition-owned checkpoint/close."""

    try:
        port = get_write_lock_port()
    except CoordinationProviderMissing:
        # Test/late-shutdown fallback: when the runtime registry is already
        # torn down, the edition singleton is the only possible writer port.
        port = _write_lock_port
    acquire = getattr(port, "acquire_single_writer_sync", None)
    release = getattr(port, "release_single_writer_sync", None)
    if not callable(acquire) or not callable(release):
        raise RuntimeError("global_discovery_writer_fence_unavailable")
    acquisition = acquire(
        board_id=GLOBAL_DISCOVERY_WRITER_SCOPE,
        artifact_id=GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
        operation=operation,
        owner_id=f"community:{operation}:{uuid.uuid4().hex}",
        ttl_seconds=300,
        admin_lane=True,
    )
    if isinstance(acquisition, Mapping):
        acquired = bool(acquisition.get("acquired"))
        owner_token = acquisition.get("owner_token")
        current_owner = acquisition.get("current_owner")
    else:
        acquired = bool(getattr(acquisition, "acquired", False))
        owner_token = getattr(acquisition, "owner_token", None)
        current_owner = getattr(acquisition, "current_owner", None)
    if not acquired or not isinstance(owner_token, str) or not owner_token:
        raise RuntimeError(
            f"global_discovery_writer_contention:owner={current_owner or 'unknown'}"
        )
    try:
        yield owner_token
    finally:
        released = release(
            board_id=GLOBAL_DISCOVERY_WRITER_SCOPE,
            artifact_id=GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
            owner_token=owner_token,
        )
        if not released:
            raise RuntimeError("global_discovery_writer_fence_lost")


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
    "community_global_discovery_writer_fence",
    "register_community_coordination_providers",
]
