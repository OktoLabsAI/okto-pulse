"""Grafx implementation of the Core ``GlobalDiscoveryRuntime`` port."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)
from okto_pulse.core.kg.interfaces.graph_lifecycle import GraphHandle
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeObservationState,
    GraphRuntimeState,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.core.kg.quarantine import KGQuarantineService
from okto_pulse.core.kg.tier_power import validate_cypher_read_only

from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory,
    remove_contained_tree,
    reject_filesystem_alias_ancestry,
    validate_scope_id,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    GlobalDiscoveryLayoutError,
)
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_global_discovery import (
    certify_grafx_global_vector_indexes,
    ensure_current_grafx_global_schema,
    search_grafx_decision_digests,
    upsert_grafx_board_summary_vector,
    upsert_grafx_decision_digest_vector,
    validate_current_grafx_global_schema,
)
from okto_pulse.community.adapters.grafx_global_operational import (
    GLOBAL_SCOPE,
    GlobalAdmission,
    GlobalCloseCallback,
    GlobalDatabaseResolver,
    GlobalFenceRevalidator,
    GlobalPathResolver,
    core_error_code,
    global_discovery_storage_ref,
    global_layout_targets,
    has_grafx_identity,
    normalize_grafx_value,
    read_safe_active_generation,
    require_global_grafx_admission,
    resolved_global_graph_path,
    validate_plain_global_artifact,
)
from okto_pulse.community.adapters.local_storage_ref import local_storage_ref

PrivacyArtifactResolver = Callable[[str], tuple[Path, ...]]

_BACKEND = "okto_grafx"
_DERIVED_RELATIONSHIPS = (
    ("DECISION_MENTIONS_ENTITY", "outgoing"),
    ("DECISION_DERIVES_FROM", "outgoing"),
    ("DECISION_DERIVES_FROM", "incoming"),
)


def _capability(reason: str, *, operation: str) -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "The Grafx Global Discovery operation cannot be completed safely.",
        details={
            "backend": _BACKEND,
            "operation": operation,
            "reason": reason,
        },
    )


def _statement_is_write(statement: str) -> bool:
    if type(statement) is not str or not statement.strip():
        raise ValueError("statement must be non-empty text")
    try:
        validate_cypher_read_only(statement)
    except Exception:
        return True
    return False


def _affected_count(statistics: object) -> int | None:
    if not isinstance(statistics, dict) or not statistics:
        return None
    values: list[int] = []
    for value in statistics.values():
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        values.append(max(0, value))
    return sum(values) if values else None


def _count_row(rows: tuple[tuple[Any, ...], ...]) -> int:
    if len(rows) != 1 or len(rows[0]) != 1:
        raise _capability("count_result_shape_invalid", operation="global_statement")
    return int(rows[0][0] or 0)


class CommunityGrafxGlobalDiscoveryRuntime:
    """Operate the separately resolved Grafx Global Discovery database."""

    def __init__(
        self,
        database_resolver: GlobalDatabaseResolver,
        path_resolver: GlobalPathResolver,
        close_callback: GlobalCloseCallback,
        revalidate_fence: GlobalFenceRevalidator,
        *,
        admission: GlobalAdmission | None = None,
        privacy_artifact_resolver: PrivacyArtifactResolver | None = None,
    ) -> None:
        self._database_resolver = database_resolver
        self._path_resolver = path_resolver
        self._close_callback = close_callback
        self._revalidate_fence = revalidate_fence
        self._admission = admission
        self._privacy_artifact_resolver = privacy_artifact_resolver
        self._lock = RLock()

    def _database(self):
        database = self._database_resolver()
        require_global_grafx_admission(database, self._admission)
        return database

    def _fence(self, phase: str) -> None:
        self._revalidate_fence(phase)

    @staticmethod
    def _state_value(
        state: GraphRuntimeObservationState,
        *,
        generation: str | None,
        reason_code: str,
        observed_at: datetime,
        details: dict[str, object] | None = None,
    ) -> GraphRuntimeState:
        return GraphRuntimeState.from_observation(
            board_id=GLOBAL_SCOPE,
            storage_ref=global_discovery_storage_ref(),
            state=state,
            generation=generation,
            reason_code=reason_code,
            observed_at=observed_at,
            backend=_BACKEND,
            details={"source": "community_grafx_global_runtime", **(details or {})},
        )

    def state(self, *, generation: str | None = None) -> GraphRuntimeState:
        """Classify the authenticated layout without opening Grafx."""

        observed_at = datetime.now(timezone.utc)
        try:
            legacy = Path(self._path_resolver())
        except Exception:
            return self._state_value(
                GraphRuntimeObservationState.PROVIDER_UNAVAILABLE,
                generation=generation,
                reason_code="global_discovery_provider_unavailable",
                observed_at=observed_at,
            )
        try:
            active = read_safe_active_generation(legacy)
            if active is not None:
                if not has_grafx_identity(active.graph_path):
                    raise GlobalDiscoveryLayoutError(
                        "active_generation_identity_missing"
                    )
                return self._state_value(
                    GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE,
                    generation=generation or active.generation_id,
                    reason_code="global_discovery_active_generation_present",
                    observed_at=observed_at,
                    details={
                        "active_generation": active.generation_id,
                        "manifest_sha256": active.manifest_sha256,
                    },
                )
            if has_grafx_identity(legacy):
                return self._state_value(
                    GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE,
                    generation=generation,
                    reason_code="global_discovery_legacy_primary_present",
                    observed_at=observed_at,
                )
            residues = global_layout_targets(legacy)
            if residues:
                return self._state_value(
                    GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                    generation=generation,
                    reason_code="global_discovery_residue_without_primary",
                    observed_at=observed_at,
                    details={"artifact_count": len(residues)},
                )
            return self._state_value(
                GraphRuntimeObservationState.CONFIRMED_ABSENT,
                generation=generation,
                reason_code="global_discovery_confirmed_absent",
                observed_at=observed_at,
            )
        except (GlobalDiscoveryLayoutError, OSError, ValueError) as exc:
            return self._state_value(
                GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code=getattr(
                    exc,
                    "code",
                    "global_discovery_layout_unreadable",
                ),
                observed_at=observed_at,
            )

    def bootstrap(self) -> GraphHandle:
        with self._lock:
            try:
                self._fence("global_bootstrap")
                database = self._database()
                ensure_current_grafx_global_schema(
                    database,
                    revalidate_fence=self._fence,
                )
                certify_grafx_global_vector_indexes(database)
                return GraphHandle(
                    board_id=GLOBAL_SCOPE,
                    storage_ref=global_discovery_storage_ref(),
                    opened=not database.closed,
                    status="opened" if not database.closed else "absent",
                    locked=False,
                    quarantined=False,
                )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="global_bootstrap")
                raise mapped from exc

    def ensure_layer_schema(self) -> tuple[str, ...]:
        with self._lock:
            try:
                self._fence("ensure_layer_schema")
                database = self._database()
                result = ensure_current_grafx_global_schema(
                    database,
                    revalidate_fence=self._fence,
                )
                validate_current_grafx_global_schema(database)
                return ("DecisionDigest.graph_layer",) if result.changed else ()
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="ensure_layer_schema")
                raise mapped from exc

    def _execute_on_database(
        self,
        database,
        statement: str,
        params: dict[str, Any] | None,
        *,
        operation: str,
        write: bool,
    ) -> GraphStatementResult:
        transaction = database.begin("write" if write else "read")
        try:
            if write:
                self._fence(operation)
            native = transaction.execute(statement, params or {})
            result = GraphStatementResult.from_rows(
                (
                    tuple(normalize_grafx_value(value) for value in row)
                    for row in native.rows
                ),
                columns=native.columns,
                affected_count=_affected_count(dict(native.statistics)),
            )
            if write:
                self._fence("commit")
                report = transaction.commit()
                if not report.durable:
                    raise _capability(
                        "commit_not_published",
                        operation=operation,
                    )
            else:
                transaction.rollback()
            return result
        except BaseException:
            if transaction.active:
                transaction.rollback()
            raise

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        with self._lock:
            write = _statement_is_write(statement)
            operation = "global_statement_write" if write else "global_statement_read"
            try:
                database = self._database()
                return self._execute_on_database(
                    database,
                    statement,
                    params,
                    operation=operation,
                    write=write,
                )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation=operation)
                raise mapped from exc

    def search_decision_digests(
        self,
        query_vector: list[float],
        *,
        board_ids: tuple[str, ...],
        graph_layer: str,
        top_k: int,
        min_similarity: float,
        exhaustive: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            try:
                database = self._database()
                return search_grafx_decision_digests(
                    database,
                    query_vector,
                    board_ids=board_ids,
                    graph_layer=graph_layer,
                    top_k=top_k,
                    min_similarity=min_similarity,
                    exhaustive=exhaustive,
                )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="global_digest_search")
                raise mapped from exc

    def list_schema_objects(self) -> tuple[str, ...]:
        with self._lock:
            try:
                database = self._database()
                return tuple(
                    sorted(table.name for table in database.catalog.catalog.tables())
                )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="global_schema_objects")
                raise mapped from exc

    def upsert_board_summary(
        self,
        *,
        board_id: str,
        name: str,
        summary: str,
        summary_embedding: list[float],
        decision_count: int,
        synced_at: str,
    ) -> None:
        with self._lock:
            try:
                self._fence("upsert_board_summary")
                database = self._database()
                upsert_grafx_board_summary_vector(
                    database,
                    board_id=board_id,
                    name=name,
                    summary=summary,
                    summary_embedding=summary_embedding,
                    decision_count=decision_count,
                    synced_at=synced_at,
                    revalidate_fence=self._fence,
                )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="upsert_board_summary")
                raise mapped from exc

    def upsert_decision_digest(
        self,
        *,
        digest_id: str,
        board_id: str,
        original_node_id: str,
        title: str,
        summary: str,
        node_type: str,
        graph_layer: str,
        embedding: list[float],
        created_at: str,
    ) -> str:
        with self._lock:
            try:
                self._fence("upsert_decision_digest")
                database = self._database()
                return upsert_grafx_decision_digest_vector(
                    database,
                    digest_id=digest_id,
                    board_id=board_id,
                    original_node_id=original_node_id,
                    title=title,
                    summary=summary,
                    node_type=node_type,
                    graph_layer=graph_layer,
                    embedding=embedding,
                    created_at=created_at,
                    revalidate_fence=self._fence,
                )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="upsert_decision_digest")
                raise mapped from exc

    def _derived_relationship_count(
        self,
        database,
        *,
        board_id: str,
        original_node_ids: tuple[str, ...],
        include_malformed: bool,
    ) -> int:
        params = {
            "board_id": board_id,
            "original_node_ids": list(original_node_ids),
        }
        predicate = (
            "d.board_id = $board_id AND (d.original_node_id IN $original_node_ids"
        )
        if include_malformed:
            predicate += " OR d.original_node_id IS NULL OR d.original_node_id = ''"
        predicate += ")"
        total = 0
        for relationship, direction in _DERIVED_RELATIONSHIPS:
            if direction == "incoming":
                statement = (
                    f"MATCH (:DecisionDigest)-[r:{relationship}]->(d:DecisionDigest) "
                    f"WHERE {predicate} RETURN count(r)"
                )
            else:
                target = (
                    "Entity"
                    if relationship == "DECISION_MENTIONS_ENTITY"
                    else "DecisionDigest"
                )
                statement = (
                    f"MATCH (d:DecisionDigest)-[r:{relationship}]->(:{target}) "
                    f"WHERE {predicate} RETURN count(r)"
                )
            result = self._execute_on_database(
                database,
                statement,
                params,
                operation="digest_relationship_preflight",
                write=False,
            )
            total += _count_row(result.rows)
        return total

    def replace_decision_digest_identity(
        self,
        *,
        digest_id: str,
        board_id: str,
        original_node_id: str,
        title: str,
        summary: str,
        node_type: str,
        graph_layer: str,
        embedding: list[float],
        created_at: str,
    ) -> int:
        """Converge the healthy Grafx PK identity without discarding derived edges."""

        with self._lock:
            try:
                database = self._database()
                semantic = self._execute_on_database(
                    database,
                    "MATCH (d:DecisionDigest) WHERE d.board_id = $board_id "
                    "AND d.original_node_id = $original_node_id RETURN d.id",
                    {
                        "board_id": board_id,
                        "original_node_id": original_node_id,
                    },
                    operation="replace_digest_identity_preflight",
                    write=False,
                )
                canonical = self._execute_on_database(
                    database,
                    "MATCH (d:DecisionDigest {id: $digest_id}) "
                    "RETURN d.board_id, d.original_node_id",
                    {"digest_id": digest_id},
                    operation="replace_digest_identity_preflight",
                    write=False,
                )
                semantic_ids = tuple(str(row[0]) for row in semantic.rows)
                if len(semantic_ids) > 1:
                    raise _capability(
                        "duplicate_semantic_identity",
                        operation="replace_digest_identity",
                    )
                if not semantic_ids and not canonical.rows:
                    raise _capability(
                        "digest_identity_not_found",
                        operation="replace_digest_identity",
                    )
                if canonical.rows and (
                    str(canonical.rows[0][0]) != board_id
                    or str(canonical.rows[0][1]) != original_node_id
                ):
                    raise _capability(
                        "digest_primary_key_collision",
                        operation="replace_digest_identity",
                    )
                if semantic_ids and semantic_ids[0] != digest_id:
                    if self._derived_relationship_count(
                        database,
                        board_id=board_id,
                        original_node_ids=(original_node_id,),
                        include_malformed=False,
                    ):
                        raise _capability(
                            "digest_replace_relationships_present",
                            operation="replace_digest_identity",
                        )
                    removed = self.delete_decision_digests_for_absent_sources(
                        board_id=board_id,
                        original_node_ids=(original_node_id,),
                    )
                else:
                    removed = 1
                self.upsert_decision_digest(
                    digest_id=digest_id,
                    board_id=board_id,
                    original_node_id=original_node_id,
                    title=title,
                    summary=summary,
                    node_type=node_type,
                    graph_layer=graph_layer,
                    embedding=embedding,
                    created_at=created_at,
                )
                self.normalize_board_digest_link(
                    board_id=board_id,
                    digest_id=digest_id,
                )
                return removed
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="replace_digest_identity")
                raise mapped from exc

    @staticmethod
    def _target_predicate(include_malformed: bool) -> str:
        predicate = (
            "d.board_id = $board_id AND (d.original_node_id IN $original_node_ids"
        )
        if include_malformed:
            predicate += " OR d.original_node_id IS NULL OR d.original_node_id = ''"
        return predicate + ")"

    def _delete_digests(
        self,
        *,
        board_id: str,
        original_node_ids: tuple[str, ...],
        include_malformed: bool,
        guarded: bool,
    ) -> int:
        if not original_node_ids and not include_malformed:
            return 0
        database = self._database()
        if guarded and self._derived_relationship_count(
            database,
            board_id=board_id,
            original_node_ids=original_node_ids,
            include_malformed=include_malformed,
        ):
            raise _capability(
                "digest_prune_relationships_present",
                operation="delete_decision_digests_guarded",
            )
        params = {
            "board_id": board_id,
            "original_node_ids": list(original_node_ids),
        }
        predicate = self._target_predicate(include_malformed)
        before = self._execute_on_database(
            database,
            f"MATCH (d:DecisionDigest) WHERE {predicate} RETURN count(d)",
            params,
            operation="delete_digest_preflight",
            write=False,
        )
        count = _count_row(before.rows)
        if count:
            self._execute_on_database(
                database,
                f"MATCH (d:DecisionDigest) WHERE {predicate} DETACH DELETE d",
                params,
                operation="delete_decision_digests",
                write=True,
            )
        return count

    def delete_decision_digests_guarded(
        self,
        *,
        board_id: str,
        original_node_ids: tuple[str, ...],
        include_malformed: bool = False,
    ) -> int:
        with self._lock:
            try:
                return self._delete_digests(
                    board_id=board_id,
                    original_node_ids=original_node_ids,
                    include_malformed=include_malformed,
                    guarded=True,
                )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="delete_digests_guarded")
                raise mapped from exc

    def delete_decision_digests_for_absent_sources(
        self,
        *,
        board_id: str,
        original_node_ids: tuple[str, ...],
        include_malformed: bool = False,
    ) -> int:
        with self._lock:
            try:
                return self._delete_digests(
                    board_id=board_id,
                    original_node_ids=original_node_ids,
                    include_malformed=include_malformed,
                    guarded=False,
                )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(
                    exc,
                    operation="delete_digests_for_absent_sources",
                )
                raise mapped from exc

    def link_board_digest(self, *, board_id: str, digest_id: str) -> None:
        with self._lock:
            try:
                database = self._database()
                present = self._execute_on_database(
                    database,
                    "MATCH (b:Board {board_id: $board_id})-"
                    "[r:CONTAINS_DECISION]->(d:DecisionDigest {id: $digest_id}) "
                    "RETURN count(r)",
                    {"board_id": board_id, "digest_id": digest_id},
                    operation="link_board_digest_preflight",
                    write=False,
                )
                if _count_row(present.rows):
                    return
                created = self._execute_on_database(
                    database,
                    "MATCH (b:Board {board_id: $board_id}), "
                    "(d:DecisionDigest {id: $digest_id}) "
                    "WHERE d.board_id = $board_id "
                    "CREATE (b)-[:CONTAINS_DECISION]->(d) "
                    "RETURN b.board_id, d.id",
                    {"board_id": board_id, "digest_id": digest_id},
                    operation="link_board_digest",
                    write=True,
                )
                if len(created.rows) != 1:
                    raise _capability(
                        "board_or_digest_not_found",
                        operation="link_board_digest",
                    )
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="link_board_digest")
                raise mapped from exc

    def normalize_board_digest_link(
        self,
        *,
        board_id: str,
        digest_id: str,
    ) -> int:
        with self._lock:
            try:
                database = self._database()
                ownership = self._execute_on_database(
                    database,
                    "MATCH (b:Board {board_id: $board_id}), "
                    "(d:DecisionDigest {id: $digest_id}) "
                    "WHERE d.board_id = $board_id RETURN b.board_id, d.id",
                    {"board_id": board_id, "digest_id": digest_id},
                    operation="normalize_board_digest_link_preflight",
                    write=False,
                )
                if len(ownership.rows) != 1:
                    raise _capability(
                        "board_or_digest_not_found",
                        operation="normalize_board_digest_link",
                    )
                inbound = self._execute_on_database(
                    database,
                    "MATCH (b:Board)-[r:CONTAINS_DECISION]->"
                    "(d:DecisionDigest {id: $digest_id}) RETURN count(r)",
                    {"digest_id": digest_id},
                    operation="normalize_board_digest_link_preflight",
                    write=False,
                )
                removed = _count_row(inbound.rows)
                if removed:
                    self._execute_on_database(
                        database,
                        "MATCH (:Board)-[r:CONTAINS_DECISION]->"
                        "(d:DecisionDigest {id: $digest_id}) DELETE r",
                        {"digest_id": digest_id},
                        operation="normalize_board_digest_link",
                        write=True,
                    )
                self._execute_on_database(
                    database,
                    "MATCH (b:Board {board_id: $board_id}), "
                    "(d:DecisionDigest {id: $digest_id}) "
                    "CREATE (b)-[:CONTAINS_DECISION]->(d)",
                    {"board_id": board_id, "digest_id": digest_id},
                    operation="normalize_board_digest_link",
                    write=True,
                )
                return removed
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(
                    exc,
                    operation="normalize_board_digest_link",
                )
                raise mapped from exc

    def delete_invalid_board_digest_links(
        self,
        *,
        board_id: str,
        expected_digest_ids: tuple[str, ...],
    ) -> int:
        with self._lock:
            try:
                database = self._database()
                params = {
                    "board_id": board_id,
                    "expected_digest_ids": list(expected_digest_ids),
                }
                predicate = (
                    "b.board_id = $board_id AND ("
                    "coalesce(d.board_id, '') <> $board_id OR "
                    "NOT (d.id IN $expected_digest_ids))"
                )
                before = self._execute_on_database(
                    database,
                    "MATCH (b:Board)-[r:CONTAINS_DECISION]->"
                    f"(d:DecisionDigest) WHERE {predicate} RETURN count(r)",
                    params,
                    operation="delete_invalid_digest_links_preflight",
                    write=False,
                )
                count = _count_row(before.rows)
                if count:
                    self._execute_on_database(
                        database,
                        "MATCH (b:Board)-[r:CONTAINS_DECISION]->"
                        f"(d:DecisionDigest) WHERE {predicate} DELETE r",
                        params,
                        operation="delete_invalid_digest_links",
                        write=True,
                    )
                return count
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(
                    exc,
                    operation="delete_invalid_digest_links",
                )
                raise mapped from exc

    @contextmanager
    def post_write_verification_scope(self) -> Iterator[None]:
        with self._lock:
            self._fence("post_write_verification")
            yield

    def flush_after_write_batch(self) -> None:
        """Flush, checkpoint, close and prove a cold schema reopen."""

        with self._lock:
            try:
                legacy = Path(self._path_resolver())
                active_path = resolved_global_graph_path(legacy)
                self._fence("flush")
                database = self._database()
                self._fence("flush")
                database.flush()
                self._fence("checkpoint")
                database.checkpoint()
                self._fence("close_reopen")
                self._close_callback()
                if not has_grafx_identity(active_path):
                    raise _capability(
                        "primary_missing_after_flush",
                        operation="flush_after_write_batch",
                    )
                self._fence("reopen_probe")
                reopened = self._database()
                validate_current_grafx_global_schema(reopened)
                certify_grafx_global_vector_indexes(reopened)
                self._fence("close_reopen")
                self._close_callback()
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="flush_after_write_batch")
                raise mapped from exc

    def close(self) -> None:
        with self._lock:
            try:
                self._fence("close_global_discovery")
                self._close_callback()
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="close_global_discovery")
                raise mapped from exc

    @staticmethod
    def _quarantine(legacy: Path, targets: tuple[Path, ...], *, reason: str) -> int:
        service = KGQuarantineService(
            base_storage_ref_hint=local_storage_ref(legacy.parent.parent),
            scope_storage_refs=[local_storage_ref(legacy.parent)],
        )
        response = service.create(
            board_id=GLOBAL_SCOPE,
            graph_type="global_discovery",
            affected_storage_refs=[local_storage_ref(target) for target in targets],
            reason=reason,
            correlation_ids=[],
        )
        return response.files_moved

    def purge(self, *, reason: str = "manual") -> GraphPurgeResult:
        with self._lock:
            try:
                legacy = Path(self._path_resolver())
                targets = global_layout_targets(legacy)
                if not targets:
                    return GraphPurgeResult(
                        board_id=GLOBAL_SCOPE,
                        removed=False,
                        not_found=True,
                        status="not_found",
                        reason=reason,
                        backend=_BACKEND,
                    )
                self._fence("purge_global_discovery")
                self._close_callback()
                for target in targets:
                    validate_plain_global_artifact(target)
                self._fence("purge_global_discovery")
                moved = self._quarantine(legacy, targets, reason=reason)
                remaining = global_layout_targets(legacy)
                if moved <= 0 or remaining:
                    return GraphPurgeResult(
                        board_id=GLOBAL_SCOPE,
                        removed=False,
                        not_found=False,
                        status="failed",
                        reason=reason,
                        backend=_BACKEND,
                        error_code="purge_absence_unverified",
                    )
                return GraphPurgeResult(
                    board_id=GLOBAL_SCOPE,
                    removed=True,
                    not_found=False,
                    status="purged",
                    reason=reason,
                    backend=_BACKEND,
                )
            except Exception as exc:
                mapped = map_grafx_error(exc, operation="purge_global_discovery")
                return GraphPurgeResult(
                    board_id=GLOBAL_SCOPE,
                    removed=False,
                    not_found=False,
                    status="failed",
                    reason=reason,
                    backend=_BACKEND,
                    error_code=core_error_code(mapped),
                )

    def _capture_privacy_survivors(
        self,
        *,
        board_id: str,
        survivor_board_ids: tuple[str, ...] | None,
    ) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        board_rows = list(
            self.execute(
                "MATCH (b:Board) RETURN b.board_id, b.name, b.summary, "
                "b.summary_embedding, b.decision_count, b.last_sync_at "
                "ORDER BY b.board_id"
            ).rows
        )
        if survivor_board_ids is None:
            allowed = {str(row[0]) for row in board_rows if str(row[0]) != board_id}
        else:
            allowed = set(survivor_board_ids)
            allowed.discard(board_id)
        boards = [row for row in board_rows if str(row[0]) in allowed]
        if any(row[3] is None for row in boards):
            raise _capability(
                "privacy_survivor_embedding_missing",
                operation="erase_storage_for_privacy",
            )
        digests = list(
            self.execute(
                "MATCH (d:DecisionDigest) WHERE d.board_id IN $boards "
                "RETURN d.id, d.board_id, d.original_node_id, d.title, "
                "d.one_line_summary, d.node_type, d.graph_layer, d.embedding, "
                "d.created_at ORDER BY d.board_id, d.id",
                {"boards": sorted(allowed)},
            ).rows
        )
        if any(row[7] is None for row in digests):
            raise _capability(
                "privacy_survivor_embedding_missing",
                operation="erase_storage_for_privacy",
            )
        links = list(
            self.execute(
                "MATCH (b:Board)-[:CONTAINS_DECISION]->(d:DecisionDigest) "
                "WHERE b.board_id IN $boards RETURN b.board_id, d.id "
                "ORDER BY b.board_id, d.id",
                {"boards": sorted(allowed)},
            ).rows
        )
        if {str(row[0]) for row in boards} != allowed:
            raise _capability(
                "privacy_survivor_inventory_incomplete",
                operation="erase_storage_for_privacy",
            )
        return boards, digests, links

    def erase_storage_for_privacy(
        self,
        *,
        board_id: str,
        reason: str,
        survivor_board_ids: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        """Physically replace every composed snapshot with target-free survivors."""

        safe_board_id = validate_scope_id(board_id)
        with self._lock:
            try:
                legacy = Path(self._path_resolver())
                boards, digests, links = self._capture_privacy_survivors(
                    board_id=safe_board_id,
                    survivor_board_ids=survivor_board_ids,
                )
                targets = list(global_layout_targets(legacy))
                if self._privacy_artifact_resolver is not None:
                    targets.extend(self._privacy_artifact_resolver(safe_board_id))
                unique_targets: list[Path] = []
                for target in targets:
                    candidate = Path(target)
                    if candidate not in unique_targets:
                        unique_targets.append(candidate)
                self._fence("privacy_erase_global_discovery")
                self._close_callback()
                files_removed = 0
                directories_removed = 0
                for target in unique_targets:
                    self._fence("privacy_erase_global_discovery")
                    base = legacy.parent
                    if (
                        self._privacy_artifact_resolver is not None
                        and target.parent != base
                    ):
                        base = target.parent
                    files, directories = remove_contained_tree(
                        target,
                        base_dir=base,
                        before_mutation=lambda: self._fence(
                            "privacy_erase_global_discovery"
                        ),
                    )
                    files_removed += files
                    directories_removed += directories
                reject_filesystem_alias_ancestry(legacy.parent.parent)
                try:
                    legacy.parent.lstat()
                except FileNotFoundError:
                    pass
                else:
                    reject_filesystem_alias_ancestry(legacy.parent)
                    fsync_directory(legacy.parent)
                if global_layout_targets(legacy):
                    raise _capability(
                        "privacy_physical_erasure_unverified",
                        operation="erase_storage_for_privacy",
                    )
                self.bootstrap()
                for row in boards:
                    self.upsert_board_summary(
                        board_id=str(row[0]),
                        name=str(row[1] or row[0]),
                        summary=str(row[2] or ""),
                        summary_embedding=list(row[3]),
                        decision_count=int(row[4] or 0),
                        synced_at=str(row[5]),
                    )
                for row in digests:
                    self.upsert_decision_digest(
                        digest_id=str(row[0]),
                        board_id=str(row[1]),
                        original_node_id=str(row[2]),
                        title=str(row[3] or ""),
                        summary=str(row[4] or ""),
                        node_type=str(row[5] or ""),
                        graph_layer=str(row[6]),
                        embedding=list(row[7]),
                        created_at=str(row[8]),
                    )
                for survivor_board, digest_id in links:
                    self.link_board_digest(
                        board_id=str(survivor_board),
                        digest_id=str(digest_id),
                    )
                self.flush_after_write_batch()
                target_count = self.execute(
                    "MATCH (b:Board) WHERE b.board_id = $board_id RETURN count(b)",
                    {"board_id": safe_board_id},
                )
                if _count_row(target_count.rows):
                    raise _capability(
                        "privacy_target_survived_rebuild",
                        operation="erase_storage_for_privacy",
                    )
                return {
                    "board_id": safe_board_id,
                    "reason": reason,
                    "objects_removed": files_removed,
                    "directories_removed": directories_removed,
                    "verified_absent": True,
                    "survivors_restored": len(boards) + len(digests) + len(links),
                    "status": "purged",
                }
            except GraphError:
                raise
            except Exception as exc:
                mapped = map_grafx_error(
                    exc,
                    operation="erase_storage_for_privacy",
                )
                raise mapped from exc


__all__ = [
    "CommunityGrafxGlobalDiscoveryRuntime",
    "PrivacyArtifactResolver",
]
