"""Relational source/recovery authority, independent of graph storage."""

from __future__ import annotations

_SOURCE_FENCE_BUSY_TIMEOUT_MS = 750
_SOURCE_FENCE_TOTAL_BUDGET_SECONDS = 1.5
import contextlib
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from okto_pulse.community.adapters.relational_schema_steps import (
    global_discovery_source_revision_trigger_manifest,
    normalize_global_discovery_source_revision_trigger_sql,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
    GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
    GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
    GlobalDiscoverySourceRevision,
)


@dataclass(frozen=True, slots=True)
class CommunitySourceRevisionFence:
    """Validated transactional fence read from the singleton revision row."""

    fence_version: str
    trigger_manifest_version: str
    scope_id: str
    incarnation_id: str
    revision: int
    mutation_nonce: str

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "fence_version": self.fence_version,
                "incarnation_id": self.incarnation_id,
                "mutation_nonce": self.mutation_nonce,
                "revision": self.revision,
                "scope_id": self.scope_id,
                "trigger_manifest_version": self.trigger_manifest_version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class CommunityRecoveryAttemptReconciliation:
    """Bounded retention outcome for attempt-owned quarantine artifacts."""

    quarantined_ids: tuple[str, ...] = ()
    retained_ids: tuple[str, ...] = ()
    deleted_ids: tuple[str, ...] = ()


class CommunityRelationalRecoverySnapshotFingerprint:
    """O(1), graph-free fingerprint of the transactional source fence."""

    def __init__(self, *, db_path_provider: Callable[[], Path]) -> None:
        self._db_path_provider = db_path_provider

    @staticmethod
    def read_fence_from_connection(
        connection: sqlite3.Connection,
    ) -> CommunitySourceRevisionFence:
        """Read and validate the fence inside the caller's transaction."""

        expected_triggers = global_discovery_source_revision_trigger_manifest()
        trigger_rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name LIKE ?",
            (f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%",),
        ).fetchall()
        actual_triggers = {str(trigger["name"]): trigger for trigger in trigger_rows}
        if set(actual_triggers) != set(expected_triggers):
            raise sqlite3.DatabaseError(
                "authoritative source revision trigger manifest is incomplete"
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            trigger = actual_triggers[trigger_name]
            if str(
                trigger["tbl_name"]
            ) != table_name or normalize_global_discovery_source_revision_trigger_sql(
                trigger["sql"]
            ) != normalize_global_discovery_source_revision_trigger_sql(trigger_sql):
                raise sqlite3.DatabaseError(
                    "authoritative source revision trigger manifest is corrupt"
                )
        rows = connection.execute(
            "SELECT scope_id, fence_version, trigger_manifest_version, "
            "incarnation_id, revision, mutation_nonce "
            f'FROM "{GlobalDiscoverySourceRevision.__tablename__}"'
        ).fetchall()
        if len(rows) != 1:
            raise sqlite3.DatabaseError(
                "authoritative source revision singleton is invalid"
            )
        row = rows[0]
        fence = CommunitySourceRevisionFence(
            scope_id=str(row["scope_id"]),
            fence_version=str(row["fence_version"]),
            trigger_manifest_version=str(row["trigger_manifest_version"]),
            incarnation_id=str(row["incarnation_id"]),
            revision=row["revision"],
            mutation_nonce=str(row["mutation_nonce"]),
        )
        if (
            fence.scope_id != GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID
            or fence.fence_version != GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION
            or fence.trigger_manifest_version
            != GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
            or isinstance(fence.revision, bool)
            or not isinstance(fence.revision, int)
            or fence.revision < 0
            or len(fence.incarnation_id) != 64
            or len(fence.mutation_nonce) != 64
            or any(
                character not in "0123456789abcdef"
                for character in fence.incarnation_id + fence.mutation_nonce
            )
        ):
            raise sqlite3.DatabaseError(
                "authoritative source revision singleton is corrupt"
            )
        return fence

    def read_fence(self) -> CommunitySourceRevisionFence:
        started = time.monotonic()
        try:
            database_path = Path(self._db_path_provider()).resolve()
            if not database_path.is_file():
                raise FileNotFoundError(database_path)
            uri_path = quote(database_path.as_posix(), safe="/:")
            connection = sqlite3.connect(
                f"file:{uri_path}?mode=ro",
                uri=True,
                timeout=_SOURCE_FENCE_BUSY_TIMEOUT_MS / 1000,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute(
                    f"PRAGMA busy_timeout={_SOURCE_FENCE_BUSY_TIMEOUT_MS}"
                )
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                fence = self.read_fence_from_connection(connection)
                if time.monotonic() - started > _SOURCE_FENCE_TOTAL_BUDGET_SECONDS:
                    raise TimeoutError("authoritative source fence read timed out")
                connection.execute("COMMIT")
                return fence
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        except CommunityGlobalDiscoveryRecoveryError:
            raise
        except Exception as exc:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_relational_snapshot_unavailable"
            ) from exc

    def read_revision(self) -> int:
        return self.read_fence().revision

    def __call__(self) -> str:
        return self.read_fence().fingerprint()


@dataclass(frozen=True, slots=True)
class CommunityRecoverySnapshotFingerprint:
    """One coherent cheap fence across relational and cognitive inputs."""

    relational: CommunityRelationalRecoverySnapshotFingerprint
    cognitive_overlay: object

    def __call__(self) -> str:
        from okto_pulse.core.ports.global_discovery_recovery_control import (
            global_discovery_recovery_snapshot_fingerprint,
        )

        current_overlay = getattr(self.cognitive_overlay, "current_fingerprint", None)
        if not callable(current_overlay):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_cognitive_snapshot_unavailable"
            )
        try:
            overlay_before = str(current_overlay()).strip()
            relational = self.relational()
            overlay_after = str(current_overlay()).strip()
        except CommunityGlobalDiscoveryRecoveryError:
            raise
        except Exception as exc:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_cognitive_snapshot_unavailable"
            ) from exc
        if not overlay_before or overlay_before != overlay_after:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_cognitive_snapshot_drift"
            )
        return global_discovery_recovery_snapshot_fingerprint(
            relational_revision_fingerprint=relational,
            cognitive_overlay_revision_fingerprint=overlay_before,
        )


class CommunityPreparedRecoveryRevoker:
    """Compose Core's create-only prepared revocation evidence for Community."""

    def __init__(self, *, artifact_store: object) -> None:
        # The artifact semantics stay Core-owned.  Community supplies only the
        # concrete artifact store and exposes the exact runtime boundary needed
        # by its durable SQL dispatcher.
        from okto_pulse.core.ports.global_discovery_recovery_control import (
            GlobalDiscoveryPreparedRevocationService,
        )

        self._revocations = GlobalDiscoveryPreparedRevocationService(
            artifact_store=artifact_store
        )

    def revoke_prepared(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
        revoked_at: datetime,
        requested_by_actor_id: str,
        reason: str | None,
    ) -> object:
        return self._revocations.revoke_prepared(
            run_id=run_id,
            epoch=epoch,
            manifest_ref=manifest_ref,
            revoked_at=revoked_at,
            requested_by_actor_id=requested_by_actor_id,
            reason=reason,
        )

    def is_prepared_revoked(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
    ) -> bool:
        return self._revocations.is_prepared_revoked(
            run_id=run_id,
            epoch=epoch,
            manifest_ref=manifest_ref,
        )

    def resolve_attempt_manifest_ref(
        self,
        *,
        run_id: str,
        epoch: int,
    ) -> str | None:
        return self._revocations.resolve_attempt_manifest_ref(
            run_id=run_id,
            epoch=epoch,
        )


class CommunityGlobalDiscoveryRecoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CommunityGlobalDiscoveryRecoveryFenceError(CommunityGlobalDiscoveryRecoveryError):
    """Preserve the caller's exact fence failure across adapter cleanup."""

    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__("global_discovery_writer_fence_lost")
