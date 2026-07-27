"""Crash-consistent Community recovery for an unreadable Global Discovery DB."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from okto_pulse.community.adapters.global_discovery_layout import (
    GlobalDiscoveryLayoutError,
    canonical_sha256,
    fsync_directory,
    generation_dir,
    generation_graph_path,
    read_active_generation,
    resolve_active_graph_path,
    restore_legacy_generation,
    switch_active_generation,
    validate_generation_id,
    write_generation_manifest,
    write_json_atomic,
)
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.ladybug_writer import ladybug_writer_scope
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
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryCutoverResult,
)
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCorruption,
    GraphLockContention,
    GraphUnavailable,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    GlobalDiscoveryWriterFenceLost,
    recovery_attempt_id,
)


_REQUIRED_SCHEMA_OBJECTS = frozenset({"Board", "DecisionDigest", "CONTAINS_DECISION"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_expected_corrupt_primary_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a narrow expected unreadable/corrupt-primary failure
    that justifies discarding an unpublished adoption candidate and falling back
    to seed rebuild (blocker 6).

    Lock contention (authority), fence loss, and programmer errors are NOT
    expected here and must propagate.
    """

    if isinstance(exc, (CommunityGlobalDiscoveryRecoveryFenceError, GraphLockContention)):
        return False
    if isinstance(exc, (GraphCorruption, GraphUnavailable)):
        return True
    if isinstance(exc, CommunityGlobalDiscoveryRecoveryError):
        return True
    # The corrupt-open RuntimeError raised by
    # ``raise_existing_global_graph_open_failed`` is a plain RuntimeError.
    if type(exc) is RuntimeError and "could not be opened" in str(exc):
        return True
    return False
_JOURNAL_FILENAME = "recovery_journal.json"
_SOURCE_FENCE_BUSY_TIMEOUT_MS = 750
_SOURCE_FENCE_TOTAL_BUDGET_SECONDS = 1.5
# B7: the exact, stable kind for the authoritative-seed rebuild journal (parallels
# the adoption kind ``adopt_complete_primary``).  The seed reconciliation reader
# fails closed unless a seed journal carries EXACTLY this kind.
_SEED_REBUILD_JOURNAL_KIND = "seed_rebuild"
# B6.5: the exact, stable kind for the reconciling successor (epoch N+1) journal
# that finishes a bound predecessor's already-crossed physical truth.  It is the
# completed-first crash floor for the reconciling attempt and the ONLY evidence
# from which the successor builds RecoveryPhysicalTruth(attempt_id=N+1).
_RECONCILE_PREDECESSOR_CUTOVER_KIND = "reconcile_predecessor_cutover"


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
        actual_triggers = {
            str(trigger["name"]): trigger for trigger in trigger_rows
        }
        if set(actual_triggers) != set(expected_triggers):
            raise sqlite3.DatabaseError(
                "authoritative source revision trigger manifest is incomplete"
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            trigger = actual_triggers[trigger_name]
            if (
                str(trigger["tbl_name"]) != table_name
                or normalize_global_discovery_source_revision_trigger_sql(
                    trigger["sql"]
                )
                != normalize_global_discovery_source_revision_trigger_sql(
                    trigger_sql
                )
            ):
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
                if (
                    time.monotonic() - started
                    > _SOURCE_FENCE_TOTAL_BUDGET_SECONDS
                ):
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


class CommunityGlobalDiscoveryRecoveryFenceError(
    CommunityGlobalDiscoveryRecoveryError
):
    """Preserve the caller's exact fence failure across adapter cleanup."""

    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__("global_discovery_writer_fence_lost")


def _assert_fenced(fence_check: Callable[[], None] | None) -> None:
    if fence_check is None:
        return
    try:
        fence_check()
    except CommunityGlobalDiscoveryRecoveryFenceError:
        raise
    except Exception as exc:
        raise CommunityGlobalDiscoveryRecoveryFenceError(exc) from exc


# R3: close-time errors that signal LOST AUTHORITY — fence loss, writer-authority
# loss, and single-writer lock contention.  A close may checkpoint/flush WAL, so
# a close raising one of these has attempted to mutate without authority and MUST
# surface; it is never suppressed as "benign cleanup" on any path.
_AUTHORITY_CLOSE_ERRORS = (
    CommunityGlobalDiscoveryRecoveryFenceError,
    GlobalDiscoveryWriterFenceLost,
    GraphLockContention,
)


def _artifact_paths(
    path: Path,
    *,
    fence_check: Callable[[], None] | None = None,
) -> tuple[Path, ...]:
    """Enumerate the primary plus every concrete engine sidecar."""

    rows: list[Path] = []
    _assert_fenced(fence_check)
    if path.is_file():
        rows.append(path)
    _assert_fenced(fence_check)
    if path.parent.exists():
        _assert_fenced(fence_check)
        for candidate in sorted(path.parent.glob(path.name + ".*")):
            _assert_fenced(fence_check)
            if candidate.is_file():
                rows.append(candidate)
    return tuple(rows)


def _snapshot(
    path: Path,
    *,
    fence_check: Callable[[], None] | None = None,
) -> GlobalDiscoveryArtifactSnapshot:
    digest = hashlib.sha256()
    total_bytes = 0
    paths = _artifact_paths(path, fence_check=fence_check)
    try:
        for candidate in paths:
            _assert_fenced(fence_check)
            before = candidate.stat()
            suffix = candidate.name[len(path.name) :]
            digest.update(len(suffix).to_bytes(4, "big"))
            digest.update(suffix.encode("utf-8"))
            _assert_fenced(fence_check)
            with candidate.open("rb") as stream:
                while True:
                    _assert_fenced(fence_check)
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    digest.update(chunk)
            _assert_fenced(fence_check)
            after = candidate.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_artifact_changed_during_snapshot"
                )
    except OSError as exc:
        raise CommunityGlobalDiscoveryRecoveryError(
            "global_discovery_artifact_snapshot_failed"
        ) from exc
    _assert_fenced(fence_check)
    exists = path.is_file()
    return GlobalDiscoveryArtifactSnapshot(
        exists=exists,
        artifact_count=len(paths),
        total_bytes=total_bytes,
        sha256=digest.hexdigest(),
    )


def _fsync_artifacts(
    path: Path,
    *,
    fence_check: Callable[[], None] | None = None,
) -> None:
    for candidate in _artifact_paths(path, fence_check=fence_check):
        _assert_fenced(fence_check)
        with candidate.open("r+b") as stream:
            os.fsync(stream.fileno())


def _copy_artifacts(
    source: Path,
    destination: Path,
    *,
    fence_check: Callable[[], None] | None = None,
) -> None:
    _assert_fenced(fence_check)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for candidate in _artifact_paths(source, fence_check=fence_check):
        suffix = candidate.name[len(source.name) :]
        target = destination.with_name(destination.name + suffix)
        tmp = target.with_name("." + target.name + ".copying")
        try:
            _assert_fenced(fence_check)
            with candidate.open("rb") as source_stream, tmp.open(
                "wb"
            ) as target_stream:
                while True:
                    _assert_fenced(fence_check)
                    chunk = source_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    _assert_fenced(fence_check)
                    target_stream.write(chunk)
            _assert_fenced(fence_check)
            shutil.copystat(candidate, tmp)
            _assert_fenced(fence_check)
            with tmp.open("r+b") as stream:
                os.fsync(stream.fileno())
            _assert_fenced(fence_check)
            os.replace(tmp, target)
        finally:
            try:
                _assert_fenced(fence_check)
                tmp.unlink()
            except FileNotFoundError:
                pass


def _remove_tree_fenced(
    root: Path,
    *,
    fence_check: Callable[[], None],
) -> None:
    """Delete a tree incrementally so a renewed exact fence bounds each unlink."""

    _assert_fenced(fence_check)
    with os.scandir(root) as iterator:
        entries = list(iterator)
    for entry in entries:
        _assert_fenced(fence_check)
        child = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            _remove_tree_fenced(child, fence_check=fence_check)
        else:
            child.unlink()
    _assert_fenced(fence_check)
    root.rmdir()


def _journal_binding(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "journal_sha256"}


def _write_journal_with_directory_fsync(
    path: Path,
    payload: dict[str, object],
    *,
    fence_check: Callable[[], None] | None = None,
) -> bool:
    binding = _journal_binding(payload)
    _assert_fenced(fence_check)
    return write_json_atomic(
        path,
        {**binding, "journal_sha256": canonical_sha256(binding)},
    )


def _write_journal(path: Path, payload: dict[str, object]) -> bool:
    """Write one valid journal; return success independently of fsync support."""

    _write_journal_with_directory_fsync(path, payload)
    return True


def _read_journal(
    path: Path,
    run_id: str,
    *,
    epoch: int | None = None,
    attempt_id: str | None = None,
    fence_check: Callable[[], None] | None = None,
) -> dict[str, object] | None:
    _assert_fenced(fence_check)
    if not path.exists():
        return None
    try:
        _assert_fenced(fence_check)
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise CommunityGlobalDiscoveryRecoveryError(
            "global_discovery_recovery_journal_unreadable"
        ) from exc
    if not isinstance(raw, dict):
        raise CommunityGlobalDiscoveryRecoveryError(
            "global_discovery_recovery_journal_invalid"
        )
    binding = _journal_binding(raw)
    identity_matches = raw.get("run_id") == run_id
    if epoch is not None:
        identity_matches = identity_matches and raw.get("epoch") == epoch
    if attempt_id is not None:
        identity_matches = identity_matches and raw.get("attempt_id") == attempt_id
    if not identity_matches or raw.get("journal_sha256") != canonical_sha256(binding):
        raise CommunityGlobalDiscoveryRecoveryError(
            "global_discovery_recovery_journal_hash_mismatch"
        )
    return raw


def _physical_generation_id(*, run_id: str, attempt_id: str, epoch: int) -> str:
    """Map a durable attempt identity to one safe, collision-resistant directory."""

    readable = f"{run_id}_attempt_{epoch}"
    try:
        return validate_generation_id(readable)
    except GlobalDiscoveryLayoutError:
        digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
        return validate_generation_id(f"gdr_attempt_{digest}")


class CommunityGlobalDiscoveryRecovery:
    """Materialize a versioned generation and commit one atomic pointer file."""

    def __init__(
        self,
        *,
        global_runtime: CommunityGlobalDiscoveryRuntime,
        graph_path_provider: Callable[[], Path] | None = None,
        runtime_factory: Callable[[Path], CommunityGlobalDiscoveryRuntime]
        | None = None,
        fence_check: Callable[[], None] | None = None,
        snapshot_fingerprint_provider: Callable[[], str] | None = None,
    ) -> None:
        self._global_runtime = global_runtime
        self._graph_path_provider = graph_path_provider
        self._runtime_factory = runtime_factory or self._default_runtime_factory
        self._fence_check = fence_check
        self._snapshot_fingerprint_provider = snapshot_fingerprint_provider

    def bind_snapshot_fingerprint_provider(
        self,
        provider: Callable[[], str],
    ) -> None:
        """Bind the relational freshness fence once, before publication.

        The graph provider registry is assembled before the relational schema
        lifecycle runs.  Runtime composition therefore performs this single
        late binding after ``init_db`` and before registering the control plane.
        Rebinding to a different provider fails closed.
        """

        if not callable(provider):
            raise TypeError("snapshot fingerprint provider must be callable")
        current = self._snapshot_fingerprint_provider
        if current is not None and current is not provider:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_snapshot_fingerprint_already_bound"
            )
        self._snapshot_fingerprint_provider = provider

    def current_snapshot_fingerprint(self) -> str:
        """Read only the authoritative relational board/source snapshot."""

        provider = self._snapshot_fingerprint_provider
        if provider is None:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_snapshot_fingerprint_unavailable"
            )
        try:
            fingerprint = str(provider()).strip()
        except CommunityGlobalDiscoveryRecoveryError:
            raise
        except Exception as exc:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_snapshot_fingerprint_unavailable"
            ) from exc
        if not fingerprint:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_snapshot_fingerprint_unavailable"
            )
        return fingerprint

    def _legacy_path(self) -> Path:
        if self._graph_path_provider is not None:
            return Path(self._graph_path_provider()).resolve()
        return self._global_runtime._legacy_global_graph_path()  # noqa: SLF001

    def _live_path(self) -> Path:
        return resolve_active_graph_path(self._legacy_path())

    def _default_runtime_factory(self, path: Path) -> CommunityGlobalDiscoveryRuntime:
        return CommunityGlobalDiscoveryRuntime(
            graph_runtime=self._global_runtime._runtime(),  # noqa: SLF001
            graph_path_provider=lambda: path,
        )

    def inspect_live_artifact(self) -> GlobalDiscoveryArtifactSnapshot:
        # Pointer + bytes only; never ask Ladybug to open the corrupt live store.
        return _snapshot(self._live_path())

    @staticmethod
    def _scalar_count(
        runtime: CommunityGlobalDiscoveryRuntime, query: str, params
    ) -> int:
        result = runtime.execute(query, params)
        if len(result.rows) != 1 or len(result.rows[0]) != 1:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_validation_shape_invalid"
            )
        return int(result.rows[0][0])

    @staticmethod
    def _embedding_sha256(value: object) -> str:
        """Hash the IEEE-754 values that Ladybug persists in ``DOUBLE[]``.

        JSON text is not a safe canonical form for vectors returned by different
        drivers.  Packing normalized Python doubles makes candidate and fresh
        readback comparisons stable across their textual representations while
        still detecting every persisted bit-level value change.  Signed zero is
        normalized because it is semantically equal in the graph model.
        """

        try:
            values = tuple(float(item) for item in value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_embedding_invalid"
            ) from exc
        if any(not math.isfinite(item) for item in values):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_embedding_invalid"
            )
        digest = hashlib.sha256()
        digest.update(len(values).to_bytes(8, "big"))
        for item in values:
            digest.update(struct.pack("!d", 0.0 if item == 0.0 else item))
        return digest.hexdigest()

    @classmethod
    def _expected_semantic_projection(
        cls,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
    ) -> dict[str, object]:
        projected_boards: list[dict[str, object]] = []
        projected_digests: list[dict[str, object]] = []
        projected_links: list[dict[str, object]] = []
        digest_id_owners: dict[str, tuple[str, str]] = {}
        for board in boards:
            projected_boards.append(
                {
                    "board_id": board.board_id,
                    "name": board.board_name or board.board_id,
                    "summary": board.summary,
                    "decision_count": len(board.digests),
                    "summary_embedding_sha256": cls._embedding_sha256(
                        board.summary_embedding
                    ),
                }
            )
            for digest in board.digests:
                digest_id = f"dd_{board.board_id[:8]}_{digest.original_node_id}"
                owner = (board.board_id, digest.original_node_id)
                previous_owner = digest_id_owners.setdefault(digest_id, owner)
                if previous_owner != owner:
                    # The id formula is part of the existing runtime contract;
                    # recovery cannot migrate it unilaterally.  Fail closed when
                    # two authoritative seeds would alias that global identity.
                    raise CommunityGlobalDiscoveryRecoveryError(
                        "global_discovery_candidate_digest_id_collision"
                    )
                projected_digests.append(
                    {
                        "id": digest_id,
                        "board_id": board.board_id,
                        "original_node_id": digest.original_node_id,
                        "title": digest.title,
                        "one_line_summary": digest.summary,
                        "node_type": digest.node_type,
                        "graph_layer": digest.graph_layer,
                        "embedding_sha256": cls._embedding_sha256(digest.embedding),
                    }
                )
                projected_links.append(
                    {
                        "board_id": board.board_id,
                        "digest_id": digest_id,
                        "digest_board_id": board.board_id,
                        "original_node_id": digest.original_node_id,
                    }
                )
        projected_boards.sort(key=lambda row: str(row["board_id"]))
        projected_digests.sort(
            key=lambda row: (
                str(row["id"]),
                str(row["board_id"]),
                str(row["original_node_id"]),
            )
        )
        projected_links.sort(
            key=lambda row: (
                str(row["board_id"]),
                str(row["digest_id"]),
                str(row["digest_board_id"]),
                str(row["original_node_id"]),
            )
        )
        return {
            "boards": projected_boards,
            "digests": projected_digests,
            "links": projected_links,
        }

    @classmethod
    def _actual_semantic_projection(
        cls,
        runtime: CommunityGlobalDiscoveryRuntime,
        *,
        fence_check: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        _assert_fenced(fence_check)
        board_rows = runtime.execute(
            "MATCH (b:Board) RETURN b.board_id, b.name, b.summary, "
            "b.decision_count, b.summary_embedding",
            {},
        ).rows
        _assert_fenced(fence_check)
        digest_rows = runtime.execute(
            "MATCH (d:DecisionDigest) RETURN d.id, d.board_id, "
            "d.original_node_id, d.title, d.one_line_summary, d.node_type, "
            "coalesce(d.graph_layer, 'legacy_unknown'), d.embedding",
            {},
        ).rows
        _assert_fenced(fence_check)
        link_rows = runtime.execute(
            "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) "
            "RETURN b.board_id, d.id, d.board_id, d.original_node_id",
            {},
        ).rows
        try:
            projected_boards = [
                {
                    "board_id": str(row[0]),
                    "name": str(row[1]),
                    "summary": str(row[2]),
                    "decision_count": int(row[3]),
                    "summary_embedding_sha256": cls._embedding_sha256(row[4]),
                }
                for row in board_rows
            ]
            projected_digests = [
                {
                    "id": str(row[0]),
                    "board_id": str(row[1]),
                    "original_node_id": str(row[2]),
                    "title": str(row[3]),
                    "one_line_summary": str(row[4]),
                    "node_type": str(row[5]),
                    "graph_layer": str(row[6] or "legacy_unknown"),
                    "embedding_sha256": cls._embedding_sha256(row[7]),
                }
                for row in digest_rows
            ]
            projected_links = [
                {
                    "board_id": str(row[0]),
                    "digest_id": str(row[1]),
                    "digest_board_id": str(row[2]),
                    "original_node_id": str(row[3]),
                }
                for row in link_rows
            ]
        except (IndexError, TypeError, ValueError) as exc:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_validation_shape_invalid"
            ) from exc
        projected_boards.sort(key=lambda row: str(row["board_id"]))
        projected_digests.sort(
            key=lambda row: (
                str(row["id"]),
                str(row["board_id"]),
                str(row["original_node_id"]),
            )
        )
        projected_links.sort(
            key=lambda row: (
                str(row["board_id"]),
                str(row["digest_id"]),
                str(row["digest_board_id"]),
                str(row["original_node_id"]),
            )
        )
        return {
            "boards": projected_boards,
            "digests": projected_digests,
            "links": projected_links,
        }

    @classmethod
    def _validate_runtime(
        cls,
        runtime: CommunityGlobalDiscoveryRuntime,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        *,
        fence_check: Callable[[], None] | None = None,
    ) -> tuple[int, dict[str, dict[str, int]], str]:
        _assert_fenced(fence_check)
        schema = set(runtime.list_schema_objects())
        if _REQUIRED_SCHEMA_OBJECTS - schema:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_schema_missing"
            )
        expected_projection = cls._expected_semantic_projection(boards)
        actual_projection = cls._actual_semantic_projection(
            runtime,
            fence_check=fence_check,
        )
        if actual_projection != expected_projection:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_semantic_mismatch"
            )
        counts = {
            board.board_id: {
                "boards": 1,
                "digests": len(board.digests),
                "links": len(board.digests),
            }
            for board in boards
        }
        return len(schema), counts, canonical_sha256(expected_projection)

    @classmethod
    def _validate_and_project_self(
        cls,
        runtime: CommunityGlobalDiscoveryRuntime,
        *,
        fence_check: Callable[[], None] | None = None,
    ) -> tuple[int, dict[str, dict[str, int]], str, dict[str, object]]:
        """Validate a genuinely complete primary and derive its OWN projection.

        Unlike ``_validate_runtime`` (which compares against seeds), this proves
        the graph is a complete/readable Global Discovery primary and captures
        its self-derived semantic projection/fingerprint + counts.  A partial
        primary (missing required schema) or an empty graph raises, so the caller
        falls back to authoritative-seed rebuild for R1/R2.
        """

        _assert_fenced(fence_check)
        schema = set(runtime.list_schema_objects())
        if _REQUIRED_SCHEMA_OBJECTS - schema:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_complete_primary_schema_missing"
            )
        projection = cls._actual_semantic_projection(runtime, fence_check=fence_check)

        def _incoherent(reason: str) -> CommunityGlobalDiscoveryRecoveryError:
            return CommunityGlobalDiscoveryRecoveryError(
                f"global_discovery_complete_primary_incoherent:{reason}"
            )

        boards = projection["boards"]
        digests = projection["digests"]
        links = projection["links"]
        if not boards or not digests:
            raise _incoherent("empty")

        # Unique, non-empty board and digest identities.
        board_ids = [str(b["board_id"]) for b in boards]
        if any(not bid for bid in board_ids) or len(board_ids) != len(set(board_ids)):
            raise _incoherent("board_identity")
        board_set = set(board_ids)
        digest_ids = [str(d["id"]) for d in digests]
        if any(not did for did in digest_ids) or len(digest_ids) != len(set(digest_ids)):
            raise _incoherent("digest_identity")
        digest_by_id = {str(d["id"]): d for d in digests}
        # No duplicate (board_id, original_node_id) semantic identities.
        semantic_ids = [
            (str(d["board_id"]), str(d["original_node_id"])) for d in digests
        ]
        if len(semantic_ids) != len(set(semantic_ids)):
            raise _incoherent("duplicate_semantic_identity")

        # Every digest's board exists (no orphan digests).
        for digest in digests:
            if str(digest["board_id"]) not in board_set:
                raise _incoherent("orphan_digest")

        # Every digest is contained exactly once, with agreeing endpoints/ownership.
        link_digest_ids = [str(link["digest_id"]) for link in links]
        if len(link_digest_ids) != len(set(link_digest_ids)):
            raise _incoherent("duplicate_containment")
        if set(link_digest_ids) != set(digest_ids):
            raise _incoherent("containment_cardinality")
        for link in links:
            digest = digest_by_id.get(str(link["digest_id"]))
            if digest is None:
                raise _incoherent("dangling_link")
            if str(link["board_id"]) not in board_set:
                raise _incoherent("link_board_missing")
            if str(link["digest_board_id"]) != str(digest["board_id"]):
                raise _incoherent("link_ownership")
            if str(link["board_id"]) != str(digest["board_id"]):
                raise _incoherent("link_containment_board")
            if str(link["original_node_id"]) != str(digest["original_node_id"]):
                raise _incoherent("link_identity")

        # Per-board decision_count agrees with the digest/link population.
        digests_per_board: dict[str, int] = {}
        for digest in digests:
            digests_per_board[str(digest["board_id"])] = (
                digests_per_board.get(str(digest["board_id"]), 0) + 1
            )
        counts: dict[str, dict[str, int]] = {}
        for board in boards:
            bid = str(board["board_id"])
            per_board = digests_per_board.get(bid, 0)
            if int(board["decision_count"]) != per_board:
                raise _incoherent("decision_count")
            counts[bid] = {"boards": 1, "digests": per_board, "links": per_board}
        return len(schema), counts, canonical_sha256(projection), projection

    @staticmethod
    def _materialize(
        runtime: CommunityGlobalDiscoveryRuntime,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        *,
        fence_check: Callable[[], None] | None = None,
    ) -> None:
        # Validate the legacy deterministic identity before the first write.
        CommunityGlobalDiscoveryRecovery._expected_semantic_projection(boards)
        synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        for board in boards:
            _assert_fenced(fence_check)
            runtime.upsert_board_summary(
                board_id=board.board_id,
                name=board.board_name or board.board_id,
                summary=board.summary,
                summary_embedding=list(board.summary_embedding),
                decision_count=len(board.digests),
                synced_at=synced_at,
            )
            for digest in board.digests:
                digest_id = f"dd_{board.board_id[:8]}_{digest.original_node_id}"
                _assert_fenced(fence_check)
                runtime.upsert_decision_digest(
                    digest_id=digest_id,
                    board_id=board.board_id,
                    original_node_id=digest.original_node_id,
                    title=digest.title,
                    summary=digest.summary,
                    node_type=digest.node_type,
                    graph_layer=digest.graph_layer,
                    embedding=list(digest.embedding),
                    created_at=synced_at,
                )
                _assert_fenced(fence_check)
                runtime.link_board_digest(
                    board_id=board.board_id,
                    digest_id=digest_id,
                )

    @staticmethod
    def _result_from_journal(
        journal: dict[str, object],
    ) -> GlobalDiscoveryCutoverResult:
        return GlobalDiscoveryCutoverResult(
            outcome=str(journal["outcome"]),
            candidate_sha256=str(journal.get("candidate_sha256") or ""),
            quarantine_ref=str(journal.get("quarantine_ref") or "") or None,
            schema_object_count=int(journal.get("schema_object_count") or 0),
            rollback_performed=bool(journal.get("rollback_performed", False)),
            failure_code=(
                str(journal["failure_code"])
                if journal.get("failure_code") is not None
                else None
            ),
            directory_fsync_supported=bool(
                journal.get("directory_fsync_supported", False)
            ),
            cutover_atomicity="atomic_generation_pointer_replace",
            recovery_journal_ref=str(journal.get("quarantine_ref") or "") or None,
        )

    def _restore_previous(
        self,
        *,
        legacy: Path,
        previous_generation_id: str | None,
        previous_manifest_sha256: str | None,
        fence_check: Callable[[], None],
    ) -> bool:
        fence_check()
        if previous_generation_id is None:
            return restore_legacy_generation(legacy)
        if previous_manifest_sha256 is None:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_previous_generation_manifest_missing"
            )
        return switch_active_generation(
            legacy,
            generation_id=previous_generation_id,
            manifest_sha256=previous_manifest_sha256,
        )

    def reconcile_attempt_artifacts(
        self,
        *,
        run_id: str,
        known_attempt_ids: tuple[str, ...],
        now: datetime,
        fence_check: Callable[[], None],
    ) -> CommunityRecoveryAttemptReconciliation:
        """Prune superseded attempts without adopting unknown artifacts.

        Attempt identities expose a monotonic epoch but no separate active-id
        field, so the highest known epoch is the current attempt and is never
        considered for pruning.  For superseded artifacts, retain whichever of
        the latest-three policy or the 24-hour policy keeps fewer directories;
        ties deterministically prefer the latest-three policy.  Artifacts with
        no trustworthy terminal timestamp remain retained fail-closed.
        """

        normalized_run_id = validate_generation_id(run_id)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        known: dict[int, str] = {}
        for raw_attempt_id in known_attempt_ids:
            attempt_id = str(raw_attempt_id)
            prefix = f"{normalized_run_id}/attempt-"
            if not attempt_id.startswith(prefix):
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_attempt_identity_invalid"
                )
            raw_epoch = attempt_id[len(prefix) :]
            if not raw_epoch.isdigit() or int(raw_epoch) < 1:
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_attempt_identity_invalid"
                )
            epoch = int(raw_epoch)
            if attempt_id != recovery_attempt_id(normalized_run_id, epoch):
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_attempt_identity_invalid"
                )
            if epoch in known:
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_attempt_identity_invalid"
                )
            known[epoch] = attempt_id

        _assert_fenced(fence_check)
        root = (
            self._legacy_path().parent
            / "quarantine"
            / "global-discovery"
            / normalized_run_id
        )
        _assert_fenced(fence_check)
        if not root.exists():
            return CommunityRecoveryAttemptReconciliation()
        _assert_fenced(fence_check)
        resolved_root = root.resolve(strict=True)
        unknown: list[str] = []
        known_names = {f"attempt-{epoch}" for epoch in known}
        _assert_fenced(fence_check)
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if child.name not in known_names:
                unknown.append(f"{normalized_run_id}/{child.name}")

        active_epoch = max(known, default=None)
        retention_cutoff = now - timedelta(hours=24)
        directories: dict[int, Path] = {}
        terminal_times: dict[int, datetime | None] = {}
        for epoch, attempt_id in sorted(known.items()):
            directory = root / f"attempt-{epoch}"
            _assert_fenced(fence_check)
            if not directory.exists():
                continue
            try:
                _assert_fenced(fence_check)
                resolved_directory = directory.resolve(strict=True)
                resolved_directory.relative_to(resolved_root)
            except (OSError, ValueError):
                unknown.append(attempt_id)
                continue
            _assert_fenced(fence_check)
            if directory.is_symlink() or resolved_directory.parent != resolved_root:
                unknown.append(attempt_id)
                continue
            try:
                journal = _read_journal(
                    directory / _JOURNAL_FILENAME,
                    normalized_run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                    fence_check=fence_check,
                )
            except CommunityGlobalDiscoveryRecoveryFenceError:
                raise
            except CommunityGlobalDiscoveryRecoveryError:
                unknown.append(attempt_id)
                continue
            terminal_at: datetime | None = None
            if journal is not None and journal.get("phase") in {
                "completed",
                "rolled_back",
            }:
                terminal_field = (
                    "completed_at"
                    if journal.get("phase") == "completed"
                    else "rolled_back_at"
                )
                raw_terminal_at = journal.get(terminal_field)
                try:
                    terminal_at = datetime.fromisoformat(str(raw_terminal_at))
                except (TypeError, ValueError):
                    terminal_at = None
                if (
                    terminal_at is not None
                    and (terminal_at.tzinfo is None or terminal_at.utcoffset() is None)
                ):
                    terminal_at = None
            directories[epoch] = resolved_directory
            terminal_times[epoch] = terminal_at

        superseded_epochs = set(directories) - {active_epoch}
        mandatory_epochs = {
            epoch
            for epoch in superseded_epochs
            if terminal_times[epoch] is None
        }
        latest_policy = mandatory_epochs | set(sorted(superseded_epochs)[-3:])
        younger_policy = mandatory_epochs | {
            epoch
            for epoch in superseded_epochs
            if terminal_times[epoch] is not None
            and terminal_times[epoch] >= retention_cutoff
        }
        retained_superseded = (
            latest_policy
            if len(latest_policy) <= len(younger_policy)
            else younger_policy
        )

        retained: list[str] = []
        deleted: list[str] = []
        for epoch, resolved_directory in sorted(directories.items()):
            attempt_id = known[epoch]
            if (
                epoch == active_epoch
                or terminal_times[epoch] is None
                or epoch in retained_superseded
            ):
                retained.append(attempt_id)
                continue
            _remove_tree_fenced(
                resolved_directory,
                fence_check=fence_check,
            )
            deleted.append(attempt_id)
        return CommunityRecoveryAttemptReconciliation(
            quarantined_ids=tuple(sorted(set(unknown))),
            retained_ids=tuple(retained),
            deleted_ids=tuple(deleted),
        )

    @staticmethod
    def _write_journal_sticky_false(
        journal_path: Path,
        payload: dict,
        *,
        aggregate: bool,
        fence_check: Callable[[], None],
    ) -> bool:
        """R4 sticky-false, crash-window-proof self-attestation.

        A single atomic replace CANNOT self-attest its own directory fsync: the
        fsync result is only known AFTER the bytes are already on disk, so any
        payload that encodes an optimistic ``true`` is a hazard — a crash right
        after the replace (before any corrective write) leaves that unattested
        ``true`` readable.  A two-write correction does not close this window.

        Therefore this writer NEVER persists an optimistic aggregate: it writes
        ``directory_fsync_supported=False`` UNCONDITIONALLY (a persisted ``false``
        can never be wrong, and is never upgraded to ``true`` later).  The real
        per-write durability is returned IN-PROCESS ONLY as
        ``aggregate AND <this write's own directory fsync>`` for the current
        result; it is never written back to disk.  A fresh reader/resume of this
        boundary therefore can only ever read a conservative ``false``.
        """

        supported = _write_journal_with_directory_fsync(
            journal_path,
            {**payload, "directory_fsync_supported": False},
            fence_check=fence_check,
        )
        return bool(aggregate) and bool(supported)

    def _persist_pending_completed_journal(
        self,
        *,
        journal_path: Path,
        completed_journal: dict,
        fence_check: Callable[[], None],
    ) -> bool:
        """R4 step 1: persist the completed journal with an explicit
        ``clear_settled=False`` PENDING flag and a CONSERVATIVE
        ``directory_fsync_supported=False`` BEFORE any physical marker clear.

        The pending on-disk record must NEVER contain an optimistic aggregate
        whose own atomic-write fsync result exists only in memory: recording
        ``false`` here means a crash after the physical clear but before the final
        settle leaves a conservative ``false`` that a resume reads (never a
        rebound to ``true``).  The real completion aggregate is carried IN MEMORY
        by the caller's ``pre_clear`` and reaches only the durable settle write.
        Still phase=completed, so COMPLETED-FIRST + the rollback exclusion hold.
        Returns the write's own directory-fsync support so callers may fold it
        into the in-memory completion aggregate.
        """

        _assert_fenced(fence_check)
        pending = {
            **completed_journal,
            "directory_fsync_supported": False,
            "clear_settled": False,
        }
        return _write_journal_with_directory_fsync(
            journal_path, pending, fence_check=fence_check
        )

    def _clear_marker_and_settle(
        self,
        *,
        legacy: Path,
        generation_id: str,
        journal_path: Path,
        completed_journal: dict,
        pre_clear_supported: bool,
        fence_check: Callable[[], None],
        swallow_late_fence_loss: bool = False,
    ) -> bool:
        """R4 steps 2-3: physically clear the marker (its directory-fsync support
        is unknown until it returns), then persist the completed journal with the
        FINAL aggregate (``pre_clear AND clear_supported``) and ``clear_settled=True``.

        MUST be preceded by ``_persist_pending_completed_journal`` so a crash
        mid-clear reads ``clear_settled=False`` (conservative) on the next resume.
        The settle write folds its OWN directory fsync into the persisted+returned
        value (``_write_journal_folded_fsync``): a settle that records
        ``clear_settled=True`` but whose own fsync is unsupported can never leave
        an optimistic ``true`` for this process's result OR a fresh-process no-op.

        ``swallow_late_fence_loss`` preserves the main-flow contract that a LATE
        fence/lease loss AFTER the durable completed journal cannot mask success:
        the clear is deferred to idempotent resume and the conservative ``false``
        (already on disk) is reported.
        """

        try:
            _assert_fenced(fence_check)
            clear_supported = self._global_runtime.note_successful_generation_cutover(
                active_path=generation_graph_path(legacy, generation_id),
                fence_check=fence_check,
            )
        except (
            CommunityGlobalDiscoveryRecoveryFenceError,
            GlobalDiscoveryWriterFenceLost,
        ):
            if not swallow_late_fence_loss:
                raise
            return False

        aggregate = bool(pre_clear_supported) and bool(clear_supported)
        # Sticky-false: the settle persists directory_fsync_supported=False
        # (never an unattested optimistic true a fresh no-op could trust) while
        # the current process's result folds the settle write's own fsync.
        return self._write_journal_sticky_false(
            journal_path,
            {**completed_journal, "clear_settled": True},
            aggregate=aggregate,
            fence_check=fence_check,
        )

    def _clear_marker_crash_conservatively(
        self,
        *,
        legacy: Path,
        generation_id: str,
        journal_path: Path,
        completed_journal: dict,
        pre_clear_supported: bool,
        fence_check: Callable[[], None],
        swallow_late_fence_loss: bool = False,
    ) -> bool:
        """Crash-conservative COMPLETED-FIRST marker clear (R4): persist a pending
        (``clear_settled=False``) completed journal BEFORE the physical clear,
        clear, then persist the FINAL aggregate with ``clear_settled=True``.
        Returns the final aggregate.  A marker-absent resume reports ``false``
        unless ``clear_settled`` is durably true, so every crash point defaults
        ``false`` until the clear plus its durability are both recorded.  Callers
        that must key a structural rollback exclusion on the durable completed
        phase between the pending write and the clear call the two sub-steps
        directly instead.  The pending write's own directory fsync is folded into
        the completion aggregate (matching the pre-R4 completed-journal fold)."""

        pending_write_supported = self._persist_pending_completed_journal(
            journal_path=journal_path,
            completed_journal=completed_journal,
            fence_check=fence_check,
        )
        return self._clear_marker_and_settle(
            legacy=legacy,
            generation_id=generation_id,
            journal_path=journal_path,
            completed_journal=completed_journal,
            pre_clear_supported=bool(pre_clear_supported)
            and bool(pending_write_supported),
            fence_check=fence_check,
            swallow_late_fence_loss=swallow_late_fence_loss,
        )

    def _reconcile_completed_and_clear_marker(
        self,
        *,
        journal: dict,
        legacy: Path,
        ordered: tuple[GlobalDiscoveryBoardSeed, ...],
        journal_path: Path,
        fence_check: Callable[[], None],
    ) -> bool:
        """Idempotent COMPLETED-FIRST reconcile + authority-checked marker clear.

        Single source of truth for the two rare ``completed + marker present``
        RESUME entry points: ``rebuild_candidate_and_cutover``'s resume branch
        and ``reconcile_attempt_terminal_truth`` (Nexus
        msg_08f6fa2df8ab4e728144e12e30ca7c67 / blocker 9+11).  The normal
        first-completion main flow does NOT route through here — it already
        re-conquered the generation inline and calls
        ``note_successful_generation_cutover`` directly.

        Pointer identity is only a name check and is insufficient.  Under the
        exact fence this re-conquers the cutover evidence:

        1. probe the durable marker FIRST (blocker 13) and return immediately if
           it is absent — zero runtime construction/validation/fsync/clear, so a
           healthy second resume is a true no-op that cannot reject on legitimate
           later WAL growth;
        2. resolve the active generation (name must match the journal);
        3. snapshot the active graph and require SHA-256 == terminal
           ``candidate_sha256``;
        4. instantiate a fresh runtime and require fresh schema-object-count,
           counts-by-board AND semantic fingerprint to all equal the terminal
           journal evidence (blocker 31 — not the fingerprint alone);
        5. fsync artifacts;
        6. revalidate the exact fence immediately before clear (the clear helper
           revalidates once more just before the physical unlink).

        Any mismatch/corruption raises and performs ZERO clear on every attempt,
        so the marker is preserved and the next process stays conservatively
        unreadable.
        """

        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            bootstrap_marker_present,
        )

        _assert_fenced(fence_check)
        if not bootstrap_marker_present(legacy):
            # Marker absent: no physical clear to perform.  R4: report the durable
            # truth, never an unconditional true.  Only a journal whose clear was
            # DURABLY SETTLED (``clear_settled`` EXACT ``True`` — a JSON string
            # ``"false"`` is NOT coerced truthy) may report its recorded aggregate;
            # a pending/unknown clear (crash between the physical clear and the
            # final settle) reports false.  ``directory_fsync_supported`` must
            # likewise be an exact ``True`` bool, never a truthy non-bool.
            if journal.get("clear_settled") is not True:
                return False
            return journal.get("directory_fsync_supported") is True

        expected_candidate_sha = str(journal.get("candidate_sha256") or "")
        expected_semantic = str(journal.get("semantic_fingerprint") or "")
        expected_schema_count = journal.get("schema_object_count")
        expected_counts = journal.get("counts_by_board") or {}
        generation_id = str(journal.get("generation_id") or "")
        expected_manifest_sha = str(journal.get("generation_manifest_sha256") or "")
        # R8-B7.2: for the SEED path the terminal evidence is MANDATORY — a seed
        # journal missing generation/manifest/schema/counts/semantic evidence
        # must never reach the optional-comparison branches below (marker stays
        # present, zero clear).
        if str(journal.get("kind") or "") == _SEED_REBUILD_JOURNAL_KIND and (
            not generation_id
            or not expected_manifest_sha
            or not expected_semantic
            or expected_schema_count is None
            or not expected_counts
        ):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_seed_terminal_evidence_missing"
            )

        _assert_fenced(fence_check)
        active = read_active_generation(legacy)
        if active is None or (
            generation_id and active.generation_id != generation_id
        ):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_completed_generation_mismatch"
            )
        # Blocker 6: require the active pointer's manifest SHA to bind exactly to
        # the terminal journal's manifest SHA (not just the generation name).
        if expected_manifest_sha and active.manifest_sha256 != expected_manifest_sha:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_completed_manifest_mismatch"
            )

        _assert_fenced(fence_check)
        snapshot = _snapshot(active.graph_path, fence_check=fence_check)
        if (
            not snapshot.exists
            or not expected_candidate_sha
            or snapshot.sha256 != expected_candidate_sha
        ):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_completed_candidate_sha_mismatch"
            )

        _assert_fenced(fence_check)
        adopted = str(journal.get("kind") or "") == "adopt_complete_primary"
        # R8-B7.6 (ruling): the resume validation must be TRULY NON-MUTATING for
        # the active artifacts — Ladybug's close may checkpoint/grow the WAL it
        # opened, which would move the persisted bytes away from the terminal
        # candidate SHA and permanently brick a legitimate resume.  Validate a
        # BYTE-IDENTICAL scratch COPY of the active primary+WAL/sidecars
        # (adoption-copy precedent): the fresh semantic reconquest runs over the
        # exact bytes whose SHA was bound, while the active artifacts can never
        # drift under validation.  A crashed prior validation leaves only a
        # scratch directory, which is deleted fail-safe on the next entry, so a
        # completed+marker resume ALWAYS converges (cold or warmed process) and
        # a second resume is a pure no-op.
        # R8-B7.8 (d): the scratch is UNIQUE per invocation — a stale claimant
        # that lost its fence can never delete/replace the CURRENT claimant's
        # scratch by name.  Each invocation cleans up only its OWN directory;
        # orphans from crashed prior validations are removed FAIL-CLOSED here,
        # under the fence (an un-removable orphan raises BEFORE any clear).
        _assert_fenced(fence_check)
        for orphan in sorted(journal_path.parent.glob("resume-validate-scratch*")):
            # R8-B7.9 (#1): incremental FENCED removal — the exact fence bounds
            # every entry/unlink, so a claimant that loses its lease mid-tree
            # stops mutating immediately.
            _remove_tree_fenced(orphan, fence_check=fence_check)
        scratch_root = journal_path.parent / (
            "resume-validate-scratch-" + uuid.uuid4().hex
        )
        scratch_root.mkdir(parents=True)
        scratch_graph = scratch_root / active.graph_path.name
        rb_error: BaseException | None = None
        try:
            # R8-B7.7 (#3): copy with the EXACT ``_snapshot`` artifact inventory
            # (primary + every engine sidecar), fenced per artifact, and require
            # the SCRATCH snapshot SHA to equal the bound candidate SHA BEFORE
            # any fresh runtime is constructed — the validated bytes are provably
            # the copied bytes, and the factory only ever receives the scratch
            # path.  Factory construction/open live INSIDE this cleanup scope.
            for artifact in _artifact_paths(
                active.graph_path, fence_check=fence_check
            ):
                _assert_fenced(fence_check)
                shutil.copy2(artifact, scratch_root / artifact.name)
            _assert_fenced(fence_check)
            scratch_snapshot = _snapshot(scratch_graph, fence_check=fence_check)
            if (
                not scratch_snapshot.exists
                or scratch_snapshot.sha256 != expected_candidate_sha
            ):
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_resume_scratch_copy_mismatch"
                )
            readback = self._runtime_factory(scratch_graph)
            inner_error: BaseException | None = None
            try:
                if adopted:
                    # An adopted complete primary carries its OWN projection, not
                    # the caller's seeds; re-conquer the self-derived evidence.
                    schema_count, counts_by_board, semantic_fingerprint, _proj = (
                        self._validate_and_project_self(
                            readback, fence_check=fence_check
                        )
                    )
                else:
                    schema_count, counts_by_board, semantic_fingerprint = (
                        self._validate_runtime(
                            readback,
                            ordered,
                            fence_check=fence_check,
                        )
                    )
                # Blocker 31 / R8-B7.9 (#2): re-conquer ALL terminal evidence
                # INSIDE the inner try, BEFORE the readback close — a semantic/
                # schema/count drift becomes the IN-FLIGHT cause, so neither a
                # benign close failure nor a cleanup failure can ever mask it.
                if (
                    not expected_semantic
                    or semantic_fingerprint != expected_semantic
                    or (
                        expected_schema_count is not None
                        and schema_count != expected_schema_count
                    )
                    or counts_by_board != expected_counts
                ):
                    raise CommunityGlobalDiscoveryRecoveryError(
                        "global_discovery_completed_semantic_drift"
                    )
            except BaseException as exc:
                inner_error = exc
                raise
            finally:
                # R3: revalidate the exact live fence before the potentially
                # WAL-checkpointing readback close (normal + exceptional paths);
                # authority close errors surface, benign errors never mask the
                # in-flight cause.  The close mutates ONLY the scratch copy.
                self._fenced_readback_close(
                    readback, fence_check, in_flight_error=inner_error
                )
        except BaseException as exc:
            rb_error = exc
            raise
        finally:
            # R8-B7.8 (d): NEVER remove a scratch without a LIVE fence.  On the
            # success path the fence is revalidated and the OWN scratch is
            # removed fail-closed — an un-removable scratch surfaces BEFORE the
            # marker clear (zero clear, marker preserved).  On the exceptional
            # path (including a lost fence) there is ZERO unfenced filesystem
            # mutation and the in-flight cause is never masked: the own scratch
            # stays behind as an orphan for the NEXT owner's fenced entry sweep.
            if rb_error is None:
                _assert_fenced(fence_check)
                try:
                    # R8-B7.9 (#1): incremental FENCED removal of the OWN
                    # scratch — every entry/unlink is bounded by the live fence.
                    _remove_tree_fenced(scratch_root, fence_check=fence_check)
                except OSError as cleanup_exc:
                    raise CommunityGlobalDiscoveryRecoveryError(
                        "global_discovery_resume_scratch_cleanup_failed"
                    ) from cleanup_exc
        # R8-B7/WAL: the accepted SHA must bind the bytes FINALLY persisted after
        # the validation readback — with the non-mutating scratch-copy protocol
        # above this holds deterministically (the active bytes were never opened
        # by the readback); this hard re-check is the fail-closed defense that
        # NO clear may ever happen over drifted bytes.  On mismatch the marker
        # is preserved and zero clear happens.
        _assert_fenced(fence_check)
        post_close = _snapshot(active.graph_path, fence_check=fence_check)
        if not post_close.exists or post_close.sha256 != expected_candidate_sha:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_completed_candidate_sha_post_close_mismatch"
            )

        _fsync_artifacts(active.graph_path, fence_check=fence_check)
        # R4: route the clear through the crash-conservative protocol — persist a
        # pessimistic false completed journal BEFORE the physical clear, clear,
        # then persist the FINAL aggregate.  Returns the persisted final
        # (pre_clear AND clear), so callers use it directly.
        return self._clear_marker_crash_conservatively(
            legacy=legacy,
            generation_id=active.generation_id,
            journal_path=journal_path,
            completed_journal=journal,
            pre_clear_supported=bool(journal.get("directory_fsync_supported", False)),
            fence_check=fence_check,
        )

    def _assert_adoption_terminal_journal(
        self,
        journal: dict,
        *,
        run_id: str,
        epoch: int,
        effective_attempt_id: str,
    ) -> None:
        """Fail-closed structural validation of a terminal adoption journal
        BEFORE any marker clear (blocker 5).  Missing/malformed fields raise;
        no field may be skipped by absence.  The caller must preserve the marker
        and perform zero clear/runtime mutation on failure."""

        def bad(reason: str) -> None:
            raise CommunityGlobalDiscoveryRecoveryError(
                f"global_discovery_adoption_terminal_journal_invalid:{reason}"
            )

        # R5: EXACT JSON types for every terminal field — NEVER coerce with
        # str()/bool()/int().  A 64-digit JSON *integer* must not be accepted as a
        # SHA (it is not a ``str``); a JSON string ``"false"`` must not be a bool.
        if not isinstance(journal, dict):
            bad("shape")
        if journal.get("kind") != "adopt_complete_primary" or not isinstance(
            journal.get("kind"), str
        ):
            bad("kind")
        if journal.get("phase") != "completed" or not isinstance(
            journal.get("phase"), str
        ):
            bad("phase")
        if journal.get("outcome") != "completed" or not isinstance(
            journal.get("outcome"), str
        ):
            bad("outcome")
        if journal.get("rollback_performed") is not False:
            bad("rollback_performed")
        if not isinstance(journal.get("run_id"), str) or journal.get("run_id") != run_id:
            bad("run_id")
        epoch_val = journal.get("epoch")
        if (
            isinstance(epoch_val, bool)
            or not isinstance(epoch_val, int)
            or epoch_val != epoch
        ):
            bad("epoch")
        if (
            not isinstance(journal.get("attempt_id"), str)
            or journal.get("attempt_id") != effective_attempt_id
        ):
            bad("attempt_id")
        generation_id = journal.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id:
            bad("generation_id_empty")
        expected_generation = _physical_generation_id(
            run_id=run_id, attempt_id=effective_attempt_id, epoch=epoch
        )
        if generation_id != expected_generation:
            bad("generation_id")
        for field, code in (
            ("candidate_sha256", "candidate_sha256"),
            ("generation_manifest_sha256", "manifest_sha256"),
            ("semantic_fingerprint", "semantic_fingerprint"),
        ):
            value = journal.get(field)
            if not isinstance(value, str) or not _SHA256_RE.match(value):
                bad(code)
        schema_count = journal.get("schema_object_count")
        if (
            not isinstance(schema_count, int)
            or isinstance(schema_count, bool)
            or schema_count <= 0
        ):
            bad("schema_object_count")
        counts = journal.get("counts_by_board")
        if not isinstance(counts, dict) or not counts:
            bad("counts_by_board")
        for board_id, per_board in counts.items():
            if not isinstance(board_id, str) or not board_id:
                bad("counts_board_id")
            if not isinstance(per_board, dict):
                bad("counts_shape")
            # Exact per-board key set — no missing or extra keys.
            if set(per_board.keys()) != {"boards", "digests", "links"}:
                bad("counts_value")
            for key in ("boards", "digests", "links"):
                value = per_board.get(key)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    bad("counts_value")
        # Exact bool type — not truthiness (``bool("false")`` would be forbidden by
        # requiring an actual ``bool``).
        if not isinstance(journal.get("directory_fsync_supported"), bool):
            bad("directory_fsync_supported")
        # R5: ``clear_settled`` must be an EXACT bool (a JSON string ``"false"``
        # must not be coerced truthy).  Both False (pending, marker present) and
        # True (already settled, marker absent) are structurally valid completed
        # journals reached through this validator; only a non-bool is rejected.
        if not isinstance(journal.get("clear_settled"), bool):
            bad("clear_settled")

    def _assert_seed_terminal_journal(
        self,
        journal: dict,
        *,
        run_id: str,
        epoch: int,
        effective_attempt_id: str,
        expected_generation_id: str | None = None,
    ) -> None:
        """R8-B7.2: fail-closed structural validation of a terminal SEED-REBUILD
        journal BEFORE any terminal seed reconciliation/marker clear, in every
        public reader (analogous to ``_assert_adoption_terminal_journal``).
        Missing/malformed fields raise; no field may be skipped by absence.  The
        caller must preserve the marker and perform zero clear/mutation on
        failure.  Validates the SAME parsed journal dict the caller read — no
        second (TOCTOU) read of the journal path happens here."""

        def bad(reason: str) -> None:
            raise CommunityGlobalDiscoveryRecoveryError(
                f"global_discovery_seed_terminal_journal_invalid:{reason}"
            )

        if not isinstance(journal, dict):
            bad("shape")
        if journal.get("kind") != _SEED_REBUILD_JOURNAL_KIND or not isinstance(
            journal.get("kind"), str
        ):
            bad("kind")
        if journal.get("phase") != "completed" or not isinstance(
            journal.get("phase"), str
        ):
            bad("phase")
        if journal.get("outcome") != "completed" or not isinstance(
            journal.get("outcome"), str
        ):
            bad("outcome")
        if journal.get("rollback_performed") is not False:
            bad("rollback_performed")
        if not isinstance(journal.get("run_id"), str) or journal.get("run_id") != run_id:
            bad("run_id")
        epoch_val = journal.get("epoch")
        if (
            isinstance(epoch_val, bool)
            or not isinstance(epoch_val, int)
            or epoch_val != epoch
        ):
            bad("epoch")
        if (
            not isinstance(journal.get("attempt_id"), str)
            or journal.get("attempt_id") != effective_attempt_id
        ):
            bad("attempt_id")
        generation_id = journal.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id:
            bad("generation_id_empty")
        # R8-B7.6: the expected generation follows the CALLER's naming mode —
        # legacy public rebuilds (no supplied attempt id) name the generation by
        # the bare run_id; attempt-scoped R5 flows use the physical formula.
        expected_generation = (
            expected_generation_id
            if expected_generation_id is not None
            else _physical_generation_id(
                run_id=run_id, attempt_id=effective_attempt_id, epoch=epoch
            )
        )
        if generation_id != expected_generation:
            bad("generation_id")
        # Exact lowercase 64-hex STRINGS — never str()/int() coercions.
        for field, code in (
            ("candidate_sha256", "candidate_sha256"),
            ("generation_manifest_sha256", "manifest_sha256"),
            ("semantic_fingerprint", "semantic_fingerprint"),
            ("source_fingerprint", "source_fingerprint"),
            ("expected_semantic_fingerprint", "expected_semantic_fingerprint"),
        ):
            value = journal.get(field)
            if not isinstance(value, str) or not _SHA256_RE.match(value):
                bad(code)
        schema_count = journal.get("schema_object_count")
        if (
            not isinstance(schema_count, int)
            or isinstance(schema_count, bool)
            or schema_count <= 0
        ):
            bad("schema_object_count")
        counts = journal.get("counts_by_board")
        if not isinstance(counts, dict) or not counts:
            bad("counts_by_board")
        for board_id, per_board in counts.items():
            if not isinstance(board_id, str) or not board_id:
                bad("counts_board_id")
            if not isinstance(per_board, dict):
                bad("counts_shape")
            if set(per_board.keys()) != {"boards", "digests", "links"}:
                bad("counts_value")
            for key in ("boards", "digests", "links"):
                value = per_board.get(key)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    bad("counts_value")
        # Structural durability/clear booleans: EXACT bool types, never coerced.
        if not isinstance(journal.get("directory_fsync_supported"), bool):
            bad("directory_fsync_supported")
        if "clear_settled" in journal and not isinstance(
            journal.get("clear_settled"), bool
        ):
            bad("clear_settled")

    @staticmethod
    def _close_adopt_runtime_preserving(adopt_runtime) -> None:
        """Close an adoption-copy runtime during the corrupt-open fallback.

        Blocker 4 / R3: a close that signals LOST AUTHORITY — fence loss, writer
        authority loss, or single-writer lock contention (``_AUTHORITY_CLOSE_ERRORS``)
        — MUST surface; it is never swallowed.  Only a genuinely BENIGN cleanup
        error is suppressed, and ONLY here, in the already-failed corrupt-open
        fallback, so it cannot replace the original expected-corrupt cause that
        triggered the fallback.  This benign-preserve policy is proven explicitly
        by the R3 regressions (benign close preserved; GraphLockContention and
        fence loss propagate).
        """

        if adopt_runtime is None:
            return
        try:
            adopt_runtime.close()
        except _AUTHORITY_CLOSE_ERRORS:
            raise
        except Exception:
            # A benign close/cleanup error must not hide the original expected
            # corrupt-open cause.
            pass

    @staticmethod
    def _fenced_readback_close(
        readback, fence_check, *, in_flight_error: BaseException | None
    ) -> None:
        """Close a read/validate runtime whose close may checkpoint/flush WAL
        (i.e. a potentially MUTATING close).

        R3: revalidate the exact live fence IMMEDIATELY before the close on BOTH
        the normal and exceptional paths — a lost fence surfaces and the mutating
        close is skipped (never mutate without authority).  A close error
        signalling lost authority (``_AUTHORITY_CLOSE_ERRORS``) ALWAYS propagates.
        On the success path (no validation error in flight) ANY close error
        propagates; on the exceptional path a BENIGN close/cleanup error is
        suppressed so it cannot mask the original validation cause.
        """

        _assert_fenced(fence_check)
        try:
            readback.close()
        except _AUTHORITY_CLOSE_ERRORS:
            raise
        except Exception:
            if in_flight_error is None:
                raise

    def recover_and_cutover(
        self,
        *,
        run_id: str,
        epoch: int = 1,
        attempt_id: str | None = None,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        fence_check: Callable[[], None] | None = None,
    ) -> GlobalDiscoveryCutoverResult:
        """Unified recovery entry (blocker 2).

        If the live primary is a genuinely complete/readable Global Discovery
        graph (fresh validation under recovery authority), ADOPT it — publish a
        copy of its exact bytes as the active generation so the recovered active
        graph preserves the pre-crash content exactly.  Otherwise (partial /
        corrupt / absent primary) fall back to rebuilding the active generation
        from authoritative seeds (R1/R2 behavior).
        """

        adopted = self._adopt_complete_primary(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            fence_check=fence_check,
        )
        if adopted is not None:
            return adopted
        return self.rebuild_candidate_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            boards=boards,
            fence_check=fence_check,
        )

    def _adopt_complete_primary(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str | None,
        expected_live_sha256: str,
        fence_check: Callable[[], None] | None,
    ) -> GlobalDiscoveryCutoverResult | None:
        """Publish a copy of a complete live primary as the active generation.

        Returns a completed result when the live is a genuine complete primary
        and cutover succeeds; returns ``None`` when the live is not a complete
        primary (so the caller rebuilds from seeds).  Never opens the marked live
        directly — it validates a COPY, so no ordinary-open marker bypass exists.
        Follows COMPLETED-FIRST: terminal completed journal + fsync BEFORE the
        marker clear, and completed-resume reconciles via the shared helper.
        """

        effective_fence = fence_check or self._fence_check
        if effective_fence is None:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_writer_fence_missing"
            )

        def pfc() -> None:
            _assert_fenced(effective_fence)

        run_id = validate_generation_id(run_id)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_epoch_invalid"
            )
        canonical_attempt_id = recovery_attempt_id(run_id, epoch)
        if attempt_id is not None and attempt_id != canonical_attempt_id:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            )
        supplied_attempt_id = attempt_id is not None
        effective_attempt_id = attempt_id or canonical_attempt_id

        legacy = self._legacy_path()
        live = self._live_path()
        quarantine_dir = (
            legacy.parent
            / "quarantine"
            / "global-discovery"
            / (Path(effective_attempt_id) if supplied_attempt_id else Path(run_id))
        )
        journal_path = quarantine_dir / _JOURNAL_FILENAME

        pfc()
        existing = _read_journal(
            journal_path,
            run_id,
            epoch=epoch if supplied_attempt_id else None,
            attempt_id=attempt_id if supplied_attempt_id else None,
            fence_check=effective_fence,
        )
        existing_is_adoption = (
            existing is not None
            and str(existing.get("kind") or "") == "adopt_complete_primary"
        )
        if existing is not None and not existing_is_adoption:
            # A seed-rebuild journal owns this run id; not an adoption.
            return None
        if existing_is_adoption:
            phase = str(existing.get("phase") or "")
            if phase == "completed":
                # Blocker 5: fail-closed structural validation of the terminal
                # adoption journal BEFORE any marker clear.
                self._assert_adoption_terminal_journal(
                    existing,
                    run_id=run_id,
                    epoch=epoch,
                    effective_attempt_id=effective_attempt_id,
                )
                # R4: the crash-conservative clear protocol persists the pending
                # (false) then final aggregate journal internally and RETURNS the
                # persisted final (pre_clear AND clear), so no post-clear
                # AND/downgrade dance is needed here.
                aggregate = self._reconcile_completed_and_clear_marker(
                    journal=existing,
                    legacy=legacy,
                    ordered=(),
                    journal_path=journal_path,
                    fence_check=effective_fence,
                )
                return self._result_from_journal(
                    {**existing, "directory_fsync_supported": aggregate}
                )
            if phase == "rolled_back":
                return self._result_from_journal(existing)
            # Non-terminal adoption journal: publication decided by the pointer.

        # Blocker 2/3: the adoption bypass is gated on the durable product
        # marker, checked BEFORE any runtime factory/open/copy/switch/delete/
        # clear.
        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            bootstrap_marker_present,
        )

        pfc()
        marker_present = bootstrap_marker_present(legacy)
        if existing is None:
            # A FRESH recovery with NO marker must never open/copy/adopt the live
            # primary through this special branch — return to the seed-rebuild
            # path with zero adoption runtime/copy/switch work.
            if not marker_present:
                return None
        elif not marker_present:
            # Blocker 3: a NONTERMINAL adoption journal (prepared /
            # pointer_switched) with the marker absent is an anomaly.  A
            # legitimately cleared marker coexists ONLY with a terminal completed
            # journal (COMPLETED-FIRST clears after the completed write).  Fail
            # closed with a typed error and zero runtime factory/open/copy/
            # switch/delete/clear.
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_marker_missing_for_nonterminal"
            )

        generation_id = _physical_generation_id(
            run_id=run_id, attempt_id=effective_attempt_id, epoch=epoch
        )
        candidate = generation_graph_path(legacy, generation_id)
        candidate_root = generation_dir(legacy, generation_id)

        with ladybug_writer_scope(scope="_global", phase="global_discovery_adopt"):
            # Restart-window reconciliation (item 5): the active pointer is the
            # source of truth for "published"; NEVER blindly delete an active
            # candidate.  A post-switch/pre-completed crash reconciles forward.
            pfc()
            active = read_active_generation(legacy)
            if active is not None and active.generation_id == generation_id:
                # Blocker 5: carry the PRIOR journal's conservative durability
                # (a false prepared/pointer_switched fsync must not become true
                # on post-switch resume).  Default False when no journal exists.
                prior_phase = str((existing or {}).get("phase") or "")
                prior_fsync = bool(
                    (existing or {}).get("directory_fsync_supported", False)
                )
                if prior_phase not in ("pointer_switched", "completed"):
                    # R4: the pointer is switched but the switch's own directory
                    # fsync was NOT yet journaled (crash in the prepared ->
                    # pointer_switched window).  The optimistic ``prepared`` fsync
                    # must NOT be inherited — the switch durability is unconfirmed,
                    # so default pessimistically to False.  Only a durable
                    # ``pointer_switched``/``completed`` journal has confirmed the
                    # switch's directory-fsync evidence.
                    prior_fsync = False
                return self._finalize_adopted_generation(
                    legacy=legacy,
                    generation_id=generation_id,
                    run_id=run_id,
                    epoch=epoch,
                    effective_attempt_id=effective_attempt_id,
                    journal_path=journal_path,
                    manifest_sha=active.manifest_sha256,
                    directory_fsync_supported=prior_fsync,
                    fence_check=effective_fence,
                )

            # Not published: discard any orphan/incomplete candidate (pre-switch
            # crash) so a fresh adoption or the seed rebuild cannot collide.
            if candidate_root.exists():
                pfc()
                _remove_tree_fenced(candidate_root, fence_check=effective_fence)

            # Fresh adoption: only applicable when a live primary physically
            # exists and matches the expected snapshot.
            pfc()
            if not live.is_file():
                return None
            live_snapshot = _snapshot(live, fence_check=effective_fence)
            if live_snapshot.sha256 != expected_live_sha256:
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_live_artifact_drift"
                )

            directory_fsync_supported = True
            pfc()
            candidate_root.mkdir(parents=True, exist_ok=False)
            # Copy the live bytes into the candidate, then open the COPY (never
            # the marked live) to validate it is a complete primary + self-project.
            _copy_artifacts(live, candidate, fence_check=effective_fence)
            # Blocker 4/6: factory CREATION + open + validation all inside the
            # narrow boundary (an optional handle), so a factory/open failure —
            # not only a failed ``list_schema_objects`` — is classified, never
            # escaping as unhandled.
            adopt_runtime = None
            try:
                adopt_runtime = self._runtime_factory(candidate)
                (
                    schema_count,
                    counts_by_board,
                    semantic_fingerprint,
                    _projection,
                ) = self._validate_and_project_self(
                    adopt_runtime, fence_check=effective_fence
                )
            except BaseException as exc:
                expected = _is_expected_corrupt_primary_error(exc)
                # Revalidate the exact fence IMMEDIATELY before any potentially
                # mutating close; a fence loss surfaces here and is never hidden
                # by cleanup.
                _assert_fenced(effective_fence)
                self._close_adopt_runtime_preserving(adopt_runtime)
                if expected:
                    # Expected corrupt/open/incoherent: remove ONLY the
                    # unpublished candidate under the live fence and fall back to
                    # authoritative-seed rebuild.
                    _remove_tree_fenced(candidate_root, fence_check=effective_fence)
                    return None
                # Fence/authority loss, lock contention, programmer or
                # post-switch errors propagate (never fallback).
                raise
            # Revalidate authority before the (potentially mutating) close.
            _assert_fenced(effective_fence)
            adopt_runtime.close()

            _fsync_artifacts(candidate, fence_check=effective_fence)
            directory_fsync_supported &= fsync_directory(candidate_root)
            candidate_snapshot = _snapshot(candidate, fence_check=effective_fence)
            if not candidate_snapshot.exists:
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_candidate_missing"
                )
            candidate_sha = candidate_snapshot.sha256

            pfc()
            manifest_sha, manifest_fsync = write_generation_manifest(
                legacy,
                generation_id,
                {
                    "run_id": run_id,
                    "epoch": epoch,
                    "attempt_id": effective_attempt_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "graph_filename": legacy.name,
                    "artifact_sha256_at_cutover": candidate_sha,
                    "artifact_count_at_cutover": candidate_snapshot.artifact_count,
                    "artifact_bytes_at_cutover": candidate_snapshot.total_bytes,
                    "semantic_fingerprint": semantic_fingerprint,
                    "schema_object_count": schema_count,
                    "counts_by_board": counts_by_board,
                },
            )
            directory_fsync_supported &= manifest_fsync

            # Preserve the live original bytes as quarantine evidence.
            original_copy = quarantine_dir / "original" / legacy.name
            _copy_artifacts(live, original_copy, fence_check=effective_fence)
            directory_fsync_supported &= fsync_directory(original_copy.parent)

            # Durable prepared journal BEFORE switch, then pointer_switched after,
            # so any crash in this window is resumable by the pointer/journal
            # (item 5).
            base = {
                "run_id": run_id,
                "epoch": epoch,
                "attempt_id": effective_attempt_id,
                "generation_id": generation_id,
                "kind": "adopt_complete_primary",
                "candidate_sha256": candidate_sha,
                "generation_manifest_sha256": manifest_sha,
                "schema_object_count": schema_count,
                "counts_by_board": counts_by_board,
                "semantic_fingerprint": semantic_fingerprint,
                # R8-B6.2: adoption terminal evidence carries an exact quarantine
                # reference so a successor reconciliation journal can bind the exact
                # source evidence ref (no computed fallback on the read path).
                "quarantine_ref": (
                    f"community-global-discovery-quarantine:{effective_attempt_id}"
                ),
            }
            # R4 sticky-false: prepared/pointer_switched persist
            # directory_fsync_supported=False on disk (a crash-after-write can
            # never leave an optimistic true a restart would trust); the real
            # per-write durability is carried in-process only.
            directory_fsync_supported = self._write_journal_sticky_false(
                journal_path,
                {**base, "phase": "prepared"},
                aggregate=directory_fsync_supported,
                fence_check=effective_fence,
            )

            pfc()
            directory_fsync_supported &= switch_active_generation(
                legacy,
                generation_id=generation_id,
                manifest_sha256=manifest_sha,
            )
            directory_fsync_supported = self._write_journal_sticky_false(
                journal_path,
                {**base, "phase": "pointer_switched"},
                aggregate=directory_fsync_supported,
                fence_check=effective_fence,
            )

            return self._finalize_adopted_generation(
                legacy=legacy,
                generation_id=generation_id,
                run_id=run_id,
                epoch=epoch,
                effective_attempt_id=effective_attempt_id,
                journal_path=journal_path,
                manifest_sha=manifest_sha,
                directory_fsync_supported=directory_fsync_supported,
                fence_check=effective_fence,
            )

    def _finalize_adopted_generation(
        self,
        *,
        legacy: Path,
        generation_id: str,
        run_id: str,
        epoch: int,
        effective_attempt_id: str,
        journal_path: Path,
        manifest_sha: str,
        directory_fsync_supported: bool,
        fence_check: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult:
        """Readback-validate the published adopted generation, then COMPLETED-FIRST
        terminal write + fsync BEFORE the marker clear.  Shared by the fresh
        adoption path and the post-switch restart reconciliation (item 5)."""

        def pfc() -> None:
            _assert_fenced(fence_check)

        pfc()
        active = read_active_generation(legacy)
        if active is None or active.generation_id != generation_id:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_active_generation_mismatch"
            )
        readback = self._runtime_factory(active.graph_path)
        rb_error: BaseException | None = None
        try:
            (
                rb_schema,
                rb_counts,
                rb_fingerprint,
                _rb_projection,
            ) = self._validate_and_project_self(readback, fence_check=fence_check)
        except BaseException as exc:
            rb_error = exc
            raise
        finally:
            # R3: revalidate the exact live fence before the potentially
            # WAL-checkpointing readback close (normal + exceptional paths).
            self._fenced_readback_close(
                readback, fence_check, in_flight_error=rb_error
            )
        _fsync_artifacts(active.graph_path, fence_check=fence_check)
        post = _snapshot(active.graph_path, fence_check=fence_check)
        if not post.exists:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_missing"
            )
        candidate_sha = post.sha256

        pfc()
        completed = {
            "run_id": run_id,
            "epoch": epoch,
            "attempt_id": effective_attempt_id,
            "generation_id": generation_id,
            "kind": "adopt_complete_primary",
            "phase": "completed",
            "outcome": "completed",
            "rollback_performed": False,
            "candidate_sha256": candidate_sha,
            "generation_manifest_sha256": manifest_sha,
            "schema_object_count": rb_schema,
            "counts_by_board": rb_counts,
            "semantic_fingerprint": rb_fingerprint,
            "directory_fsync_supported": directory_fsync_supported,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "quarantine_ref": (
                f"community-global-discovery-quarantine:{effective_attempt_id}"
            ),
        }
        # R4: route the terminal completion + clear through the crash-conservative
        # protocol — persist a pessimistic false completed journal BEFORE the
        # physical clear, clear, then persist the FINAL aggregate — so a crash in
        # the clear window can never leave a marker-absent + optimistic-true state.
        final = self._clear_marker_crash_conservatively(
            legacy=legacy,
            generation_id=generation_id,
            journal_path=journal_path,
            completed_journal=completed,
            pre_clear_supported=directory_fsync_supported,
            fence_check=fence_check,
        )
        return self._result_from_journal(
            {**completed, "directory_fsync_supported": final}
        )

    def reconcile_attempt_terminal_truth(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        fence_check: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult | None:
        """Resolve only pre-existing physical truth after the work deadline.

        A deadline may expire after a durable pointer/journal write but before
        SQL completion.  This path never starts or publishes a fresh candidate:
        it returns terminal journals, or lets the normal idempotent state
        machine finish validation/rollback only after the pointer boundary was
        already crossed.  Pre-cutover ``building``/``prepared`` work remains
        quarantined and is settled as a SQL timeout by the caller.
        """

        normalized_run_id = validate_generation_id(run_id)
        if attempt_id != recovery_attempt_id(normalized_run_id, epoch):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            )
        ordered = tuple(sorted(boards, key=lambda row: row.board_id))
        expected_semantic_fingerprint = canonical_sha256(
            self._expected_semantic_projection(ordered)
        )
        source_fingerprint = canonical_sha256([row.to_dict() for row in ordered])
        _assert_fenced(fence_check)
        legacy = self._legacy_path()
        quarantine_dir = (
            legacy.parent
            / "quarantine"
            / "global-discovery"
            / Path(attempt_id)
        )
        journal = _read_journal(
            quarantine_dir / _JOURNAL_FILENAME,
            normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            fence_check=fence_check,
        )
        if journal is None:
            return None
        if str(journal.get("kind") or "") == "adopt_complete_primary":
            # Blocker 4: adoption journals carry no seed source/expected-semantic
            # fingerprints; reconcile adoption terminal truth through the adoption
            # path (exact active-generation/semantic/fence rules), never the
            # seed-only preconditions below.
            return self._adopt_complete_primary(
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=expected_live_sha256,
                fence_check=fence_check,
            )
        if str(journal.get("kind") or "") == _RECONCILE_PREDECESSOR_CUTOVER_KIND:
            # B6.5: a successor's OWN completed reconciliation journal already
            # bound and finished a predecessor's crossed truth.  Validate its exact
            # predecessor bindings fail-closed and return it idempotently — never
            # re-run physical work and never relabel the predecessor result.
            self._assert_reconcile_predecessor_journal(
                journal,
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                boards=ordered,
                fence_check=fence_check,
            )
            return self._result_from_journal(journal)
        # B7.3: fail closed — only a journal carrying the EXACT seed-rebuild kind
        # may enter the seed reconciliation path.  An arbitrary/missing non-adoption
        # kind is a corrupt/foreign journal and must never be treated as a seed.
        if str(journal.get("kind") or "") != _SEED_REBUILD_JOURNAL_KIND:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_kind_invalid"
            )
        if (
            journal.get("source_fingerprint") != source_fingerprint
            or journal.get("expected_semantic_fingerprint")
            != expected_semantic_fingerprint
        ):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_resume_source_drift"
            )
        phase = str(journal.get("phase") or "")
        if phase == "completed":
            # R8-B7.2: strict fail-closed structural validation of the terminal
            # seed journal BEFORE any reconciliation/marker clear — on the SAME
            # parsed dict read above (no TOCTOU reread).
            self._assert_seed_terminal_journal(
                journal,
                run_id=normalized_run_id,
                epoch=epoch,
                effective_attempt_id=attempt_id,
            )
            # Blocker 11: both terminal entry points MUST run the same exact-hash
            # + fresh-semantic-validation + fence + idempotent-clear reconcile so
            # a completed->clear crash cannot leave Global Discovery permanently
            # unreadable through the R5 worker path.
            # R4: returns the persisted final aggregate (pre_clear AND clear).
            aggregate = self._reconcile_completed_and_clear_marker(
                journal=journal,
                legacy=legacy,
                ordered=ordered,
                journal_path=quarantine_dir / _JOURNAL_FILENAME,
                fence_check=fence_check,
            )
            return self._result_from_journal(
                {**journal, "directory_fsync_supported": aggregate}
            )
        if phase == "rolled_back":
            return self._result_from_journal(journal)
        crossed_pointer = phase in {
            "pointer_switched",
            "readback_validated",
            "rollback_pending",
        }
        if phase == "prepared":
            _assert_fenced(fence_check)
            active = read_active_generation(legacy)
            generation_id = _physical_generation_id(
                run_id=normalized_run_id,
                attempt_id=attempt_id,
                epoch=epoch,
            )
            crossed_pointer = bool(
                active is not None and active.generation_id == generation_id
            )
        if not crossed_pointer:
            return None
        return self.rebuild_candidate_and_cutover(
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            boards=ordered,
            fence_check=fence_check,
        )

    def _assert_reconcile_predecessor_journal(
        self,
        journal: dict[str, object],
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        fence_check: Callable[[], None],
    ) -> None:
        """B6.5/R8-B6.2: fail-closed provenance + PHYSICAL-TRUTH validation of a
        successor reconciliation journal on EVERY read (and on creation).  Beyond
        exact syntax it (a) reads the bound source journal ONCE and validates its
        canonical hash + identity + exact terminal fields from THAT SAME buffer
        whose raw SHA-256 must equal ``predecessor_journal_sha256`` (no TOCTOU
        reread); (b) requires the successor generation/manifest/candidate/schema and
        evidence ref to equal the exact reopened source AND the ACTUAL active
        pointer/manifest/snapshot; and (c) REOPENS the actual active graph with the
        production runtime factory under the fence and reconquers the FRESH semantic
        projection (seed -> _validate_runtime; adoption -> _validate_and_project_self)
        requiring exact schema/counts/semantic equality to the strict source.  A
        fully self-consistent forged set (forged source + own + matching pointer/
        manifest/candidate over arbitrary active bytes, recomputed valid hashes)
        dies on the fresh semantic reopen.  No marker clear/write happens here."""

        import re as _re

        def bad(field: str) -> None:
            raise CommunityGlobalDiscoveryRecoveryError(
                f"global_discovery_reconcile_predecessor_journal_invalid:{field}"
            )

        # --- exact own syntax ---
        if journal.get("kind") != _RECONCILE_PREDECESSOR_CUTOVER_KIND:
            bad("kind")
        if journal.get("run_id") != run_id:
            bad("run_id")
        j_epoch = journal.get("epoch")
        if not isinstance(j_epoch, int) or isinstance(j_epoch, bool) or j_epoch != epoch:
            bad("epoch")
        if journal.get("attempt_id") != attempt_id:
            bad("attempt_id")
        if journal.get("phase") != "completed":
            bad("phase")
        if journal.get("outcome") != "completed":
            bad("outcome")
        if journal.get("rollback_performed") is not False:
            bad("rollback_performed")
        if journal.get("quarantine_ref") != (
            f"community-global-discovery-quarantine:{attempt_id}"
        ):
            bad("quarantine_ref")
        pred_epoch = journal.get("predecessor_epoch")
        if (
            not isinstance(pred_epoch, int)
            or isinstance(pred_epoch, bool)
            or pred_epoch < 1
            or pred_epoch >= epoch
        ):
            bad("predecessor_epoch")
        pred_attempt = journal.get("predecessor_attempt_id")
        if not isinstance(pred_attempt, str) or pred_attempt != recovery_attempt_id(
            run_id, int(pred_epoch)
        ):
            bad("predecessor_attempt_id")
        pred_sha = journal.get("predecessor_journal_sha256")
        if not isinstance(pred_sha, str) or not _re.fullmatch(r"[0-9a-f]{64}", pred_sha):
            bad("predecessor_journal_sha256")
        evidence = journal.get("predecessor_evidence_ref")
        if not isinstance(evidence, str) or not evidence:
            bad("predecessor_evidence_ref")
        if journal.get("reconciled_outcome") != "completed":
            bad("reconciled_outcome")
        own_candidate = journal.get("candidate_sha256")
        own_manifest = journal.get("generation_manifest_sha256")
        for _field, _value in (
            ("candidate_sha256", own_candidate),
            ("generation_manifest_sha256", own_manifest),
        ):
            if not isinstance(_value, str) or not _re.fullmatch(r"[0-9a-f]{64}", _value):
                bad(_field)
        own_generation_id = journal.get("generation_id")
        if not isinstance(own_generation_id, str) or not own_generation_id:
            bad("generation_id")
        own_schema = journal.get("schema_object_count")
        if (
            not isinstance(own_schema, int)
            or isinstance(own_schema, bool)
            or own_schema <= 0
        ):
            bad("schema_object_count")

        legacy = self._legacy_path()
        ordered = tuple(sorted(boards, key=lambda row: row.board_id))

        # --- read the source journal ONCE; validate hash/identity/fields from the
        #     SAME buffer whose raw SHA is bound (no TOCTOU reread) ---
        source_path = (
            legacy.parent
            / "quarantine"
            / "global-discovery"
            / Path(str(pred_attempt))
            / _JOURNAL_FILENAME
        )
        _assert_fenced(fence_check)
        if not source_path.exists():
            bad("source_missing")
        source_bytes = source_path.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != pred_sha:
            bad("predecessor_journal_sha256_mismatch")
        try:
            source = json.loads(source_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            bad("source_unreadable")
            return
        if not isinstance(source, dict):
            bad("source_unreadable")
        if source.get("journal_sha256") != canonical_sha256(_journal_binding(source)):
            bad("source_hash")
        if source.get("run_id") != run_id or source.get("attempt_id") != str(
            pred_attempt
        ):
            bad("source_identity")
        s_epoch = source.get("epoch")
        if (
            isinstance(s_epoch, bool)
            or not isinstance(s_epoch, int)
            or s_epoch != int(pred_epoch)
        ):
            bad("source_epoch")
        source_kind = source.get("kind")
        if source_kind not in {"adopt_complete_primary", _SEED_REBUILD_JOURNAL_KIND}:
            bad("source_kind")
        if source.get("phase") != "completed" or source.get("outcome") != "completed":
            bad("source_terminal")
        if source.get("rollback_performed") is not False:
            bad("source_rollback")
        src_gen = source.get("generation_id")
        if not isinstance(src_gen, str) or not src_gen:
            bad("source_generation_id")
        src_candidate = source.get("candidate_sha256")
        src_manifest = source.get("generation_manifest_sha256")
        src_semantic = source.get("semantic_fingerprint")
        for _field, _value in (
            ("source_candidate", src_candidate),
            ("source_manifest", src_manifest),
            ("source_semantic", src_semantic),
        ):
            if not isinstance(_value, str) or not _re.fullmatch(r"[0-9a-f]{64}", _value):
                bad(_field)
        src_schema = source.get("schema_object_count")
        if (
            not isinstance(src_schema, int)
            or isinstance(src_schema, bool)
            or src_schema <= 0
        ):
            bad("source_schema")
        src_counts = source.get("counts_by_board")
        if not isinstance(src_counts, dict) or not src_counts:
            bad("source_counts")
        src_ref = source.get("quarantine_ref")
        if not isinstance(src_ref, str) or src_ref != (
            f"community-global-discovery-quarantine:{pred_attempt}"
        ):
            bad("source_quarantine_ref")
        if source_kind == _SEED_REBUILD_JOURNAL_KIND:
            # R8-B7.5: a SEED source gets the FULL strict terminal validator on
            # this SAME parsed dict, plus fingerprint binding to the caller's
            # exact boards — a self-hashed forged seed source whose fingerprints
            # do not derive from these boards/expected projection dies here with
            # zero mutation.
            self._assert_seed_terminal_journal(
                source,
                run_id=run_id,
                epoch=int(pred_epoch),
                effective_attempt_id=str(pred_attempt),
            )
            bound_source_fp = canonical_sha256(
                [row.to_dict() for row in ordered]
            )
            bound_expected_sem = canonical_sha256(
                self._expected_semantic_projection(ordered)
            )
            if source.get("source_fingerprint") != bound_source_fp:
                bad("source_fingerprint_binding")
            if source.get("expected_semantic_fingerprint") != bound_expected_sem:
                bad("source_expected_semantic_binding")
            if source.get("semantic_fingerprint") != bound_expected_sem:
                bad("source_semantic_binding")
        # own bindings == exact source terminal physical truth.
        if evidence != src_ref:
            bad("predecessor_evidence_ref")
        if own_generation_id != src_gen:
            bad("generation_id_source_mismatch")
        if own_manifest != src_manifest:
            bad("generation_manifest_source_mismatch")
        if own_candidate != src_candidate:
            bad("candidate_source_mismatch")
        if own_schema != src_schema:
            bad("schema_source_mismatch")

        # --- bind to the ACTUAL active pointer / manifest / snapshot bytes ---
        _assert_fenced(fence_check)
        active = read_active_generation(legacy)
        if active is None:
            bad("active_generation_missing")
        if own_generation_id != active.generation_id:
            bad("generation_id_mismatch")
        if own_manifest != active.manifest_sha256:
            bad("generation_manifest_mismatch")
        _assert_fenced(fence_check)
        active_snapshot = _snapshot(active.graph_path, fence_check=fence_check)
        if not active_snapshot.exists:
            bad("active_snapshot_missing")
        if own_candidate != active_snapshot.sha256:
            bad("candidate_sha256_mismatch")

        # --- reconquer the FRESH semantic projection from the ACTUAL active bytes
        #     (a self-consistent forged set dies here); fenced readback close ---
        _assert_fenced(fence_check)
        readback = self._runtime_factory(active.graph_path)
        readback_error: BaseException | None = None
        try:
            if source_kind == _SEED_REBUILD_JOURNAL_KIND:
                fresh_schema, fresh_counts, fresh_semantic = self._validate_runtime(
                    readback, ordered, fence_check=fence_check
                )
            else:
                (
                    fresh_schema,
                    fresh_counts,
                    fresh_semantic,
                    _fresh_projection,
                ) = self._validate_and_project_self(readback, fence_check=fence_check)
            if fresh_semantic != src_semantic:
                bad("fresh_semantic_mismatch")
            if int(fresh_schema) != int(src_schema):
                bad("fresh_schema_mismatch")
            if fresh_counts != src_counts:
                bad("fresh_counts_mismatch")
        except BaseException as exc:
            readback_error = exc
            raise
        finally:
            self._fenced_readback_close(
                readback, fence_check, in_flight_error=readback_error
            )

    def reconcile_predecessor_and_complete(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        ancestry: tuple[tuple[int, str], ...],
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        fence_check: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult | None:
        """B6.4/B6.5: under the NEW fence, walk the bound pending ANCESTRY (ordered
        immediate-predecessor -> source), find the exact unresolved source whose
        crossed physical truth reconciles to completed (a completed successor
        journal is consumed idempotently; a journal-less crashed successor yields
        None so the walk continues deeper), then write the successor's OWN
        completed-first journal (kind ``reconcile_predecessor_cutover``) binding the
        exact source identity + SHA-256 of the raw source journal bytes + evidence
        ref + reconciled outcome/physical bindings.  Returns a cutover bound to the
        successor's OWN journal (from which the caller builds
        ``RecoveryPhysicalTruth(attempt_id=N+1)``) when a crossing is healed;
        returns ``None`` when NO ancestry entry completed a crossing so the caller
        performs normal N+1 work.  The predecessor result is NEVER relabeled and NO
        fresh recover with the copied old live SHA is performed."""

        normalized_run_id = validate_generation_id(run_id)
        if attempt_id != recovery_attempt_id(normalized_run_id, epoch):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            )
        if not ancestry:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            )
        for pe, pa in ancestry:
            if (
                int(pe) < 1
                or int(pe) >= int(epoch)
                or pa != recovery_attempt_id(normalized_run_id, int(pe))
            ):
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_attempt_identity_invalid"
                )
        effective_fence = fence_check or self._fence_check
        if effective_fence is None:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_writer_fence_missing"
            )

        legacy = self._legacy_path()
        own_quarantine_dir = (
            legacy.parent / "quarantine" / "global-discovery" / Path(attempt_id)
        )
        own_journal_path = own_quarantine_dir / _JOURNAL_FILENAME
        own_quarantine_ref = f"community-global-discovery-quarantine:{attempt_id}"

        # Idempotent: an existing successor journal is validated and returned as-is;
        # never re-run physical work or rewrite the completed-first crash floor.
        _assert_fenced(effective_fence)
        existing_own = _read_journal(
            own_journal_path,
            normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            fence_check=effective_fence,
        )
        ordered_boards = tuple(sorted(boards, key=lambda row: row.board_id))
        if existing_own is not None:
            self._assert_reconcile_predecessor_journal(
                existing_own,
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                boards=ordered_boards,
                fence_check=effective_fence,
            )
            # R8-B6.2: the recorded predecessor must be within the ORDERED bound
            # ancestry, and no EARLIER ancestry entry may have a source journal that
            # the walk should have selected first (never skip a completed source).
            recorded = (
                int(existing_own["predecessor_epoch"]),
                str(existing_own["predecessor_attempt_id"]),
            )
            ancestry_list = [(int(pe), str(pa)) for pe, pa in ancestry]
            if recorded not in ancestry_list:
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_reconcile_predecessor_journal_invalid"
                    ":predecessor_not_in_ancestry"
                )
            for _pe, _pa in ancestry_list[: ancestry_list.index(recorded)]:
                _assert_fenced(effective_fence)
                earlier = (
                    legacy.parent / "quarantine" / "global-discovery"
                    / Path(_pa) / _JOURNAL_FILENAME
                )
                if earlier.exists():
                    raise CommunityGlobalDiscoveryRecoveryError(
                        "global_discovery_reconcile_predecessor_journal_invalid"
                        ":ancestry_skipped_source"
                    )
            return self._result_from_journal(existing_own)

        # B6.4: walk the bound pending ancestry (immediate -> source) under the new
        # fence; the first entry that reconciles to a completed crossing is the
        # exact unresolved source.  A journal-less crashed successor reconciles to
        # None and the walk continues deeper toward the source.
        predecessor_epoch: int | None = None
        predecessor_attempt_id: str | None = None
        predecessor_result: GlobalDiscoveryCutoverResult | None = None
        for pe, pa in ancestry:
            _assert_fenced(effective_fence)
            candidate_result = self.reconcile_attempt_terminal_truth(
                run_id=normalized_run_id,
                epoch=int(pe),
                attempt_id=pa,
                expected_live_sha256=expected_live_sha256,
                boards=boards,
                fence_check=effective_fence,
            )
            if candidate_result is not None and candidate_result.outcome == "completed":
                predecessor_epoch = int(pe)
                predecessor_attempt_id = pa
                predecessor_result = candidate_result
                break
        if predecessor_result is None or predecessor_attempt_id is None:
            # No completed crossing anywhere in the ancestry (rolled back / no
            # pointer cross): the caller does normal N+1 work AFTER this floor.
            return None

        # Raw SOURCE journal bytes + SHA-256 (exact source evidence).
        predecessor_journal_path = (
            legacy.parent
            / "quarantine"
            / "global-discovery"
            / Path(predecessor_attempt_id)
            / _JOURNAL_FILENAME
        )
        _assert_fenced(effective_fence)
        predecessor_bytes = predecessor_journal_path.read_bytes()
        predecessor_journal_sha256 = hashlib.sha256(predecessor_bytes).hexdigest()

        # Bind the ACTUAL active physical generation (predecessor completed => the
        # pointer is installed).  The candidate SHA is the actual active snapshot.
        _assert_fenced(effective_fence)
        active = read_active_generation(legacy)
        if active is None:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_active_generation_mismatch"
            )
        active_snapshot_sha = _snapshot(
            active.graph_path, fence_check=effective_fence
        ).sha256

        own_journal: dict[str, object] = {
            "run_id": normalized_run_id,
            "epoch": int(epoch),
            "attempt_id": attempt_id,
            "kind": _RECONCILE_PREDECESSOR_CUTOVER_KIND,
            "phase": "completed",
            "outcome": "completed",
            "rollback_performed": False,
            "predecessor_epoch": int(predecessor_epoch),
            "predecessor_attempt_id": predecessor_attempt_id,
            "predecessor_journal_sha256": predecessor_journal_sha256,
            "predecessor_evidence_ref": (
                predecessor_result.recovery_journal_ref
                or predecessor_result.quarantine_ref
                or f"community-global-discovery-quarantine:{predecessor_attempt_id}"
            ),
            "reconciled_outcome": predecessor_result.outcome,
            "candidate_sha256": active_snapshot_sha,
            "generation_id": active.generation_id,
            "generation_manifest_sha256": active.manifest_sha256,
            "schema_object_count": int(predecessor_result.schema_object_count or 0),
            "quarantine_ref": own_quarantine_ref,
        }
        # R8-B6.2: validate the full provenance + fresh active semantics on the
        # CREATION path BEFORE recording — a completed source whose marker is
        # already absent must not let an arbitrary active graph be blessed here.
        self._assert_reconcile_predecessor_journal(
            own_journal,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            boards=ordered_boards,
            fence_check=effective_fence,
        )
        _assert_fenced(effective_fence)
        own_quarantine_dir.mkdir(parents=True, exist_ok=True)
        # Completed-first: the successor's OWN terminal journal is durable BEFORE
        # the caller drives SQL SUCCESS.
        supported = _write_journal_with_directory_fsync(
            own_journal_path,
            own_journal,
            fence_check=effective_fence,
        )
        # Re-read and re-validate the EXACT durably-written bytes before returning.
        _assert_fenced(effective_fence)
        persisted = _read_journal(
            own_journal_path,
            normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            fence_check=effective_fence,
        )
        if persisted is None:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_reconcile_predecessor_journal_not_durable"
            )
        self._assert_reconcile_predecessor_journal(
            persisted,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            boards=ordered_boards,
            fence_check=effective_fence,
        )
        return self._result_from_journal(
            {**persisted, "directory_fsync_supported": supported}
        )

    def rebuild_candidate_and_cutover(
        self,
        *,
        run_id: str,
        epoch: int = 1,
        attempt_id: str | None = None,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        fence_check: Callable[[], None] | None = None,
    ) -> GlobalDiscoveryCutoverResult:
        effective_fence_check = fence_check or self._fence_check
        if effective_fence_check is None:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_writer_fence_missing"
            )

        def physical_fence_check() -> None:
            _assert_fenced(effective_fence_check)

        run_id = validate_generation_id(run_id)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_epoch_invalid"
            )
        supplied_attempt_id = attempt_id is not None
        canonical_attempt_id = recovery_attempt_id(run_id, epoch)
        if attempt_id is None:
            attempt_id = canonical_attempt_id
        elif attempt_id != canonical_attempt_id:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            )
        generation_id = (
            _physical_generation_id(
                run_id=run_id,
                attempt_id=attempt_id,
                epoch=epoch,
            )
            if supplied_attempt_id
            else run_id
        )
        ordered = tuple(sorted(boards, key=lambda row: row.board_id))
        if not ordered or len({row.board_id for row in ordered}) != len(ordered):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_board_inventory_invalid"
            )
        if any(not row.summary_embedding for row in ordered) or any(
            not digest.embedding for row in ordered for digest in row.digests
        ):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_embedding_missing"
            )

        physical_fence_check()
        legacy = self._legacy_path()
        physical_fence_check()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        candidate = generation_graph_path(legacy, generation_id)
        candidate_root = generation_dir(legacy, generation_id)
        quarantine_dir = (
            legacy.parent
            / "quarantine"
            / "global-discovery"
            / (Path(attempt_id) if supplied_attempt_id else Path(run_id))
        )
        journal_path = quarantine_dir / _JOURNAL_FILENAME
        quarantine_ref = f"community-global-discovery-quarantine:{attempt_id}"
        expected_semantic_fingerprint = canonical_sha256(
            self._expected_semantic_projection(ordered)
        )
        # ``source_artifact_ref`` intentionally participates in this source
        # fingerprint only.  DecisionDigest has no provenance column yet, so it
        # must not be invented as adapter-private graph state.
        source_fingerprint = canonical_sha256([row.to_dict() for row in ordered])
        journal = _read_journal(
            journal_path,
            run_id,
            epoch=epoch if supplied_attempt_id else None,
            attempt_id=attempt_id if supplied_attempt_id else None,
            fence_check=physical_fence_check,
        )
        if journal is not None:
            # B7.3: an existing seed-rebuild resume journal must carry the EXACT
            # seed-rebuild kind; fail closed on any arbitrary/missing kind so a
            # corrupt/foreign journal cannot drive the seed reconciliation path.
            if str(journal.get("kind") or "") != _SEED_REBUILD_JOURNAL_KIND:
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_journal_kind_invalid"
                )
            if (
                journal.get("source_fingerprint") != source_fingerprint
                or journal.get("expected_semantic_fingerprint")
                != expected_semantic_fingerprint
            ):
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_resume_source_drift"
                )
            if journal.get("phase") == "completed":
                # R8-B7.2: strict fail-closed structural validation of the
                # terminal seed journal BEFORE any reconciliation/marker clear —
                # on the SAME parsed dict read above (no TOCTOU reread).  The
                # expected generation follows THIS caller's naming mode (legacy
                # bare run_id vs attempt-scoped physical).
                self._assert_seed_terminal_journal(
                    journal,
                    run_id=run_id,
                    epoch=epoch,
                    effective_attempt_id=attempt_id,
                    expected_generation_id=generation_id,
                )
                # COMPLETED-FIRST resume-with-marker (Nexus
                # msg_08f6fa2df8ab4e728144e12e30ca7c67): the terminal journal is
                # durable but the marker clear may not have run (crash between
                # completed and clear, or during clear).  Re-conquer the cutover
                # evidence (exact bytes + fresh semantic validation) under the
                # fence, then clear the marker idempotently so state becomes
                # readable.  A second resume revalidates and is the same no-op.
                # R4: returns the persisted final aggregate (pre_clear AND clear).
                aggregate = self._reconcile_completed_and_clear_marker(
                    journal=journal,
                    legacy=legacy,
                    ordered=ordered,
                    journal_path=journal_path,
                    fence_check=physical_fence_check,
                )
                return self._result_from_journal(
                    {**journal, "directory_fsync_supported": aggregate}
                )
            if journal.get("phase") == "rolled_back":
                return self._result_from_journal(journal)
            # R8-B7.5: fail-closed classification of EVERY resume phase BEFORE
            # the writer scope and BEFORE any physical mutation or terminal
            # journal write.  A valid self-hashed journal whose phase is
            # absent/unknown/malformed must never skip the known resume branches
            # and fall into candidate/terminalization work: it is corruption and
            # raises here with ZERO writes — the marker, the journal bytes and
            # every artifact stay untouched (``completed``/``rolled_back`` were
            # already consumed above).
            if str(journal.get("phase")) not in {
                "building",
                "prepared",
                "pointer_switched",
                "readback_validated",
                "rollback_pending",
            }:
                raise CommunityGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_journal_phase_invalid"
                )
        else:
            physical_fence_check()
        candidate_exists = False
        quarantine_exists = False
        if journal is None:
            physical_fence_check()
            candidate_exists = candidate_root.exists()
            physical_fence_check()
            quarantine_exists = quarantine_dir.exists()
        if journal is None and (candidate_exists or quarantine_exists):
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_run_id_collision"
            )

        physical_fence_check()
        previous = read_active_generation(legacy)
        previous_generation_id = previous.generation_id if previous else None
        previous_manifest_sha256 = previous.manifest_sha256 if previous else None
        directory_fsync_supported = True
        schema_count = 0
        counts_by_board: dict[str, dict[str, int]] = {}
        semantic_fingerprint = expected_semantic_fingerprint
        candidate_sha = ""
        manifest_sha = ""

        with ladybug_writer_scope(scope="_global", phase="global_discovery_recovery"):
            try:
                physical_fence_check()
                if journal is not None:
                    previous_generation_id = (
                        str(journal["previous_generation_id"])
                        if journal.get("previous_generation_id")
                        else None
                    )
                    previous_manifest_sha256 = (
                        str(journal["previous_manifest_sha256"])
                        if journal.get("previous_manifest_sha256")
                        else None
                    )
                    candidate_sha = str(journal.get("candidate_sha256") or "")
                    manifest_sha = str(journal.get("generation_manifest_sha256") or "")
                    schema_count = int(journal.get("schema_object_count") or 0)
                    counts_by_board = dict(journal.get("counts_by_board") or {})
                    semantic_fingerprint = str(
                        journal.get("semantic_fingerprint") or ""
                    )

                phase = str(journal.get("phase")) if journal else ""
                if phase == "building":
                    # A process died before publishing a prepared generation.
                    # The partial directory was never active and can be retained
                    # as evidence while a clean same-id directory is rebuilt.
                    physical_fence_check()
                    if candidate_root.exists():
                        failed_root = quarantine_dir / "failed-candidate"
                        physical_fence_check()
                        failed_root.mkdir(parents=True, exist_ok=True)
                        failed = failed_root / "interrupted"
                        suffix = 1
                        physical_fence_check()
                        while failed.exists():
                            failed = failed_root / f"interrupted-{suffix}"
                            suffix += 1
                            physical_fence_check()
                        physical_fence_check()
                        os.replace(candidate_root, failed)
                    journal = None
                    phase = ""

                if phase == "rollback_pending":
                    try:
                        physical_fence_check()
                        active = read_active_generation(legacy)
                    except GlobalDiscoveryLayoutError as exc:
                        raise CommunityGlobalDiscoveryRecoveryError(
                            "global_discovery_rollback_state_ambiguous"
                        ) from exc
                    previous_is_active = (
                        previous_generation_id is None and active is None
                    ) or (
                        active is not None
                        and active.generation_id == previous_generation_id
                        and active.manifest_sha256 == previous_manifest_sha256
                    )
                    if not previous_is_active:
                        if active is None or active.generation_id != generation_id:
                            raise CommunityGlobalDiscoveryRecoveryError(
                                "global_discovery_rollback_state_ambiguous"
                            )
                        directory_fsync_supported &= self._restore_previous(
                            legacy=legacy,
                            previous_generation_id=previous_generation_id,
                            previous_manifest_sha256=previous_manifest_sha256,
                            fence_check=physical_fence_check,
                        )
                    rolled_back = {
                        **journal,
                        "phase": "rolled_back",
                        "outcome": "rolled_back",
                        "rollback_performed": True,
                        "directory_fsync_supported": directory_fsync_supported,
                        "rolled_back_at": datetime.now(timezone.utc).isoformat(),
                    }
                    _write_journal_with_directory_fsync(
                        journal_path,
                        rolled_back,
                        fence_check=physical_fence_check,
                    )
                    return self._result_from_journal(rolled_back)

                if journal is None:
                    physical_fence_check()
                    quarantine_dir.mkdir(parents=True, exist_ok=True)
                    base_journal: dict[str, object] = {
                        "run_id": run_id,
                        "epoch": epoch,
                        "attempt_id": attempt_id,
                        "generation_id": generation_id,
                        # B7: explicit, stable kind for the authoritative-seed
                        # rebuild journal (parallels ``adopt_complete_primary``), so
                        # a terminal seed-rebuild record is bound to its exact kind
                        # rather than merely "not an adoption".
                        "kind": _SEED_REBUILD_JOURNAL_KIND,
                        "phase": "building",
                        "source_fingerprint": source_fingerprint,
                        "expected_semantic_fingerprint": (
                            expected_semantic_fingerprint
                        ),
                        "previous_generation_id": previous_generation_id,
                        "previous_manifest_sha256": previous_manifest_sha256,
                        "expected_live_sha256": expected_live_sha256,
                        "quarantine_ref": quarantine_ref,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    directory_fsync_supported &= _write_journal_with_directory_fsync(
                        journal_path,
                        base_journal,
                        fence_check=physical_fence_check,
                    )
                    physical_fence_check()
                    candidate_root.mkdir(parents=True, exist_ok=False)
                    physical_fence_check()
                    candidate_runtime = self._runtime_factory(candidate)
                    candidate_close_error: BaseException | None = None
                    try:
                        physical_fence_check()
                        candidate_runtime.bootstrap()
                        self._materialize(
                            candidate_runtime,
                            ordered,
                            fence_check=physical_fence_check,
                        )
                        (
                            schema_count,
                            counts_by_board,
                            semantic_fingerprint,
                        ) = self._validate_runtime(
                            candidate_runtime,
                            ordered,
                            fence_check=physical_fence_check,
                        )
                        physical_fence_check()
                        candidate_runtime.flush_after_write_batch()
                    except BaseException as exc:
                        candidate_close_error = exc
                        raise
                    finally:
                        # R3: revalidate the exact live fence before this
                        # WAL-checkpointing close (normal + exceptional paths);
                        # authority close errors surface, benign errors never mask
                        # an already-in-flight error.
                        self._fenced_readback_close(
                            candidate_runtime,
                            physical_fence_check,
                            in_flight_error=candidate_close_error,
                        )
                    _fsync_artifacts(
                        candidate,
                        fence_check=physical_fence_check,
                    )
                    physical_fence_check()
                    directory_fsync_supported &= fsync_directory(candidate_root)
                    candidate_snapshot = _snapshot(
                        candidate,
                        fence_check=physical_fence_check,
                    )
                    if not candidate_snapshot.exists:
                        raise CommunityGlobalDiscoveryRecoveryError(
                            "global_discovery_candidate_missing"
                        )
                    candidate_sha = candidate_snapshot.sha256
                    physical_fence_check()
                    manifest_sha, manifest_fsync = write_generation_manifest(
                        legacy,
                        generation_id,
                        {
                            "run_id": run_id,
                            "epoch": epoch,
                            "attempt_id": attempt_id,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "graph_filename": legacy.name,
                            "artifact_sha256_at_cutover": candidate_sha,
                            "artifact_count_at_cutover": candidate_snapshot.artifact_count,
                            "artifact_bytes_at_cutover": candidate_snapshot.total_bytes,
                            "source_fingerprint": source_fingerprint,
                            "semantic_fingerprint": semantic_fingerprint,
                            "schema_object_count": schema_count,
                            "counts_by_board": counts_by_board,
                        },
                    )
                    directory_fsync_supported &= manifest_fsync

                    physical_fence_check()
                    self._global_runtime.close()
                    physical_fence_check()
                    live = self._live_path()
                    current = _snapshot(
                        live,
                        fence_check=physical_fence_check,
                    )
                    if current.sha256 != expected_live_sha256:
                        raise CommunityGlobalDiscoveryRecoveryError(
                            "global_discovery_live_artifact_drift"
                        )
                    original_copy = quarantine_dir / "original" / legacy.name
                    _copy_artifacts(
                        live,
                        original_copy,
                        fence_check=physical_fence_check,
                    )
                    copied = _snapshot(
                        original_copy,
                        fence_check=physical_fence_check,
                    )
                    if copied.sha256 != current.sha256:
                        raise CommunityGlobalDiscoveryRecoveryError(
                            "global_discovery_quarantine_copy_mismatch"
                        )
                    physical_fence_check()
                    directory_fsync_supported &= fsync_directory(
                        original_copy.parent
                    )
                    journal = {
                        **base_journal,
                        "phase": "prepared",
                        "candidate_sha256": candidate_sha,
                        "generation_manifest_sha256": manifest_sha,
                        "schema_object_count": schema_count,
                        "counts_by_board": counts_by_board,
                        "semantic_fingerprint": semantic_fingerprint,
                        "directory_fsync_supported": directory_fsync_supported,
                    }
                    directory_fsync_supported &= _write_journal_with_directory_fsync(
                        journal_path,
                        journal,
                        fence_check=physical_fence_check,
                    )
                    phase = "prepared"

                if phase == "prepared":
                    physical_fence_check()
                    self._global_runtime.close()
                    current = _snapshot(
                        self._live_path(),
                        fence_check=physical_fence_check,
                    )
                    if current.sha256 != expected_live_sha256:
                        raise CommunityGlobalDiscoveryRecoveryError(
                            "global_discovery_live_artifact_drift"
                        )
                    physical_fence_check()
                    directory_fsync_supported &= switch_active_generation(
                        legacy,
                        generation_id=generation_id,
                        manifest_sha256=manifest_sha,
                    )
                    journal = {
                        **journal,
                        "phase": "pointer_switched",
                        "directory_fsync_supported": directory_fsync_supported,
                    }
                    directory_fsync_supported &= _write_journal_with_directory_fsync(
                        journal_path,
                        journal,
                        fence_check=physical_fence_check,
                    )
                    phase = "pointer_switched"

                if phase in {"pointer_switched", "readback_validated"}:
                    physical_fence_check()
                    active = read_active_generation(legacy)
                    if active is None or active.generation_id != generation_id:
                        raise CommunityGlobalDiscoveryRecoveryError(
                            "global_discovery_active_generation_mismatch"
                        )
                    physical_fence_check()
                    readback = self._runtime_factory(active.graph_path)
                    readback_close_error: BaseException | None = None
                    try:
                        (
                            schema_count,
                            counts_by_board,
                            semantic_fingerprint,
                        ) = self._validate_runtime(
                            readback,
                            ordered,
                            fence_check=physical_fence_check,
                        )
                        if semantic_fingerprint != journal.get("semantic_fingerprint"):
                            raise CommunityGlobalDiscoveryRecoveryError(
                                "global_discovery_readback_semantic_drift"
                            )
                    except BaseException as exc:
                        readback_close_error = exc
                        raise
                    finally:
                        # R3: revalidate the exact live fence before this
                        # WAL-checkpointing readback close (normal + exceptional).
                        self._fenced_readback_close(
                            readback,
                            physical_fence_check,
                            in_flight_error=readback_close_error,
                        )
                    _fsync_artifacts(
                        active.graph_path,
                        fence_check=physical_fence_check,
                    )
                    # Blocker 12: the terminal candidate_sha256 must reflect the
                    # FINAL post-readback bytes.  Fresh validation runs
                    # ``LOAD VECTOR`` which can grow the WAL, so recompute the
                    # snapshot AFTER close + fsync and persist that SHA in
                    # readback_validated (and thus completed).  Otherwise the
                    # resume-with-marker SHA re-conquest would reject an intact
                    # generation on self-induced WAL drift.
                    physical_fence_check()
                    post_readback = _snapshot(
                        active.graph_path,
                        fence_check=physical_fence_check,
                    )
                    if not post_readback.exists:
                        raise CommunityGlobalDiscoveryRecoveryError(
                            "global_discovery_candidate_missing"
                        )
                    candidate_sha = post_readback.sha256
                    journal = {
                        **journal,
                        "phase": "readback_validated",
                        "candidate_sha256": candidate_sha,
                        "schema_object_count": schema_count,
                        "counts_by_board": counts_by_board,
                        "semantic_fingerprint": semantic_fingerprint,
                        "directory_fsync_supported": directory_fsync_supported,
                    }
                    directory_fsync_supported &= _write_journal_with_directory_fsync(
                        journal_path,
                        journal,
                        fence_check=physical_fence_check,
                    )

                # COMPLETED-FIRST terminal order (Nexus
                # msg_20533dbbce3741248416fc0e53b7ea4e): persist terminal
                # `phase=completed` + fsync BEFORE clearing the marker.  Never
                # clear before terminal evidence.  Order:
                # readback_validated -> revalidate fence -> completed + fsync ->
                # revalidate fence -> clear marker + directory fsync.
                physical_fence_check()
                completed = {
                    **journal,
                    "phase": "completed",
                    "outcome": "completed",
                    "rollback_performed": False,
                    "candidate_sha256": candidate_sha,
                    "schema_object_count": schema_count,
                    "directory_fsync_supported": directory_fsync_supported,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                # R4: persist the PENDING (clear_settled=False) completed journal
                # FIRST (COMPLETED-FIRST) — a fault HERE means the terminal journal
                # is not durable, so ``journal`` is still non-completed and the
                # outer handler rolls back and keeps the marker.  Fold the terminal
                # journal writer's OWN directory fsync into the aggregate
                # (pre-R4 behaviour).
                pending_write_supported = self._persist_pending_completed_journal(
                    journal_path=journal_path,
                    completed_journal=completed,
                    fence_check=physical_fence_check,
                )
                directory_fsync_supported &= bool(pending_write_supported)
                completed = {
                    **completed,
                    "directory_fsync_supported": directory_fsync_supported,
                }
                # Terminal evidence is now durable.  Key the rollback exclusion
                # STRUCTURALLY on ``journal.phase == "completed"`` (Nexus
                # msg_08f6fa2df8ab4e728144e12e30ca7c67): assign the terminal
                # journal (clear_settled still false) so a failure between here and
                # the clear leaves terminal completed + validated active generation
                # + marker (conservatively unreadable) and resume clears
                # idempotently.
                journal = {**completed, "clear_settled": False}
                # Clear + settle the FINAL aggregate.  A LATE fence/lease loss
                # AFTER the durable completed journal cannot mask success
                # (``swallow_late_fence_loss``): the clear defers to idempotent
                # resume and the conservative false (already on disk) is reported.
                directory_fsync_supported = self._clear_marker_and_settle(
                    legacy=legacy,
                    generation_id=generation_id,
                    journal_path=journal_path,
                    completed_journal=completed,
                    pre_clear_supported=directory_fsync_supported,
                    fence_check=physical_fence_check,
                    swallow_late_fence_loss=True,
                )
                return self._result_from_journal(
                    {**journal, "directory_fsync_supported": directory_fsync_supported}
                )
            except CommunityGlobalDiscoveryRecoveryFenceError:
                # Losing either the durable dispatch token or writer lease is
                # not a candidate failure.  Do not write failure/rollback
                # evidence under a stale owner; the next exact-token claimant
                # reconciles the existing journal and generation pointer.
                raise
            except (GlobalDiscoveryWriterFenceLost, GraphLockContention):
                # R3: a writer-authority loss or single-writer lock contention
                # (e.g. from a WAL-checkpointing close after the last validation)
                # is NOT a candidate failure and must NEVER be reclassified into
                # rollback / candidate_build_failed.  Propagate unchanged so the
                # next exact-token claimant reconciles the durable journal/pointer.
                raise
            except Exception as original_exc:
                if str((journal or {}).get("phase")) == "completed":
                    # Rollback exclusion keyed STRUCTURALLY on the durable
                    # terminal phase (Nexus msg_08f6fa2df8ab4e728144e12e30ca7c67).
                    # The terminal `completed` journal is already durable; a
                    # failure while revalidating/clearing the marker must never
                    # restore an earlier partial primary.  Leave completed +
                    # validated active generation + marker so a fresh process is
                    # conservatively unreadable and resume clears idempotently.
                    raise
                try:
                    physical_fence_check()
                    active = read_active_generation(legacy)
                    pointer_owned = (
                        active is not None and active.generation_id == generation_id
                    )
                except GlobalDiscoveryLayoutError:
                    pointer_owned = True
                if pointer_owned:
                    code = str(
                        getattr(
                            original_exc,
                            "code",
                            "global_discovery_post_cutover_readback_failed",
                        )
                    )
                    rollback_pending = {
                        **(journal or {}),
                        "run_id": run_id,
                        "epoch": epoch,
                        "attempt_id": attempt_id,
                        "generation_id": generation_id,
                        "phase": "rollback_pending",
                        "outcome": "rolling_back",
                        "rollback_performed": False,
                        "failure_code": code,
                        "candidate_sha256": candidate_sha,
                        "schema_object_count": schema_count,
                        "source_fingerprint": source_fingerprint,
                        "quarantine_ref": quarantine_ref,
                        "directory_fsync_supported": directory_fsync_supported,
                    }
                    directory_fsync_supported &= _write_journal_with_directory_fsync(
                        journal_path,
                        rollback_pending,
                        fence_check=physical_fence_check,
                    )
                    journal = rollback_pending
                    try:
                        directory_fsync_supported &= self._restore_previous(
                            legacy=legacy,
                            previous_generation_id=previous_generation_id,
                            previous_manifest_sha256=previous_manifest_sha256,
                            fence_check=physical_fence_check,
                        )
                    except CommunityGlobalDiscoveryRecoveryFenceError:
                        raise
                    except Exception as rollback_exc:
                        raise CommunityGlobalDiscoveryRecoveryError(
                            "global_discovery_rollback_failed"
                        ) from rollback_exc
                    rolled_back = {
                        **(journal or {}),
                        "run_id": run_id,
                        "epoch": epoch,
                        "attempt_id": attempt_id,
                        "generation_id": generation_id,
                        "phase": "rolled_back",
                        "outcome": "rolled_back",
                        "rollback_performed": True,
                        "failure_code": code,
                        "candidate_sha256": candidate_sha,
                        "schema_object_count": schema_count,
                        "source_fingerprint": source_fingerprint,
                        "quarantine_ref": quarantine_ref,
                        "directory_fsync_supported": directory_fsync_supported,
                        "rolled_back_at": datetime.now(timezone.utc).isoformat(),
                    }
                    _write_journal_with_directory_fsync(
                        journal_path,
                        rolled_back,
                        fence_check=physical_fence_check,
                    )
                    return self._result_from_journal(rolled_back)
                code = str(
                    getattr(
                        original_exc,
                        "code",
                        "global_discovery_candidate_build_failed",
                    )
                )
                failed = {
                    **(journal or {}),
                    "run_id": run_id,
                    "epoch": epoch,
                    "attempt_id": attempt_id,
                    "generation_id": generation_id,
                    "phase": "building",
                    "outcome": "failed",
                    "failure_code": code,
                    "source_fingerprint": source_fingerprint,
                    "quarantine_ref": quarantine_ref,
                    "directory_fsync_supported": directory_fsync_supported,
                }
                _write_journal_with_directory_fsync(
                    journal_path,
                    failed,
                    fence_check=physical_fence_check,
                )
                raise CommunityGlobalDiscoveryRecoveryError(code) from original_exc


__all__ = [
    "CommunityGlobalDiscoveryRecovery",
    "CommunityGlobalDiscoveryRecoveryError",
    "CommunityGlobalDiscoveryRecoveryFenceError",
    "CommunityPreparedRecoveryRevoker",
    "CommunityRecoveryAttemptReconciliation",
    "CommunityRelationalRecoverySnapshotFingerprint",
    "CommunityRecoverySnapshotFingerprint",
    "CommunitySourceRevisionFence",
]
