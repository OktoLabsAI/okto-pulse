"""Fail-closed physical recovery for Grafx Global Discovery generations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from okto_grafx import Database
from okto_grafx.errors import GrafxError
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryCutoverResult,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphError

from okto_pulse.community.adapters.filesystem_erasure import (
    remove_contained_tree,
    validate_scope_id,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    GENERATION_MANIFEST_FILENAME,
    active_pointer_path,
    canonical_sha256,
    generation_dir,
    generation_graph_path,
    read_active_generation,
    restore_legacy_generation,
    switch_active_generation,
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
    normalize_grafx_value,
    require_global_grafx_admission,
    resolved_global_graph_path,
    snapshot_global_artifact,
)
from okto_pulse.community.adapters.grafx_schema_manifest import EMBEDDING_DIMENSION

CandidateDatabaseFactory = Callable[[Path], Database]
SnapshotFingerprintProvider = Callable[[], str]

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SCHEMA = frozenset({"Board", "DecisionDigest", "CONTAINS_DECISION"})
_FIXED_TIMESTAMP = "1970-01-01T00:00:00Z"


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
    binding = f"{run_id}\0{epoch}\0{attempt_id}".encode("utf-8")
    return f"gdr_{hashlib.sha256(binding).hexdigest()[:32]}"


def _adoption_generation_id(*, run_id: str, epoch: int, attempt_id: str) -> str:
    return _generation_id(
        run_id=run_id,
        epoch=epoch,
        attempt_id=f"{attempt_id}\0adoption",
    )


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker is not None and checker())


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
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or _is_junction(source):
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_adoption_linked_artifact"
            )
        if stat.S_ISDIR(metadata.st_mode):
            fence_check()
            destination.mkdir(exist_ok=False)
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
        before = source.stat()
        fence_check()
        with source.open("rb") as reader, destination.open("xb") as writer:
            while True:
                fence_check()
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = source.stat()
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
        parent_metadata = source_graph.parent.lstat()
        if stat.S_ISLNK(parent_metadata.st_mode) or _is_junction(source_graph.parent):
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
            remove_contained_tree(candidate_root, base_dir=candidate_root.parent)
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
        active = read_active_generation(legacy)
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
        candidate_root = generation_dir(legacy, generation_id)
        candidate_path = generation_graph_path(legacy, generation_id)
        try:
            candidate_root.lstat()
        except FileNotFoundError:
            pass
        else:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_generation_already_exists"
            )
        self._fence("recovery_adoption_candidate", call_fence)
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

        candidate_snapshot = snapshot_global_artifact(
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
        manifest_sha, _manifest_fsync = write_generation_manifest(
            legacy,
            generation_id,
            manifest_payload,
        )
        pointer = active_pointer_path(legacy)
        try:
            previous_pointer = pointer.read_bytes()
        except FileNotFoundError:
            previous_pointer = None
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
        switch_active_generation(
            legacy,
            generation_id=generation_id,
            manifest_sha256=manifest_sha,
        )
        try:
            active = read_active_generation(legacy)
            if active is None or active.generation_id != generation_id:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_cutover_readback_mismatch"
                )
            self._fence("recovery_adoption_readback", call_fence)
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
        except Exception as failure:
            self._fence("recovery_adoption_rollback", call_fence)
            if previous_pointer is None:
                restore_legacy_generation(legacy)
            else:
                try:
                    document = json.loads(previous_pointer.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, TypeError) as exc:
                    raise CommunityGrafxGlobalDiscoveryRecoveryError(
                        "global_discovery_previous_pointer_unrestorable"
                    ) from exc
                write_json_atomic(pointer, document)
            raise failure

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
        active = read_active_generation(legacy)
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
        candidate_root = generation_dir(legacy, generation_id)
        candidate_path = generation_graph_path(legacy, generation_id)
        try:
            candidate_root.lstat()
        except FileNotFoundError:
            pass
        else:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_candidate_generation_already_exists"
            )

        self._fence("recovery_candidate", fence_check)
        candidate_root.mkdir(parents=True, exist_ok=False)
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
        candidate_snapshot = snapshot_global_artifact(
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
        try:
            previous_pointer = pointer.read_bytes()
        except FileNotFoundError:
            previous_pointer = None
        self._fence("recovery_close_live", fence_check)
        try:
            self._close_callback()
        except Exception as exc:
            raise CommunityGrafxGlobalDiscoveryRecoveryError(
                "global_discovery_live_close_failed"
            ) from exc
        self._fence("recovery_cutover", fence_check)
        _pointer_fsync = switch_active_generation(
            legacy,
            generation_id=generation_id,
            manifest_sha256=manifest_sha,
        )
        switched = True
        try:
            active = read_active_generation(legacy)
            if active is None or active.generation_id != generation_id:
                raise CommunityGrafxGlobalDiscoveryRecoveryError(
                    "global_discovery_cutover_readback_mismatch"
                )
            self._fence("recovery_readback", fence_check)
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
        except Exception as failure:
            if switched:
                self._fence("recovery_rollback", fence_check)
                if previous_pointer is None:
                    restore_legacy_generation(legacy)
                else:
                    try:
                        document = json.loads(previous_pointer.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, TypeError) as exc:
                        raise CommunityGrafxGlobalDiscoveryRecoveryError(
                            "global_discovery_previous_pointer_unrestorable"
                        ) from exc
                    write_json_atomic(pointer, document)
            raise failure

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
            return adoption_retry
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
            return adopted
        return self.rebuild_candidate_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=expected_live_sha256,
            boards=boards,
            fence_check=fence_check,
        )


__all__ = [
    "CandidateDatabaseFactory",
    "CommunityGrafxGlobalDiscoveryFenceError",
    "CommunityGrafxGlobalDiscoveryRecovery",
    "CommunityGrafxGlobalDiscoveryRecoveryError",
    "SnapshotFingerprintProvider",
]
