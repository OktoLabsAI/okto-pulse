"""Fail-closed physical recovery for Grafx Global Discovery generations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from okto_grafx import Database
from okto_grafx.errors import GrafxError
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryCutoverResult,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.ports.global_discovery_recovery_control import (
    recovery_attempt_id,
)

from okto_pulse.community.adapters.filesystem_erasure import (
    is_filesystem_alias,
    reject_filesystem_alias_ancestry,
    remove_contained_tree,
    validate_scope_id,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    GENERATION_MANIFEST_FILENAME,
    active_pointer_path,
    canonical_sha256,
    generations_root,
    restore_legacy_generation,
    switch_active_generation,
    validate_generation_id,
    write_generation_manifest,
    write_json_atomic,
)
from okto_pulse.community.adapters.grafx_global_discovery import (
    PULSE_GRAFX_GLOBAL_SCHEMA,
    certify_grafx_global_vector_indexes,
    ensure_current_grafx_global_schema,
    validate_current_grafx_global_schema,
)
from okto_pulse.community.adapters.grafx_global_discovery_runtime import (
    CommunityGrafxGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.grafx_global_operational import (
    GlobalAdmission,
    GlobalCloseCallback,
    GlobalFenceRevalidator,
    GlobalPathResolver,
    has_grafx_identity,
    normalize_grafx_value,
    read_safe_active_generation,
    require_global_grafx_admission,
    resolved_global_graph_path,
    safe_global_generation_dir,
    safe_global_generation_graph_path,
    snapshot_global_artifact,
    validate_plain_global_artifact,
)
from okto_pulse.community.adapters.grafx_schema_manifest import EMBEDDING_DIMENSION

CandidateDatabaseFactory = Callable[[Path], Database]
SnapshotFingerprintProvider = Callable[[], str]

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SCHEMA = frozenset({"Board", "DecisionDigest", "CONTAINS_DECISION"})
_FIXED_TIMESTAMP = "1970-01-01T00:00:00Z"
_JOURNAL_FILENAME = "recovery_journal.json"
_TERMINAL_JOURNAL_KIND = "grafx_global_discovery_terminal"
_PREDECESSOR_JOURNAL_KIND = "grafx_global_discovery_reconcile_predecessor"
_RECOVERY_MANIFEST_KIND = "grafx_global_discovery_recovery"
_ADOPTION_MANIFEST_KIND = "grafx_global_discovery_recovery_adoption"


@dataclass(frozen=True, slots=True)
class CommunityGrafxRecoveryAttemptReconciliation:
    """Bounded retention outcome for Grafx attempt-owned evidence."""

    quarantined_ids: tuple[str, ...] = ()
    retained_ids: tuple[str, ...] = ()
    deleted_ids: tuple[str, ...] = ()


class CommunityGrafxGlobalDiscoveryRecoveryError(RuntimeError):
    """Stable Community recovery failure without a Grafx exception payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CommunityGrafxGlobalDiscoveryFenceError(
    CommunityGrafxGlobalDiscoveryRecoveryError
):
    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__("global_discovery_writer_fence_lost")


def _assert_call_fence(fence_check: Callable[[], None]) -> None:
    try:
        fence_check()
    except CommunityGrafxGlobalDiscoveryFenceError:
        raise
    except Exception as exc:
        raise CommunityGrafxGlobalDiscoveryFenceError(exc) from exc


def _generation_id(*, run_id: str, epoch: int, attempt_id: str) -> str:
    binding = f"{run_id}\0{epoch}\0{attempt_id}".encode()
    return f"gdr_{hashlib.sha256(binding).hexdigest()[:32]}"


def _adoption_generation_id(*, run_id: str, epoch: int, attempt_id: str) -> str:
    return _generation_id(
        run_id=run_id,
        epoch=epoch,
        attempt_id=f"{attempt_id}\0adoption",
    )


def _digest_id(board_id: str, original_node_id: str) -> str:
    return f"dd_{board_id[:8]}_{original_node_id}"


def _vector(value: object) -> list[float]:
    try:
        values = [float(item) for item in value]  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CommunityGrafxGlobalDiscoveryRecoveryError(
            "global_discovery_candidate_embedding_invalid"
        ) from exc
    if len(values) != EMBEDDING_DIMENSION or any(
        not math.isfinite(item) for item in values
    ):
        raise CommunityGrafxGlobalDiscoveryRecoveryError(
            "global_discovery_candidate_embedding_invalid"
        )
    return values


def _ordered_boards(
    boards: tuple[GlobalDiscoveryBoardSeed, ...],
) -> tuple[GlobalDiscoveryBoardSeed, ...]:
    ordered = tuple(sorted(boards, key=lambda item: item.board_id))
    if not ordered or len({item.board_id for item in ordered}) != len(ordered):
        raise CommunityGrafxGlobalDiscoveryRecoveryError(
            "global_discovery_candidate_board_inventory_invalid"
        )
    digest_ids: set[str] = set()
    semantic_ids: set[tuple[str, str]] = set()
    for board in ordered:
        validate_scope_id(board.board_id)
        if not board.source_inventory_hash:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_source_inventory_hash_missing"
            )
        _vector(board.summary_embedding)
        for digest in board.digests:
            if digest.graph_layer not in {"canonical", "working"}:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_candidate_graph_layer_invalid"
                )
            identity = (board.board_id, digest.original_node_id)
            physical_id = _digest_id(*identity)
            if identity in semantic_ids or physical_id in digest_ids:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_candidate_digest_inventory_invalid"
                )
            semantic_ids.add(identity)
            digest_ids.add(physical_id)
            _vector(digest.embedding)
    return ordered


def _expected_projection(
    boards: tuple[GlobalDiscoveryBoardSeed, ...],
) -> dict[str, object]:
    board_rows: list[dict[str, object]] = []
    digest_rows: list[dict[str, object]] = []
    link_rows: list[dict[str, str]] = []
    for board in boards:
        board_rows.append(
            {
                "board_id": board.board_id,
                "name": board.board_name or board.board_id,
                "summary": board.summary,
                "summary_embedding": _vector(board.summary_embedding),
                "decision_count": len(board.digests),
            }
        )
        for digest in sorted(board.digests, key=lambda item: item.original_node_id):
            physical_id = _digest_id(board.board_id, digest.original_node_id)
            digest_rows.append(
                {
                    "id": physical_id,
                    "board_id": board.board_id,
                    "original_node_id": digest.original_node_id,
                    "title": digest.title,
                    "summary": digest.summary,
                    "node_type": digest.node_type,
                    "graph_layer": digest.graph_layer,
                    "embedding": _vector(digest.embedding),
                }
            )
            link_rows.append({"board_id": board.board_id, "digest_id": physical_id})
    return {
        "boards": board_rows,
        "digests": sorted(
            digest_rows,
            key=lambda item: (str(item["board_id"]), str(item["id"])),
        ),
        "links": sorted(
            link_rows,
            key=lambda item: (item["board_id"], item["digest_id"]),
        ),
    }


def _actual_projection(
    runtime: CommunityGrafxGlobalDiscoveryRuntime,
) -> dict[str, object]:
    boards = runtime.execute(
        "MATCH (b:Board) RETURN b.board_id, b.name, b.summary, "
        "b.summary_embedding, b.decision_count ORDER BY b.board_id"
    ).rows
    digests = runtime.execute(
        "MATCH (d:DecisionDigest) RETURN d.id, d.board_id, "
        "d.original_node_id, d.title, d.one_line_summary, d.node_type, "
        "d.graph_layer, d.embedding ORDER BY d.board_id, d.id"
    ).rows
    links = runtime.execute(
        "MATCH (b:Board)-[:CONTAINS_DECISION]->(d:DecisionDigest) "
        "RETURN b.board_id, d.id ORDER BY b.board_id, d.id"
    ).rows
    return {
        "boards": [
            {
                "board_id": str(row[0]),
                "name": str(row[1] or row[0]),
                "summary": str(row[2] or ""),
                "summary_embedding": _vector(normalize_grafx_value(row[3])),
                "decision_count": int(row[4] or 0),
            }
            for row in boards
        ],
        "digests": [
            {
                "id": str(row[0]),
                "board_id": str(row[1]),
                "original_node_id": str(row[2]),
                "title": str(row[3] or ""),
                "summary": str(row[4] or ""),
                "node_type": str(row[5] or ""),
                "graph_layer": str(row[6]),
                "embedding": _vector(normalize_grafx_value(row[7])),
            }
            for row in digests
        ],
        "links": [{"board_id": str(row[0]), "digest_id": str(row[1])} for row in links],
    }


def _snapshot_certified_generation(
    graph_path: Path,
    *,
    fence_check: Callable[[], None],
) -> GlobalDiscoveryArtifactSnapshot:
    """Fingerprint durable Grafx bytes while excluding coordination debris.

    Grafx opens create per-process advisory ``control`` files whose names and
    bytes intentionally change between otherwise read-only opens.  They are not
    database content; ``control/commit.state`` is the sole durable publication
    record in that namespace and remains part of this evidence hash.
    """

    graph = Path(graph_path)
    reject_filesystem_alias_ancestry(graph.parent)
    if is_filesystem_alias(graph):
        raise CommunityGrafxGlobalDiscoveryRecoveryError(
            "global_discovery_candidate_linked_artifact"
        )
    try:
        root_metadata = graph.lstat()
    except FileNotFoundError:
        return GlobalDiscoveryArtifactSnapshot(
            False, 0, 0, hashlib.sha256().hexdigest()
        )
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise CommunityGrafxGlobalDiscoveryRecoveryError(
            "global_discovery_candidate_artifact_kind_invalid"
        )

    files: list[Path] = []

    def collect(directory: Path) -> None:
        fence_check()
        reject_filesystem_alias_ancestry(directory.parent)
        if is_filesystem_alias(directory):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_linked_artifact"
            )
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda item: item.name)
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(graph).as_posix()
            metadata = path.lstat()
            if is_filesystem_alias(path):
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_candidate_linked_artifact"
                )
            if stat.S_ISDIR(metadata.st_mode):
                if relative == "control/readers":
                    continue
                collect(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_candidate_artifact_kind_invalid"
                )
            if relative.startswith("control/") and relative != "control/commit.state":
                continue
            files.append(path)

    collect(graph)
    digest = hashlib.sha256()
    total = 0
    base = graph.parent
    for path in files:
        fence_check()
        relative = path.relative_to(base).as_posix().encode()
        before = path.lstat()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            while True:
                fence_check()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
        if is_filesystem_alias(path):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_linked_artifact"
            )
        after = path.lstat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_changed_during_snapshot"
            )
    fence_check()
    return GlobalDiscoveryArtifactSnapshot(
        exists=has_grafx_identity(graph),
        artifact_count=len(files),
        total_bytes=total,
        sha256=digest.hexdigest(),
    )


class CommunityGrafxGlobalDiscoveryRecovery:
    """Build and certify an inactive Grafx generation before pointer cutover."""

    def __init__(
        self,
        path_resolver: GlobalPathResolver,
        candidate_database_factory: CandidateDatabaseFactory,
        close_callback: GlobalCloseCallback,
        revalidate_fence: GlobalFenceRevalidator,
        *,
        admission: GlobalAdmission | None = None,
        snapshot_fingerprint_provider: SnapshotFingerprintProvider | None = None,
    ) -> None:
        self._path_resolver = path_resolver
        self._candidate_database_factory = candidate_database_factory
        self._close_callback = close_callback
        self._revalidate_fence = revalidate_fence
        self._admission = admission
        self._snapshot_fingerprint_provider = snapshot_fingerprint_provider

    def bind_snapshot_fingerprint_provider(
        self,
        provider: SnapshotFingerprintProvider,
    ) -> None:
        """Late-bind the relational freshness fence exactly once by identity."""

        if not callable(provider):
            raise TypeError("snapshot fingerprint provider must be callable")
        current = self._snapshot_fingerprint_provider
        if current is not None and current is not provider:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_snapshot_fingerprint_already_bound"
            )
        self._snapshot_fingerprint_provider = provider

    def _fence(
        self,
        phase: str,
        call_fence: Callable[[], None] | None = None,
    ) -> None:
        try:
            self._revalidate_fence(phase)
        except CommunityGrafxGlobalDiscoveryFenceError:
            raise
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryFenceError(exc) from exc
        if call_fence is not None:
            _assert_call_fence(call_fence)

    def _close_database(
        self,
        database: Database,
        *,
        phase: str,
        call_fence: Callable[[], None],
        error_code: str,
    ) -> None:
        self._fence(phase, call_fence)
        try:
            database.close()
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(error_code) from exc

    @staticmethod
    def _assert_database_path_safe(path: Path) -> None:
        reject_filesystem_alias_ancestry(path.parent)
        try:
            path.lstat()
        except FileNotFoundError:
            return
        validate_plain_global_artifact(path)

    def inspect_live_artifact(self) -> GlobalDiscoveryArtifactSnapshot:
        try:
            legacy = Path(self._path_resolver())
            return snapshot_global_artifact(legacy)
        except CommunityGrafxGlobalDiscoveryRecoveryError:
            raise
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_artifact_snapshot_failed"
            ) from exc

    def current_snapshot_fingerprint(self) -> str:
        provider = self._snapshot_fingerprint_provider
        if provider is None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_snapshot_fingerprint_unavailable"
            )
        try:
            fingerprint = str(provider()).strip()
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_snapshot_fingerprint_unavailable"
            ) from exc
        if not fingerprint:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_snapshot_fingerprint_unavailable"
            )
        return fingerprint

    @staticmethod
    def _worker_identity(
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str | None = None,
    ) -> str:
        try:
            normalized_run_id = validate_generation_id(run_id)
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            ) from exc
        if (
            isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 1
            or attempt_id != recovery_attempt_id(normalized_run_id, epoch)
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            )
        if (
            expected_live_sha256 is not None
            and _HEX_SHA256.fullmatch(expected_live_sha256) is None
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_expected_live_sha_invalid"
            )
        return normalized_run_id

    @staticmethod
    def _attempt_directory(
        legacy: Path,
        *,
        run_id: str,
        epoch: int,
    ) -> Path:
        root = Path(
            os.path.abspath(legacy.parent / "quarantine" / "global-discovery" / run_id)
        )
        reject_filesystem_alias_ancestry(root.parent)
        if is_filesystem_alias(root):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_artifact_unsafe"
            )
        attempt = Path(os.path.abspath(root / f"attempt-{epoch}"))
        try:
            attempt.relative_to(root)
        except ValueError as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_artifact_unsafe"
            ) from exc
        return attempt

    def _read_attempt_journal(
        self,
        path: Path,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        call_fence: Callable[[], None],
    ) -> tuple[dict[str, object], str] | None:
        self._fence("recovery_journal_read", call_fence)
        reject_filesystem_alias_ancestry(path.parent)
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        if is_filesystem_alias(path) or not stat.S_ISREG(before.st_mode):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_unsafe"
            )
        try:
            raw_bytes = path.read_bytes()
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_unreadable"
            ) from exc
        self._fence("recovery_journal_readback", call_fence)
        if is_filesystem_alias(path):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_unsafe"
            )
        after = path.lstat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_changed"
            )
        if not isinstance(raw, dict):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_unreadable"
            )
        supplied_sha = str(raw.get("journal_sha256") or "")
        binding = {key: value for key, value in raw.items() if key != "journal_sha256"}
        if (
            _HEX_SHA256.fullmatch(supplied_sha) is None
            or supplied_sha != canonical_sha256(binding)
            or raw.get("run_id") != run_id
            or raw.get("epoch") != epoch
            or raw.get("attempt_id") != attempt_id
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_binding_invalid"
            )
        return raw, hashlib.sha256(raw_bytes).hexdigest()

    def _write_attempt_journal(
        self,
        path: Path,
        payload: dict[str, object],
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        call_fence: Callable[[], None],
    ) -> dict[str, object]:
        binding = dict(payload)
        binding["directory_fsync_supported"] = False
        document = {**binding, "journal_sha256": canonical_sha256(binding)}
        existing = self._read_attempt_journal(
            path,
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            call_fence=call_fence,
        )
        if existing is not None:
            if existing[0] != document:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_journal_binding_conflict"
                )
            return existing[0]
        self._fence("recovery_journal_prepare", call_fence)
        reject_filesystem_alias_ancestry(path.parent.parent)
        if is_filesystem_alias(path.parent):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_artifact_unsafe"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        reject_filesystem_alias_ancestry(path.parent)
        if is_filesystem_alias(path):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_unsafe"
            )
        self._fence("recovery_journal_commit", call_fence)
        write_json_atomic(path, document)
        persisted = self._read_attempt_journal(
            path,
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            call_fence=call_fence,
        )
        if persisted is None or persisted[0] != document:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_not_durable"
            )
        return persisted[0]

    @staticmethod
    def _result_from_journal(
        journal: dict[str, object],
    ) -> GlobalDiscoveryCutoverResult:
        evidence_ref = str(journal.get("evidence_ref") or "") or None
        return GlobalDiscoveryCutoverResult(
            outcome=str(journal.get("outcome") or ""),
            candidate_sha256=str(journal.get("candidate_sha256") or ""),
            quarantine_ref=evidence_ref,
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
            cutover_atomicity="atomic_pointer_replace",
            recovery_journal_ref=evidence_ref,
        )

    @staticmethod
    def _input_fingerprints(
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
    ) -> tuple[
        tuple[GlobalDiscoveryBoardSeed, ...],
        dict[str, object],
        str,
        str,
    ]:
        ordered = _ordered_boards(boards)
        expected = _expected_projection(ordered)
        return (
            ordered,
            expected,
            canonical_sha256([row.to_dict() for row in ordered]),
            canonical_sha256(expected),
        )

    def _validate_terminal_journal(
        self,
        journal: dict[str, object],
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        call_fence: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult:
        _, expected, source_fingerprint, expected_semantic = self._input_fingerprints(
            boards
        )

        def invalid(field: str) -> None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                f"global_discovery_recovery_journal_invalid:{field}"
            )

        if journal.get("kind") != _TERMINAL_JOURNAL_KIND:
            invalid("kind")
        if journal.get("phase") != "completed" or journal.get("outcome") != "completed":
            invalid("terminal")
        if journal.get("rollback_performed") is not False:
            invalid("rollback_performed")
        if journal.get("expected_live_sha256") != expected_live_sha256:
            invalid("expected_live_sha256")
        if journal.get("source_fingerprint") != source_fingerprint:
            invalid("source_fingerprint")
        if journal.get("expected_semantic_fingerprint") != expected_semantic:
            invalid("expected_semantic_fingerprint")
        expected_ref = f"grafx-global-discovery-recovery:{attempt_id}"
        if journal.get("evidence_ref") != expected_ref:
            invalid("evidence_ref")
        generation_id = str(journal.get("generation_id") or "")
        manifest_kind = str(journal.get("manifest_kind") or "")
        expected_generation = {
            _RECOVERY_MANIFEST_KIND: _generation_id(
                run_id=run_id,
                epoch=epoch,
                attempt_id=attempt_id,
            ),
            _ADOPTION_MANIFEST_KIND: _adoption_generation_id(
                run_id=run_id,
                epoch=epoch,
                attempt_id=attempt_id,
            ),
        }.get(manifest_kind)
        if expected_generation is None or generation_id != expected_generation:
            invalid("generation_id")
        candidate_sha = str(journal.get("candidate_sha256") or "")
        manifest_sha = str(journal.get("generation_manifest_sha256") or "")
        manifest_semantic = str(journal.get("manifest_semantic_fingerprint") or "")
        schema_count = journal.get("schema_object_count")
        if (
            _HEX_SHA256.fullmatch(candidate_sha) is None
            or _HEX_SHA256.fullmatch(manifest_sha) is None
            or _HEX_SHA256.fullmatch(manifest_semantic) is None
            or isinstance(schema_count, bool)
            or not isinstance(schema_count, int)
            or schema_count < 1
        ):
            invalid("physical_evidence")

        self._fence("recovery_journal_active", call_fence)
        legacy = Path(self._path_resolver())
        active = read_safe_active_generation(legacy)
        if (
            active is None
            or active.generation_id != generation_id
            or active.manifest_sha256 != manifest_sha
        ):
            invalid("active_generation")
        manifest = self._manifest_document(
            active.graph_path.parent / GENERATION_MANIFEST_FILENAME
        )
        if (
            manifest.get("kind") != manifest_kind
            or manifest.get("run_id") != run_id
            or manifest.get("epoch") != epoch
            or manifest.get("attempt_id") != attempt_id
            or manifest.get("expected_live_sha256") != expected_live_sha256
            or manifest.get("candidate_sha256") != candidate_sha
            or manifest.get("semantic_fingerprint") != manifest_semantic
            or manifest.get("schema_object_count") != schema_count
        ):
            invalid("generation_manifest")
        if manifest_kind == _RECOVERY_MANIFEST_KIND and (
            manifest.get("source_fingerprint") != source_fingerprint
            or manifest_semantic != expected_semantic
        ):
            invalid("generation_source")
        active_snapshot = _snapshot_certified_generation(
            active.graph_path,
            fence_check=lambda: self._fence(
                "recovery_journal_active_snapshot", call_fence
            ),
        )
        if not active_snapshot.exists or active_snapshot.sha256 != candidate_sha:
            invalid("candidate_sha256")

        self._fence("recovery_journal_readback_open", call_fence)
        self._assert_database_path_safe(active.graph_path)
        try:
            database = self._candidate_database_factory(active.graph_path)
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_readback_open_failed"
            ) from exc
        try:
            require_global_grafx_admission(database, self._admission)
            validate_current_grafx_global_schema(database)
            certify_grafx_global_vector_indexes(database)
            runtime = self._runtime_for_database(
                database,
                active.graph_path,
                call_fence,
            )
            if manifest_kind == _RECOVERY_MANIFEST_KIND:
                fresh_schema = self._validate_runtime(runtime, expected)
                fresh_semantic = expected_semantic
            else:
                fresh_schema, projection = self._validate_complete_projection(runtime)
                fresh_semantic = canonical_sha256(projection)
            if fresh_schema != schema_count or fresh_semantic != manifest_semantic:
                invalid("fresh_semantic")
        finally:
            self._close_database(
                database,
                phase="recovery_journal_readback_close",
                call_fence=call_fence,
                error_code="global_discovery_readback_close_failed",
            )
        return self._result_from_journal(journal)

    def _record_completed_attempt(
        self,
        result: GlobalDiscoveryCutoverResult,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        call_fence: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult:
        # Preserve the established direct adapter API used by unit-level callers
        # with legacy, non-worker attempt labels.  The production worker always
        # supplies the canonical identity and receives durable attempt evidence.
        try:
            normalized_run_id = self._worker_identity(
                run_id=run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=expected_live_sha256,
            )
        except CommunityGrafxGlobalDiscoveryRecoveryError:
            return result
        _, _, source_fingerprint, expected_semantic = self._input_fingerprints(boards)
        legacy = Path(self._path_resolver())
        attempt_dir = self._attempt_directory(
            legacy,
            run_id=normalized_run_id,
            epoch=epoch,
        )
        journal_path = attempt_dir / _JOURNAL_FILENAME
        existing = self._read_attempt_journal(
            journal_path,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            call_fence=call_fence,
        )
        if existing is not None:
            return self._validate_terminal_journal(
                existing[0],
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=expected_live_sha256,
                boards=boards,
                call_fence=call_fence,
            )

        self._fence("recovery_journal_capture", call_fence)
        active = read_safe_active_generation(legacy)
        if active is None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_active_generation_mismatch"
            )
        expected_generations = {
            _generation_id(
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
            ): _RECOVERY_MANIFEST_KIND,
            _adoption_generation_id(
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
            ): _ADOPTION_MANIFEST_KIND,
        }
        manifest_kind = expected_generations.get(active.generation_id)
        if manifest_kind is None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_active_generation_mismatch"
            )
        manifest = self._manifest_document(
            active.graph_path.parent / GENERATION_MANIFEST_FILENAME
        )
        candidate_sha = str(manifest.get("candidate_sha256") or "")
        manifest_semantic = str(manifest.get("semantic_fingerprint") or "")
        schema_count = manifest.get("schema_object_count")
        if (
            manifest.get("kind") != manifest_kind
            or manifest.get("run_id") != normalized_run_id
            or manifest.get("epoch") != epoch
            or manifest.get("attempt_id") != attempt_id
            or manifest.get("expected_live_sha256") != expected_live_sha256
            or candidate_sha != result.candidate_sha256
            or _HEX_SHA256.fullmatch(candidate_sha) is None
            or _HEX_SHA256.fullmatch(manifest_semantic) is None
            or isinstance(schema_count, bool)
            or not isinstance(schema_count, int)
            or schema_count != result.schema_object_count
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_completed_generation_binding_mismatch"
            )
        if manifest_kind == _RECOVERY_MANIFEST_KIND and (
            manifest.get("source_fingerprint") != source_fingerprint
            or manifest_semantic != expected_semantic
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_completed_generation_binding_mismatch"
            )
        active_snapshot = _snapshot_certified_generation(
            active.graph_path,
            fence_check=lambda: self._fence(
                "recovery_journal_capture_snapshot", call_fence
            ),
        )
        if not active_snapshot.exists or active_snapshot.sha256 != candidate_sha:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_sha_invalid"
            )
        payload: dict[str, object] = {
            "run_id": normalized_run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "kind": _TERMINAL_JOURNAL_KIND,
            "phase": "completed",
            "outcome": "completed",
            "rollback_performed": False,
            "expected_live_sha256": expected_live_sha256,
            "source_fingerprint": source_fingerprint,
            "expected_semantic_fingerprint": expected_semantic,
            "manifest_kind": manifest_kind,
            "generation_id": active.generation_id,
            "generation_manifest_sha256": active.manifest_sha256,
            "manifest_semantic_fingerprint": manifest_semantic,
            "candidate_sha256": candidate_sha,
            "schema_object_count": schema_count,
            "completed_at": datetime.now(UTC).isoformat(),
            "evidence_ref": f"grafx-global-discovery-recovery:{attempt_id}",
        }
        persisted = self._write_attempt_journal(
            journal_path,
            payload,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            call_fence=call_fence,
        )
        return self._validate_terminal_journal(
            persisted,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            boards=boards,
            call_fence=call_fence,
        )

    def reconcile_attempt_artifacts(
        self,
        *,
        run_id: str,
        known_attempt_ids: tuple[str, ...],
        now: datetime,
        fence_check: Callable[[], None],
    ) -> CommunityGrafxRecoveryAttemptReconciliation:
        """Prune only authenticated, terminal Grafx attempt evidence.

        Unknown, malformed and non-terminal artifacts are retained fail-closed.
        A generation referenced by the active authenticated pointer is never
        removed, even when its attempt is older than the retention window.
        """

        try:
            normalized_run_id = validate_generation_id(run_id)
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            ) from exc
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        known: dict[int, str] = {}
        prefix = f"{normalized_run_id}/attempt-"
        for raw_attempt_id in known_attempt_ids:
            attempt_id = str(raw_attempt_id)
            suffix = attempt_id[len(prefix) :] if attempt_id.startswith(prefix) else ""
            if not suffix.isdigit():
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_attempt_identity_invalid"
                )
            epoch = int(suffix)
            self._worker_identity(
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
            )
            if epoch in known:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_attempt_identity_invalid"
                )
            known[epoch] = attempt_id

        legacy = Path(self._path_resolver())
        root = self._attempt_directory(
            legacy,
            run_id=normalized_run_id,
            epoch=1,
        ).parent
        self._fence("recovery_retention_scan", fence_check)
        reject_filesystem_alias_ancestry(root.parent)
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            return CommunityGrafxRecoveryAttemptReconciliation()
        if is_filesystem_alias(root) or not stat.S_ISDIR(root_metadata.st_mode):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_artifact_unsafe"
            )

        expected_names = {f"attempt-{epoch}" for epoch in known}
        quarantined: list[str] = []
        self._fence("recovery_retention_enumerate", fence_check)
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.name not in expected_names:
                quarantined.append(f"{normalized_run_id}/{child.name}")

        active = read_safe_active_generation(legacy)
        active_generation_id = active.generation_id if active is not None else None
        directories: dict[int, Path] = {}
        journals: dict[int, dict[str, object] | None] = {}
        terminal_times: dict[int, datetime | None] = {}
        for epoch, attempt_id in sorted(known.items()):
            directory = self._attempt_directory(
                legacy,
                run_id=normalized_run_id,
                epoch=epoch,
            )
            self._fence("recovery_retention_attempt", fence_check)
            try:
                metadata = directory.lstat()
            except FileNotFoundError:
                continue
            if is_filesystem_alias(directory) or not stat.S_ISDIR(metadata.st_mode):
                quarantined.append(attempt_id)
                continue
            try:
                read = self._read_attempt_journal(
                    directory / _JOURNAL_FILENAME,
                    run_id=normalized_run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                    call_fence=fence_check,
                )
            except CommunityGrafxGlobalDiscoveryFenceError:
                raise
            except CommunityGrafxGlobalDiscoveryRecoveryError:
                quarantined.append(attempt_id)
                continue
            journal = read[0] if read is not None else None
            terminal_at: datetime | None = None
            if journal is not None:
                kind = journal.get("kind")
                expected_evidence_ref = f"grafx-global-discovery-recovery:{attempt_id}"
                structurally_terminal = (
                    kind in {_TERMINAL_JOURNAL_KIND, _PREDECESSOR_JOURNAL_KIND}
                    and journal.get("phase") == "completed"
                    and journal.get("outcome") == "completed"
                    and journal.get("rollback_performed") is False
                    and journal.get("evidence_ref") == expected_evidence_ref
                    and _HEX_SHA256.fullmatch(
                        str(journal.get("expected_live_sha256") or "")
                    )
                    is not None
                    and _HEX_SHA256.fullmatch(
                        str(journal.get("source_fingerprint") or "")
                    )
                    is not None
                    and _HEX_SHA256.fullmatch(
                        str(journal.get("expected_semantic_fingerprint") or "")
                    )
                    is not None
                    and _HEX_SHA256.fullmatch(
                        str(journal.get("candidate_sha256") or "")
                    )
                    is not None
                    and _HEX_SHA256.fullmatch(
                        str(journal.get("generation_manifest_sha256") or "")
                    )
                    is not None
                    and isinstance(journal.get("schema_object_count"), int)
                    and not isinstance(journal.get("schema_object_count"), bool)
                    and int(journal.get("schema_object_count") or 0) > 0
                )
                if kind == _TERMINAL_JOURNAL_KIND:
                    manifest_kind = str(journal.get("manifest_kind") or "")
                    expected_generation = {
                        _RECOVERY_MANIFEST_KIND: _generation_id(
                            run_id=normalized_run_id,
                            epoch=epoch,
                            attempt_id=attempt_id,
                        ),
                        _ADOPTION_MANIFEST_KIND: _adoption_generation_id(
                            run_id=normalized_run_id,
                            epoch=epoch,
                            attempt_id=attempt_id,
                        ),
                    }.get(manifest_kind)
                    structurally_terminal = structurally_terminal and (
                        journal.get("generation_id") == expected_generation
                    )
                else:
                    raw_ancestry = journal.get("ancestry")
                    structurally_terminal = structurally_terminal and isinstance(
                        raw_ancestry, list
                    )
                if not structurally_terminal:
                    quarantined.append(attempt_id)
                    continue
                try:
                    terminal_at = datetime.fromisoformat(
                        str(journal.get("completed_at") or "")
                    )
                except ValueError:
                    terminal_at = None
                if terminal_at is not None and (
                    terminal_at.tzinfo is None or terminal_at.utcoffset() is None
                ):
                    terminal_at = None
            directories[epoch] = directory
            journals[epoch] = journal
            terminal_times[epoch] = terminal_at

        active_epoch = max(known, default=None)
        superseded = set(directories) - {active_epoch}
        mandatory = {
            epoch
            for epoch in superseded
            if terminal_times[epoch] is None
            or (
                journals[epoch] is not None
                and journals[epoch].get("generation_id") == active_generation_id
            )
        }
        latest_policy = mandatory | set(sorted(superseded)[-3:])
        cutoff = now - timedelta(hours=24)
        younger_policy = mandatory | {
            epoch
            for epoch in superseded
            if terminal_times[epoch] is not None and terminal_times[epoch] >= cutoff
        }
        retained_superseded = (
            latest_policy
            if len(latest_policy) <= len(younger_policy)
            else younger_policy
        )
        retained_epochs = retained_superseded | (
            {active_epoch} if active_epoch else set()
        )
        retained_generation_ids = {
            str(journals[epoch].get("generation_id"))
            for epoch in retained_epochs
            if epoch in journals
            and journals[epoch] is not None
            and journals[epoch].get("generation_id")
        }

        retained: list[str] = []
        deleted: list[str] = []
        for epoch, directory in sorted(directories.items()):
            attempt_id = known[epoch]
            if epoch in retained_epochs:
                retained.append(attempt_id)
                continue
            journal = journals[epoch]
            if journal is None:
                retained.append(attempt_id)
                continue
            generation_id = str(journal.get("generation_id") or "")
            manifest_kind = str(journal.get("manifest_kind") or "")
            direct_generation = {
                _RECOVERY_MANIFEST_KIND: _generation_id(
                    run_id=normalized_run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                ),
                _ADOPTION_MANIFEST_KIND: _adoption_generation_id(
                    run_id=normalized_run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                ),
            }.get(manifest_kind)
            self._fence("recovery_retention_active_recheck", fence_check)
            current_active = read_safe_active_generation(legacy)
            if (
                current_active is not None
                and current_active.generation_id == generation_id
            ):
                retained.append(attempt_id)
                retained_generation_ids.add(generation_id)
                continue
            if (
                direct_generation == generation_id
                and generation_id not in retained_generation_ids
            ):
                generation_root = safe_global_generation_dir(legacy, generation_id)
                try:
                    generation_root.lstat()
                except FileNotFoundError:
                    pass
                else:
                    manifest = self._manifest_document(
                        generation_root / GENERATION_MANIFEST_FILENAME
                    )
                    supplied_manifest_sha = str(manifest.get("manifest_sha256") or "")
                    manifest_binding = {
                        key: value
                        for key, value in manifest.items()
                        if key != "manifest_sha256"
                    }
                    if (
                        supplied_manifest_sha != canonical_sha256(manifest_binding)
                        or supplied_manifest_sha
                        != journal.get("generation_manifest_sha256")
                        or manifest.get("run_id") != normalized_run_id
                        or manifest.get("epoch") != epoch
                        or manifest.get("attempt_id") != attempt_id
                        or manifest.get("kind") != manifest_kind
                    ):
                        quarantined.append(attempt_id)
                        retained.append(attempt_id)
                        continue
                    remove_contained_tree(
                        generation_root,
                        base_dir=generations_root(legacy),
                        before_mutation=lambda: self._fence(
                            "recovery_retention_generation", fence_check
                        ),
                    )
            remove_contained_tree(
                directory,
                base_dir=root,
                before_mutation=lambda: self._fence(
                    "recovery_retention_attempt_delete", fence_check
                ),
            )
            deleted.append(attempt_id)
        return CommunityGrafxRecoveryAttemptReconciliation(
            quarantined_ids=tuple(sorted(set(quarantined))),
            retained_ids=tuple(retained),
            deleted_ids=tuple(deleted),
        )

    @staticmethod
    def _normalized_ancestry(
        *,
        run_id: str,
        epoch: int,
        ancestry: tuple[tuple[int, str], ...],
    ) -> tuple[tuple[int, str], ...]:
        if not ancestry:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_identity_invalid"
            )
        normalized: list[tuple[int, str]] = []
        previous = epoch
        for index, (raw_epoch, raw_attempt_id) in enumerate(ancestry):
            predecessor_epoch = int(raw_epoch)
            predecessor_attempt_id = str(raw_attempt_id)
            if (
                isinstance(raw_epoch, bool)
                or predecessor_epoch < 1
                or predecessor_epoch >= previous
                or (index == 0 and predecessor_epoch != epoch - 1)
                or predecessor_attempt_id
                != recovery_attempt_id(run_id, predecessor_epoch)
            ):
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_recovery_attempt_identity_invalid"
                )
            normalized.append((predecessor_epoch, predecessor_attempt_id))
            previous = predecessor_epoch
        return tuple(normalized)

    def _validate_predecessor_journal(
        self,
        journal: dict[str, object],
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        call_fence: Callable[[], None],
        expected_ancestry: tuple[tuple[int, str], ...] | None = None,
    ) -> GlobalDiscoveryCutoverResult:
        _, _, source_fingerprint, expected_semantic = self._input_fingerprints(boards)

        def invalid(field: str) -> None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                f"global_discovery_reconcile_predecessor_journal_invalid:{field}"
            )

        if journal.get("kind") != _PREDECESSOR_JOURNAL_KIND:
            invalid("kind")
        if journal.get("phase") != "completed" or journal.get("outcome") != "completed":
            invalid("terminal")
        if journal.get("rollback_performed") is not False:
            invalid("rollback_performed")
        if journal.get("expected_live_sha256") != expected_live_sha256:
            invalid("expected_live_sha256")
        if journal.get("source_fingerprint") != source_fingerprint:
            invalid("source_fingerprint")
        if journal.get("expected_semantic_fingerprint") != expected_semantic:
            invalid("expected_semantic_fingerprint")
        expected_ref = f"grafx-global-discovery-recovery:{attempt_id}"
        if journal.get("evidence_ref") != expected_ref:
            invalid("evidence_ref")
        raw_ancestry = journal.get("ancestry")
        if not isinstance(raw_ancestry, list):
            invalid("ancestry")
        try:
            ancestry = tuple(
                (int(item[0]), str(item[1]))
                for item in raw_ancestry
                if isinstance(item, list) and len(item) == 2
            )
        except (TypeError, ValueError):
            invalid("ancestry")
            raise AssertionError("unreachable")
        if len(ancestry) != len(raw_ancestry):
            invalid("ancestry")
        try:
            ancestry = self._normalized_ancestry(
                run_id=run_id,
                epoch=epoch,
                ancestry=ancestry,
            )
        except CommunityGrafxGlobalDiscoveryRecoveryError:
            invalid("ancestry")
            raise AssertionError("unreachable")
        if expected_ancestry is not None and ancestry != expected_ancestry:
            invalid("ancestry_binding")
        predecessor_epoch = journal.get("predecessor_epoch")
        predecessor_attempt_id = str(journal.get("predecessor_attempt_id") or "")
        recorded = (predecessor_epoch, predecessor_attempt_id)
        if recorded not in ancestry:
            invalid("predecessor_identity")
        predecessor_index = ancestry.index(recorded)  # type: ignore[arg-type]

        legacy = Path(self._path_resolver())
        # The recorded source must be the first ancestry entry with durable
        # evidence.  A journal before it means the successor skipped a source.
        for earlier_epoch, _earlier_attempt_id in ancestry[:predecessor_index]:
            earlier_path = (
                self._attempt_directory(
                    legacy,
                    run_id=run_id,
                    epoch=earlier_epoch,
                )
                / _JOURNAL_FILENAME
            )
            self._fence("recovery_predecessor_order", call_fence)
            if earlier_path.exists():
                invalid("ancestry_skipped_source")

        predecessor_path = (
            self._attempt_directory(
                legacy,
                run_id=run_id,
                epoch=int(predecessor_epoch),
            )
            / _JOURNAL_FILENAME
        )
        predecessor_read = self._read_attempt_journal(
            predecessor_path,
            run_id=run_id,
            epoch=int(predecessor_epoch),
            attempt_id=predecessor_attempt_id,
            call_fence=call_fence,
        )
        if predecessor_read is None:
            invalid("predecessor_journal_missing")
        predecessor, predecessor_raw_sha = predecessor_read
        if journal.get("predecessor_journal_sha256") != predecessor_raw_sha:
            invalid("predecessor_journal_sha256")
        predecessor_kind = predecessor.get("kind")
        if predecessor_kind == _TERMINAL_JOURNAL_KIND:
            predecessor_result = self._validate_terminal_journal(
                predecessor,
                run_id=run_id,
                epoch=int(predecessor_epoch),
                attempt_id=predecessor_attempt_id,
                expected_live_sha256=expected_live_sha256,
                boards=boards,
                call_fence=call_fence,
            )
        elif predecessor_kind == _PREDECESSOR_JOURNAL_KIND:
            predecessor_result = self._validate_predecessor_journal(
                predecessor,
                run_id=run_id,
                epoch=int(predecessor_epoch),
                attempt_id=predecessor_attempt_id,
                expected_live_sha256=expected_live_sha256,
                boards=boards,
                call_fence=call_fence,
            )
        else:
            invalid("predecessor_kind")
            raise AssertionError("unreachable")
        if (
            journal.get("predecessor_evidence_ref")
            != predecessor_result.recovery_journal_ref
            or journal.get("reconciled_outcome") != predecessor_result.outcome
            or journal.get("generation_id") != predecessor.get("generation_id")
            or journal.get("generation_manifest_sha256")
            != predecessor.get("generation_manifest_sha256")
            or journal.get("manifest_kind") != predecessor.get("manifest_kind")
            or journal.get("manifest_semantic_fingerprint")
            != predecessor.get("manifest_semantic_fingerprint")
            or journal.get("candidate_sha256") != predecessor_result.candidate_sha256
            or journal.get("schema_object_count")
            != predecessor_result.schema_object_count
        ):
            invalid("predecessor_physical_evidence")
        return self._result_from_journal(journal)

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
        """Resolve only already-published Grafx truth; never create a candidate."""

        normalized_run_id = self._worker_identity(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
        )
        ordered, expected, source_fingerprint, _ = self._input_fingerprints(boards)
        legacy = Path(self._path_resolver())
        journal_path = (
            self._attempt_directory(
                legacy,
                run_id=normalized_run_id,
                epoch=epoch,
            )
            / _JOURNAL_FILENAME
        )
        existing = self._read_attempt_journal(
            journal_path,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            call_fence=fence_check,
        )
        if existing is not None:
            kind = existing[0].get("kind")
            if kind == _TERMINAL_JOURNAL_KIND:
                return self._validate_terminal_journal(
                    existing[0],
                    run_id=normalized_run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                    expected_live_sha256=expected_live_sha256,
                    boards=ordered,
                    call_fence=fence_check,
                )
            if kind == _PREDECESSOR_JOURNAL_KIND:
                return self._validate_predecessor_journal(
                    existing[0],
                    run_id=normalized_run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                    expected_live_sha256=expected_live_sha256,
                    boards=ordered,
                    call_fence=fence_check,
                )
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_journal_kind_invalid"
            )

        self._fence("recovery_reconcile_active", fence_check)
        active = read_safe_active_generation(legacy)
        if active is None:
            return None
        normal_generation = _generation_id(
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
        )
        adoption_generation = _adoption_generation_id(
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
        )
        reconciled: GlobalDiscoveryCutoverResult | None
        if active.generation_id == normal_generation:
            reconciled = self._completed_retry(
                legacy=legacy,
                generation_id=normal_generation,
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=expected_live_sha256,
                source_fingerprint=source_fingerprint,
                expected=expected,
                call_fence=fence_check,
            )
        elif active.generation_id == adoption_generation:
            reconciled = self._completed_adoption_retry(
                legacy=legacy,
                generation_id=adoption_generation,
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=expected_live_sha256,
                call_fence=fence_check,
            )
        else:
            return None
        if reconciled is None:
            return None
        return self._record_completed_attempt(
            reconciled,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            boards=ordered,
            call_fence=fence_check,
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
        """Bind a successor journal to already-crossed predecessor truth."""

        normalized_run_id = self._worker_identity(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
        )
        normalized_ancestry = self._normalized_ancestry(
            run_id=normalized_run_id,
            epoch=epoch,
            ancestry=ancestry,
        )
        ordered, _, source_fingerprint, expected_semantic = self._input_fingerprints(
            boards
        )
        legacy = Path(self._path_resolver())
        own_journal_path = (
            self._attempt_directory(
                legacy,
                run_id=normalized_run_id,
                epoch=epoch,
            )
            / _JOURNAL_FILENAME
        )
        existing = self._read_attempt_journal(
            own_journal_path,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            call_fence=fence_check,
        )
        if existing is not None:
            return self._validate_predecessor_journal(
                existing[0],
                run_id=normalized_run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=expected_live_sha256,
                boards=ordered,
                call_fence=fence_check,
                expected_ancestry=normalized_ancestry,
            )

        predecessor_epoch: int | None = None
        predecessor_attempt_id: str | None = None
        predecessor_result: GlobalDiscoveryCutoverResult | None = None
        for candidate_epoch, candidate_attempt_id in normalized_ancestry:
            self._fence("recovery_predecessor_reconcile", fence_check)
            candidate = self.reconcile_attempt_terminal_truth(
                run_id=normalized_run_id,
                epoch=candidate_epoch,
                attempt_id=candidate_attempt_id,
                expected_live_sha256=expected_live_sha256,
                boards=ordered,
                fence_check=fence_check,
            )
            if candidate is not None and candidate.outcome == "completed":
                predecessor_epoch = candidate_epoch
                predecessor_attempt_id = candidate_attempt_id
                predecessor_result = candidate
                break
        if predecessor_result is None or predecessor_attempt_id is None:
            return None

        predecessor_journal_path = (
            self._attempt_directory(
                legacy,
                run_id=normalized_run_id,
                epoch=int(predecessor_epoch),
            )
            / _JOURNAL_FILENAME
        )
        predecessor_read = self._read_attempt_journal(
            predecessor_journal_path,
            run_id=normalized_run_id,
            epoch=int(predecessor_epoch),
            attempt_id=predecessor_attempt_id,
            call_fence=fence_check,
        )
        if predecessor_read is None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_reconcile_predecessor_journal_missing"
            )
        predecessor, predecessor_raw_sha = predecessor_read
        payload: dict[str, object] = {
            "run_id": normalized_run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "kind": _PREDECESSOR_JOURNAL_KIND,
            "phase": "completed",
            "outcome": "completed",
            "rollback_performed": False,
            "expected_live_sha256": expected_live_sha256,
            "source_fingerprint": source_fingerprint,
            "expected_semantic_fingerprint": expected_semantic,
            "ancestry": [list(item) for item in normalized_ancestry],
            "predecessor_epoch": int(predecessor_epoch),
            "predecessor_attempt_id": predecessor_attempt_id,
            "predecessor_journal_sha256": predecessor_raw_sha,
            "predecessor_evidence_ref": predecessor_result.recovery_journal_ref,
            "reconciled_outcome": predecessor_result.outcome,
            "manifest_kind": predecessor.get("manifest_kind"),
            "generation_id": predecessor.get("generation_id"),
            "generation_manifest_sha256": predecessor.get("generation_manifest_sha256"),
            "manifest_semantic_fingerprint": predecessor.get(
                "manifest_semantic_fingerprint"
            ),
            "candidate_sha256": predecessor_result.candidate_sha256,
            "schema_object_count": predecessor_result.schema_object_count,
            "completed_at": datetime.now(UTC).isoformat(),
            "evidence_ref": f"grafx-global-discovery-recovery:{attempt_id}",
        }
        persisted = self._write_attempt_journal(
            own_journal_path,
            payload,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            call_fence=fence_check,
        )
        return self._validate_predecessor_journal(
            persisted,
            run_id=normalized_run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            boards=ordered,
            call_fence=fence_check,
            expected_ancestry=normalized_ancestry,
        )

    @staticmethod
    def _validate_runtime(
        runtime: CommunityGrafxGlobalDiscoveryRuntime,
        expected: dict[str, object],
    ) -> int:
        schema = set(runtime.list_schema_objects())
        if _REQUIRED_SCHEMA - schema:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_schema_missing"
            )
        if _actual_projection(runtime) != expected:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_semantic_mismatch"
            )
        return len(schema)

    @staticmethod
    def _validate_complete_projection(
        runtime: CommunityGrafxGlobalDiscoveryRuntime,
    ) -> tuple[int, dict[str, object]]:
        """Prove that a copied primary is complete and internally coherent."""

        schema = set(runtime.list_schema_objects())
        if _REQUIRED_SCHEMA - schema:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_complete_primary_schema_missing"
            )
        projection = _actual_projection(runtime)
        boards = list(projection["boards"])  # type: ignore[arg-type]
        digests = list(projection["digests"])  # type: ignore[arg-type]
        links = list(projection["links"])  # type: ignore[arg-type]
        if not boards or not digests:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_complete_primary_empty"
            )
        board_ids = [str(row["board_id"]) for row in boards]
        digest_ids = [str(row["id"]) for row in digests]
        semantic_ids = [
            (str(row["board_id"]), str(row["original_node_id"])) for row in digests
        ]
        if (
            any(not value for value in board_ids)
            or len(board_ids) != len(set(board_ids))
            or any(not value for value in digest_ids)
            or len(digest_ids) != len(set(digest_ids))
            or len(semantic_ids) != len(set(semantic_ids))
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_complete_primary_identity_invalid"
            )
        board_set = set(board_ids)
        digest_by_id = {str(row["id"]): row for row in digests}
        if any(str(row["board_id"]) not in board_set for row in digests):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_complete_primary_orphan_digest"
            )
        linked_ids = [str(row["digest_id"]) for row in links]
        if len(linked_ids) != len(set(linked_ids)) or set(linked_ids) != set(
            digest_ids
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_complete_primary_containment_invalid"
            )
        for link in links:
            digest = digest_by_id.get(str(link["digest_id"]))
            if digest is None or str(link["board_id"]) != str(digest["board_id"]):
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_complete_primary_ownership_invalid"
                )
        per_board: dict[str, int] = {}
        for digest in digests:
            owner = str(digest["board_id"])
            per_board[owner] = per_board.get(owner, 0) + 1
        if any(
            int(board["decision_count"]) != per_board.get(str(board["board_id"]), 0)
            for board in boards
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_complete_primary_count_invalid"
            )
        return len(schema), projection

    @staticmethod
    def _copy_tree_fenced(
        source: Path,
        destination: Path,
        fence_check: Callable[[], None],
    ) -> None:
        reject_filesystem_alias_ancestry(source.parent)
        metadata = source.lstat()
        if is_filesystem_alias(source):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_linked_artifact"
            )
        if stat.S_ISDIR(metadata.st_mode):
            fence_check()
            reject_filesystem_alias_ancestry(destination.parent)
            if is_filesystem_alias(destination):
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_adoption_linked_destination"
                )
            destination.mkdir(exist_ok=False)
            reject_filesystem_alias_ancestry(source.parent)
            if is_filesystem_alias(source):
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_adoption_linked_artifact"
                )
            with os.scandir(source) as entries:
                children = sorted(entries, key=lambda item: item.name)
            for child in children:
                CommunityGrafxGlobalDiscoveryRecovery._copy_tree_fenced(
                    Path(child.path),
                    destination / child.name,
                    fence_check,
                )
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_artifact_kind_unsupported"
            )
        reject_filesystem_alias_ancestry(source.parent)
        if is_filesystem_alias(source):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_linked_artifact"
            )
        before = source.lstat()
        fence_check()
        reject_filesystem_alias_ancestry(destination.parent)
        if is_filesystem_alias(destination):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_linked_destination"
            )
        if is_filesystem_alias(source):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_linked_artifact"
            )
        with source.open("rb") as reader, destination.open("xb") as writer:
            while True:
                fence_check()
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if is_filesystem_alias(source):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_linked_artifact"
            )
        after = source.lstat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_source_changed"
            )

    def _copy_live_graph_set(
        self,
        *,
        source_graph: Path,
        candidate_graph: Path,
        call_fence: Callable[[], None],
    ) -> None:
        reject_filesystem_alias_ancestry(source_graph.parent)
        if is_filesystem_alias(source_graph.parent):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_linked_parent"
            )
        sources = [source_graph]
        sources.extend(
            sorted(
                (
                    child
                    for child in source_graph.parent.iterdir()
                    if child.name.startswith(f"{source_graph.name}.")
                ),
                key=lambda child: child.name,
            )
        )
        for source in sources:
            destination = (
                candidate_graph
                if source == source_graph
                else candidate_graph.parent / source.name
            )
            self._copy_tree_fenced(
                source,
                destination,
                lambda: self._fence("recovery_adoption_copy", call_fence),
            )

    def _remove_unpublished_candidate(
        self,
        candidate_root: Path,
        call_fence: Callable[[], None],
    ) -> None:
        self._fence("recovery_adoption_cleanup", call_fence)
        try:
            remove_contained_tree(
                candidate_root,
                base_dir=candidate_root.parent,
                before_mutation=lambda: self._fence(
                    "recovery_adoption_cleanup", call_fence
                ),
            )
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_cleanup_failed"
            ) from exc

    def _completed_adoption_retry(
        self,
        *,
        legacy: Path,
        generation_id: str,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        call_fence: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult | None:
        active = read_safe_active_generation(legacy)
        if active is None or active.generation_id != generation_id:
            return None
        document = self._manifest_document(
            active.graph_path.parent / GENERATION_MANIFEST_FILENAME
        )
        required = {
            "kind": "grafx_global_discovery_recovery_adoption",
            "run_id": run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "expected_live_sha256": expected_live_sha256,
            "schema_fingerprint": PULSE_GRAFX_GLOBAL_SCHEMA.logical_fingerprint,
        }
        if any(document.get(key) != value for key, value in required.items()):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_completed_generation_binding_mismatch"
            )
        candidate_sha = str(document.get("candidate_sha256") or "")
        semantic_sha = str(document.get("semantic_fingerprint") or "")
        if (
            _HEX_SHA256.fullmatch(candidate_sha) is None
            or _HEX_SHA256.fullmatch(semantic_sha) is None
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_manifest_evidence_invalid"
            )
        self._fence("recovery_adoption_retry_open", call_fence)
        self._assert_database_path_safe(active.graph_path)
        try:
            database = self._candidate_database_factory(active.graph_path)
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_readback_open_failed"
            ) from exc
        try:
            require_global_grafx_admission(database, self._admission)
            validate_current_grafx_global_schema(database)
            certify_grafx_global_vector_indexes(database)
            runtime = self._runtime_for_database(
                database,
                active.graph_path,
                call_fence,
            )
            schema_count, projection = self._validate_complete_projection(runtime)
            if canonical_sha256(projection) != semantic_sha:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_adoption_semantic_mismatch"
                )
        finally:
            self._close_database(
                database,
                phase="recovery_adoption_retry_close",
                call_fence=call_fence,
                error_code="global_discovery_adoption_close_failed",
            )
        durable_snapshot = _snapshot_certified_generation(
            active.graph_path,
            fence_check=lambda: self._fence(
                "recovery_adoption_retry_snapshot", call_fence
            ),
        )
        if not durable_snapshot.exists or durable_snapshot.sha256 != candidate_sha:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_sha_invalid"
            )
        return GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256=candidate_sha,
            quarantine_ref=None,
            schema_object_count=schema_count,
            rollback_performed=False,
            directory_fsync_supported=bool(
                document.get("directory_fsync_supported", False)
            ),
            cutover_atomicity="atomic_pointer_replace",
            recovery_journal_ref=f"generation-manifest:{generation_id}",
        )

    def _try_adopt_complete_live(
        self,
        *,
        legacy: Path,
        generation_id: str,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        call_fence: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult | None:
        """Copy, self-certify and atomically adopt a complete live primary."""

        self._fence("recovery_adoption_close_live", call_fence)
        try:
            self._close_callback()
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_close_failed"
            ) from exc
        before = snapshot_global_artifact(
            legacy,
            fence_check=lambda: self._fence("recovery_adoption_snapshot", call_fence),
        )
        if before.sha256 != expected_live_sha256:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_snapshot_changed"
            )
        if not before.exists:
            return None
        source_graph = resolved_global_graph_path(legacy)
        source_snapshot = snapshot_global_artifact(
            source_graph,
            fence_check=lambda: self._fence(
                "recovery_adoption_source_snapshot", call_fence
            ),
        )
        if not source_snapshot.exists:
            return None
        candidate_root = safe_global_generation_dir(legacy, generation_id)
        candidate_path = safe_global_generation_graph_path(legacy, generation_id)
        reject_filesystem_alias_ancestry(candidate_root.parent)
        try:
            candidate_root.lstat()
        except FileNotFoundError:
            pass
        else:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_generation_already_exists"
            )
        self._fence("recovery_adoption_candidate", call_fence)
        reject_filesystem_alias_ancestry(candidate_root.parent)
        candidate_root.mkdir(parents=True, exist_ok=False)
        try:
            self._copy_live_graph_set(
                source_graph=source_graph,
                candidate_graph=candidate_path,
                call_fence=call_fence,
            )
        except CommunityGrafxGlobalDiscoveryRecoveryError:
            raise
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_copy_failed"
            ) from exc
        copied_source = snapshot_global_artifact(
            source_graph,
            fence_check=lambda: self._fence(
                "recovery_adoption_source_readback", call_fence
            ),
        )
        copied_candidate = snapshot_global_artifact(
            candidate_path,
            fence_check=lambda: self._fence(
                "recovery_adoption_copy_readback", call_fence
            ),
        )
        if copied_source.sha256 != source_snapshot.sha256:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_source_changed"
            )
        if (
            not copied_candidate.exists
            or copied_candidate.sha256 != source_snapshot.sha256
        ):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_copy_mismatch"
            )

        try:
            self._fence("recovery_adoption_open", call_fence)
            self._assert_database_path_safe(candidate_path)
            database = self._candidate_database_factory(candidate_path)
        except CommunityGrafxGlobalDiscoveryFenceError:
            raise
        except (GrafxError, OSError):
            self._remove_unpublished_candidate(candidate_root, call_fence)
            return None
        validation_failure = False
        projection: dict[str, object] | None = None
        schema_count = 0
        try:
            require_global_grafx_admission(database, self._admission)
            validate_current_grafx_global_schema(database)
            certify_grafx_global_vector_indexes(database)
            runtime = self._runtime_for_database(database, candidate_path, call_fence)
            schema_count, projection = self._validate_complete_projection(runtime)
            self._fence("recovery_adoption_flush", call_fence)
            database.flush()
            self._fence("recovery_adoption_checkpoint", call_fence)
            database.checkpoint()
        except CommunityGrafxGlobalDiscoveryFenceError:
            raise
        except (
            CommunityGrafxGlobalDiscoveryRecoveryError,
            GraphError,
            GrafxError,
            OSError,
        ):
            validation_failure = True
        finally:
            self._close_database(
                database,
                phase="recovery_adoption_close",
                call_fence=call_fence,
                error_code="global_discovery_adoption_close_failed",
            )
        if validation_failure or projection is None:
            self._remove_unpublished_candidate(candidate_root, call_fence)
            return None

        candidate_snapshot = _snapshot_certified_generation(
            candidate_path,
            fence_check=lambda: self._fence(
                "recovery_adoption_candidate_snapshot", call_fence
            ),
        )
        if not candidate_snapshot.exists:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_absent_after_close"
            )
        manifest_payload = {
            "kind": "grafx_global_discovery_recovery_adoption",
            "run_id": run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "expected_live_sha256": expected_live_sha256,
            "candidate_sha256": candidate_snapshot.sha256,
            "semantic_fingerprint": canonical_sha256(projection),
            "schema_fingerprint": PULSE_GRAFX_GLOBAL_SCHEMA.logical_fingerprint,
            "schema_object_count": schema_count,
            "board_count": len(projection["boards"]),  # type: ignore[arg-type]
            "digest_count": len(projection["digests"]),  # type: ignore[arg-type]
            "directory_fsync_supported": False,
        }
        current = snapshot_global_artifact(
            legacy,
            fence_check=lambda: self._fence(
                "recovery_adoption_pre_manifest", call_fence
            ),
        )
        if current.sha256 != expected_live_sha256:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_snapshot_changed"
            )
        self._fence("recovery_adoption_manifest", call_fence)
        reject_filesystem_alias_ancestry(candidate_root)
        manifest_path = candidate_root / GENERATION_MANIFEST_FILENAME
        if is_filesystem_alias(manifest_path):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_generation_manifest_unsafe"
            )
        manifest_sha, _manifest_fsync = write_generation_manifest(
            legacy,
            generation_id,
            manifest_payload,
        )
        pointer = active_pointer_path(legacy)
        previous_pointer = self._optional_plain_bytes(pointer)
        self._fence("recovery_adoption_close_live", call_fence)
        try:
            self._close_callback()
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_close_failed"
            ) from exc
        current = snapshot_global_artifact(
            legacy,
            fence_check=lambda: self._fence(
                "recovery_adoption_pre_cutover", call_fence
            ),
        )
        if current.sha256 != expected_live_sha256:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_snapshot_changed"
            )
        self._fence("recovery_adoption_cutover", call_fence)
        reject_filesystem_alias_ancestry(pointer.parent)
        if is_filesystem_alias(pointer):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_active_pointer_unsafe"
            )
        switch_active_generation(
            legacy,
            generation_id=generation_id,
            manifest_sha256=manifest_sha,
        )
        try:
            active = read_safe_active_generation(legacy)
            if active is None or active.generation_id != generation_id:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_cutover_readback_mismatch"
                )
            self._fence("recovery_adoption_readback", call_fence)
            self._assert_database_path_safe(active.graph_path)
            try:
                readback = self._candidate_database_factory(active.graph_path)
            except Exception as exc:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_readback_open_failed"
                ) from exc
            try:
                require_global_grafx_admission(readback, self._admission)
                validate_current_grafx_global_schema(readback)
                certify_grafx_global_vector_indexes(readback)
                runtime = self._runtime_for_database(
                    readback,
                    active.graph_path,
                    call_fence,
                )
                readback_schema_count, readback_projection = (
                    self._validate_complete_projection(runtime)
                )
                if (
                    readback_schema_count != schema_count
                    or readback_projection != projection
                ):
                    raise CommunityGrafxGlobalDiscoveryRecoveryError(
                        "global_discovery_adoption_semantic_mismatch"
                    )
            finally:
                self._close_database(
                    readback,
                    phase="recovery_adoption_readback_close",
                    call_fence=call_fence,
                    error_code="global_discovery_adoption_readback_close_failed",
                )
        except Exception:
            self._fence("recovery_adoption_rollback", call_fence)
            if previous_pointer is None:
                reject_filesystem_alias_ancestry(pointer.parent)
                restore_legacy_generation(legacy)
            else:
                try:
                    document = json.loads(previous_pointer.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, TypeError) as exc:
                    raise CommunityGrafxGlobalDiscoveryRecoveryError(
                        "global_discovery_previous_pointer_unrestorable"
                    ) from exc
                reject_filesystem_alias_ancestry(pointer.parent)
                write_json_atomic(pointer, document)
            raise

        return GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256=candidate_snapshot.sha256,
            quarantine_ref=None,
            schema_object_count=schema_count,
            rollback_performed=False,
            directory_fsync_supported=False,
            cutover_atomicity="atomic_pointer_replace",
            recovery_journal_ref=f"generation-manifest:{generation_id}",
        )

    def _runtime_for_database(
        self,
        database: Database,
        path: Path,
        call_fence: Callable[[], None],
    ) -> CommunityGrafxGlobalDiscoveryRuntime:
        return CommunityGrafxGlobalDiscoveryRuntime(
            lambda: database,
            lambda: path,
            database.close,
            lambda phase: self._fence(phase, call_fence),
            admission=self._admission,
        )

    def _materialize(
        self,
        runtime: CommunityGrafxGlobalDiscoveryRuntime,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        call_fence: Callable[[], None],
    ) -> None:
        for board in boards:
            self._fence("recovery_materialize", call_fence)
            runtime.upsert_board_summary(
                board_id=board.board_id,
                name=board.board_name or board.board_id,
                summary=board.summary,
                summary_embedding=_vector(board.summary_embedding),
                decision_count=len(board.digests),
                synced_at=_FIXED_TIMESTAMP,
            )
            for digest in sorted(
                board.digests,
                key=lambda item: item.original_node_id,
            ):
                self._fence("recovery_materialize", call_fence)
                physical_id = _digest_id(board.board_id, digest.original_node_id)
                runtime.upsert_decision_digest(
                    digest_id=physical_id,
                    board_id=board.board_id,
                    original_node_id=digest.original_node_id,
                    title=digest.title,
                    summary=digest.summary,
                    node_type=digest.node_type,
                    graph_layer=digest.graph_layer,
                    embedding=_vector(digest.embedding),
                    created_at=_FIXED_TIMESTAMP,
                )
                runtime.link_board_digest(
                    board_id=board.board_id,
                    digest_id=physical_id,
                )

    @staticmethod
    def _manifest_document(path: Path) -> dict[str, object]:
        try:
            reject_filesystem_alias_ancestry(path.parent)
            metadata = path.lstat()
            if is_filesystem_alias(path) or not stat.S_ISREG(metadata.st_mode):
                raise OSError("linked_generation_manifest")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_generation_manifest_unreadable"
            ) from exc
        if not isinstance(raw, dict):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_generation_manifest_unreadable"
            )
        return raw

    @staticmethod
    def _optional_plain_bytes(path: Path) -> bytes | None:
        reject_filesystem_alias_ancestry(path.parent)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if is_filesystem_alias(path) or not stat.S_ISREG(metadata.st_mode):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_active_pointer_unsafe"
            )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_active_pointer_unreadable"
            ) from exc

    def _completed_retry(
        self,
        *,
        legacy: Path,
        generation_id: str,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        source_fingerprint: str,
        expected: dict[str, object],
        call_fence: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult | None:
        active = read_safe_active_generation(legacy)
        if active is None or active.generation_id != generation_id:
            return None
        document = self._manifest_document(
            active.graph_path.parent / GENERATION_MANIFEST_FILENAME
        )
        required = {
            "kind": "grafx_global_discovery_recovery",
            "run_id": run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "expected_live_sha256": expected_live_sha256,
            "source_fingerprint": source_fingerprint,
        }
        if any(document.get(key) != value for key, value in required.items()):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_completed_generation_binding_mismatch"
            )
        self._fence("recovery_readback", call_fence)
        self._assert_database_path_safe(active.graph_path)
        try:
            database = self._candidate_database_factory(active.graph_path)
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_readback_open_failed"
            ) from exc
        try:
            require_global_grafx_admission(database, self._admission)
            validate_current_grafx_global_schema(database)
            certify_grafx_global_vector_indexes(database)
            runtime = self._runtime_for_database(
                database, active.graph_path, call_fence
            )
            schema_count = self._validate_runtime(runtime, expected)
        finally:
            self._close_database(
                database,
                phase="recovery_retry_close",
                call_fence=call_fence,
                error_code="global_discovery_readback_close_failed",
            )
        candidate_sha = str(document.get("candidate_sha256") or "")
        if _HEX_SHA256.fullmatch(candidate_sha) is None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_sha_invalid"
            )
        durable_snapshot = _snapshot_certified_generation(
            active.graph_path,
            fence_check=lambda: self._fence("recovery_retry_snapshot", call_fence),
        )
        if not durable_snapshot.exists or durable_snapshot.sha256 != candidate_sha:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_sha_invalid"
            )
        return GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256=candidate_sha,
            quarantine_ref=None,
            schema_object_count=schema_count,
            directory_fsync_supported=bool(
                document.get("directory_fsync_supported", False)
            ),
            cutover_atomicity="atomic_pointer_replace",
            recovery_journal_ref=f"generation-manifest:{generation_id}",
        )

    def rebuild_candidate_and_cutover(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        fence_check: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult:
        if type(run_id) is not str or not run_id:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_run_id_invalid"
            )
        if type(attempt_id) is not str or not attempt_id:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_id_invalid"
            )
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_epoch_invalid"
            )
        if _HEX_SHA256.fullmatch(expected_live_sha256) is None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_expected_live_sha_invalid"
            )
        ordered = _ordered_boards(boards)
        expected = _expected_projection(ordered)
        source_fingerprint = canonical_sha256([row.to_dict() for row in ordered])
        generation_id = _generation_id(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
        )
        self._fence("recovery_start", fence_check)
        legacy = Path(self._path_resolver())
        retry = self._completed_retry(
            legacy=legacy,
            generation_id=generation_id,
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            source_fingerprint=source_fingerprint,
            expected=expected,
            call_fence=fence_check,
        )
        if retry is not None:
            return retry

        observed = snapshot_global_artifact(
            legacy,
            fence_check=lambda: self._fence("recovery_snapshot", fence_check),
        )
        if observed.sha256 != expected_live_sha256:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_snapshot_changed"
            )
        candidate_root = safe_global_generation_dir(legacy, generation_id)
        candidate_path = safe_global_generation_graph_path(legacy, generation_id)
        reject_filesystem_alias_ancestry(candidate_root.parent)
        try:
            candidate_root.lstat()
        except FileNotFoundError:
            pass
        else:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_generation_already_exists"
            )

        self._fence("recovery_candidate", fence_check)
        reject_filesystem_alias_ancestry(candidate_root.parent)
        candidate_root.mkdir(parents=True, exist_ok=False)
        self._assert_database_path_safe(candidate_path)
        try:
            candidate = self._candidate_database_factory(candidate_path)
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_open_failed"
            ) from exc
        try:
            require_global_grafx_admission(candidate, self._admission)
            ensure_current_grafx_global_schema(
                candidate,
                revalidate_fence=lambda phase: self._fence(phase, fence_check),
            )
            runtime = self._runtime_for_database(candidate, candidate_path, fence_check)
            self._materialize(runtime, ordered, fence_check)
            self._fence("flush", fence_check)
            try:
                candidate.flush()
            except Exception as exc:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_candidate_flush_failed"
                ) from exc
            self._fence("checkpoint", fence_check)
            try:
                candidate.checkpoint()
            except Exception as exc:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_candidate_checkpoint_failed"
                ) from exc
            validate_current_grafx_global_schema(candidate)
            schema_count = self._validate_runtime(runtime, expected)
        finally:
            self._close_database(
                candidate,
                phase="recovery_candidate_close",
                call_fence=fence_check,
                error_code="global_discovery_candidate_close_failed",
            )

        self._fence("recovery_candidate_certified", fence_check)
        candidate_snapshot = _snapshot_certified_generation(
            candidate_path,
            fence_check=lambda: self._fence("recovery_candidate_snapshot", fence_check),
        )
        if not candidate_snapshot.exists:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_absent_after_close"
            )
        manifest_payload = {
            "kind": "grafx_global_discovery_recovery",
            "run_id": run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "expected_live_sha256": expected_live_sha256,
            "candidate_sha256": candidate_snapshot.sha256,
            "source_fingerprint": source_fingerprint,
            "semantic_fingerprint": canonical_sha256(expected),
            "schema_fingerprint": PULSE_GRAFX_GLOBAL_SCHEMA.logical_fingerprint,
            "schema_object_count": schema_count,
            "board_count": len(ordered),
            "digest_count": sum(len(board.digests) for board in ordered),
            # The pointer and manifest are individually fsynced when supported,
            # but a portable retry cannot re-probe the historical platform
            # result.  Publish the conservative, stable value in the manifest.
            "directory_fsync_supported": False,
        }
        self._fence("recovery_manifest", fence_check)
        reject_filesystem_alias_ancestry(candidate_root)
        manifest_path = candidate_root / GENERATION_MANIFEST_FILENAME
        if is_filesystem_alias(manifest_path):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_generation_manifest_unsafe"
            )
        manifest_sha, _manifest_fsync = write_generation_manifest(
            legacy,
            generation_id,
            manifest_payload,
        )
        current = snapshot_global_artifact(
            legacy,
            fence_check=lambda: self._fence("recovery_pre_cutover", fence_check),
        )
        if current.sha256 != expected_live_sha256:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_snapshot_changed"
            )

        pointer = active_pointer_path(legacy)
        previous_pointer = self._optional_plain_bytes(pointer)
        self._fence("recovery_close_live", fence_check)
        try:
            self._close_callback()
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_close_failed"
            ) from exc
        self._fence("recovery_cutover", fence_check)
        reject_filesystem_alias_ancestry(pointer.parent)
        if is_filesystem_alias(pointer):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_active_pointer_unsafe"
            )
        _pointer_fsync = switch_active_generation(
            legacy,
            generation_id=generation_id,
            manifest_sha256=manifest_sha,
        )
        switched = True
        try:
            active = read_safe_active_generation(legacy)
            if active is None or active.generation_id != generation_id:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_cutover_readback_mismatch"
                )
            self._fence("recovery_readback", fence_check)
            self._assert_database_path_safe(active.graph_path)
            try:
                readback = self._candidate_database_factory(active.graph_path)
            except Exception as exc:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_readback_open_failed"
                ) from exc
            try:
                require_global_grafx_admission(readback, self._admission)
                validate_current_grafx_global_schema(readback)
                certify_grafx_global_vector_indexes(readback)
                readback_runtime = self._runtime_for_database(
                    readback,
                    active.graph_path,
                    fence_check,
                )
                self._validate_runtime(readback_runtime, expected)
            finally:
                self._close_database(
                    readback,
                    phase="recovery_readback_close",
                    call_fence=fence_check,
                    error_code="global_discovery_readback_close_failed",
                )
        except Exception:
            if switched:
                self._fence("recovery_rollback", fence_check)
                if previous_pointer is None:
                    reject_filesystem_alias_ancestry(pointer.parent)
                    restore_legacy_generation(legacy)
                else:
                    try:
                        document = json.loads(previous_pointer.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, TypeError) as exc:
                        raise CommunityGrafxGlobalDiscoveryRecoveryError(
                            "global_discovery_previous_pointer_unrestorable"
                        ) from exc
                    reject_filesystem_alias_ancestry(pointer.parent)
                    write_json_atomic(pointer, document)
            raise

        return GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256=candidate_snapshot.sha256,
            quarantine_ref=None,
            schema_object_count=schema_count,
            rollback_performed=False,
            directory_fsync_supported=False,
            cutover_atomicity="atomic_pointer_replace",
            recovery_journal_ref=f"generation-manifest:{generation_id}",
        )

    def recover_and_cutover(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        fence_check: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult:
        """Adopt a complete live primary, otherwise rebuild from authoritative seeds."""

        if type(run_id) is not str or not run_id:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_run_id_invalid"
            )
        if type(attempt_id) is not str or not attempt_id:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_attempt_id_invalid"
            )
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_recovery_epoch_invalid"
            )
        if _HEX_SHA256.fullmatch(expected_live_sha256) is None:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_expected_live_sha_invalid"
            )
        ordered = _ordered_boards(boards)
        expected = _expected_projection(ordered)
        source_fingerprint = canonical_sha256([row.to_dict() for row in ordered])
        generation_id = _generation_id(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
        )
        adoption_generation_id = _adoption_generation_id(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
        )
        self._fence("recovery_start", fence_check)
        legacy = Path(self._path_resolver())
        adoption_retry = self._completed_adoption_retry(
            legacy=legacy,
            generation_id=adoption_generation_id,
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            call_fence=fence_check,
        )
        if adoption_retry is not None:
            result = adoption_retry
        else:
            retry = self._completed_retry(
                legacy=legacy,
                generation_id=generation_id,
                run_id=run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=expected_live_sha256,
                source_fingerprint=source_fingerprint,
                expected=expected,
                call_fence=fence_check,
            )
            if retry is not None:
                result = retry
            else:
                adopted = self._try_adopt_complete_live(
                    legacy=legacy,
                    generation_id=adoption_generation_id,
                    run_id=run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                    expected_live_sha256=expected_live_sha256,
                    call_fence=fence_check,
                )
                if adopted is not None:
                    result = adopted
                else:
                    result = self.rebuild_candidate_and_cutover(
                        run_id=run_id,
                        epoch=epoch,
                        attempt_id=attempt_id,
                        expected_live_sha256=expected_live_sha256,
                        boards=boards,
                        fence_check=fence_check,
                    )
        return self._record_completed_attempt(
            result,
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            boards=boards,
            call_fence=fence_check,
        )


__all__ = [
    "CandidateDatabaseFactory",
    "CommunityGrafxGlobalDiscoveryFenceError",
    "CommunityGrafxGlobalDiscoveryRecovery",
    "CommunityGrafxGlobalDiscoveryRecoveryError",
    "CommunityGrafxRecoveryAttemptReconciliation",
    "SnapshotFingerprintProvider",
]
