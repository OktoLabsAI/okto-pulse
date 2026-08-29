"""Explicit Board facades over immutable Community graph routes.

Each call enters the board lifecycle window before resolving its persisted
route.  Ordinary graph operations require a live physical binding; diagnosis,
recovery, and erasure only inspect the authenticated binding so they remain
available when the physical database needs repair.  No method consults current
backend settings or falls back to the other provider.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from okto_pulse.core.kg.interfaces.cypher_executor import CypherExecutor
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
)
from okto_pulse.core.kg.interfaces.graph_recovery import WalRecoveryReport
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeBudgetSnapshot,
    GraphRuntimeObservationState,
    GraphRuntimeState,
    GraphRuntimeStore,
    GraphStorageFootprint,
)
from okto_pulse.core.kg.interfaces.graph_schema_manager import (
    GraphSchemaManager,
    SchemaValidationResult,
)
from okto_pulse.core.kg.interfaces.graph_store import (
    GraphCapabilities,
    QueryFilters,
    SemanticGraphStore,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef

from okto_pulse.community.adapters.graph_rollout_capture import (
    BoardRolloutMutationRecorder,
    invoke_captured_auto_commit,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)

_ROUTED_SOURCE = "community_graph_routed"
_BINDING_MISSING_REASON = "graph_route_binding_missing"
_ProviderT = TypeVar("_ProviderT")
_ResultT = TypeVar("_ResultT")


class BoardGraphOperationWindowFactory(Protocol):
    def __call__(self, board_id: str) -> AbstractContextManager[None]: ...


class BoardStorageMutationWindowFactory(Protocol):
    def __call__(
        self,
        board_id: str,
        *,
        phase: str,
    ) -> AbstractContextManager[None]: ...


class BoardGraphWriteFenceRevalidator(Protocol):
    def __call__(self, board_id: str, phase: str) -> None: ...


class _BoardMutationOperation(Protocol):
    """Mutate one backend while the routed facade owns the only guard."""

    def __call__(self, board_id: str, *, reason: str) -> GraphPurgeResult: ...


class _BoardRecoveryOperation(Protocol):
    """Recover one backend while the routed facade owns the mutation guard."""

    async def __call__(self, board_id: str) -> WalRecoveryReport: ...


class _PairedCypherExecutor(CypherExecutor, Protocol):
    def execute_read_only_pair(
        self,
        board_id: str,
        primary_cypher: str,
        comparison_cypher: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int = 1000,
    ) -> dict[str, dict[str, Any]]: ...


def _invalid_board_route(
    snapshot: CommunityGraphRouteSnapshot,
    *,
    board_id: str,
) -> GraphCorruption:
    return GraphCorruption(
        "The routed Community Board graph snapshot is inconsistent.",
        details={
            "operation": "route_board_graph_facade",
            "reason": "graph_route_snapshot_scope_invalid",
            "scope": snapshot.scope,
            "scope_id": snapshot.scope_id,
            "board_id": board_id,
            "backend": snapshot.backend,
            "generation": snapshot.generation,
        },
    )


def _select_board_provider(
    snapshot: CommunityGraphRouteSnapshot,
    *,
    board_id: str,
    ladybug: _ProviderT,
    grafx: _ProviderT,
) -> _ProviderT:
    if snapshot.scope != "board" or snapshot.scope_id != board_id:
        raise _invalid_board_route(snapshot, board_id=board_id)
    if snapshot.backend == "ladybug":
        return ladybug
    if snapshot.backend == "grafx":
        return grafx
    raise _invalid_board_route(snapshot, board_id=board_id)


def _is_missing_binding(failure: GraphCapabilityUnavailable) -> bool:
    return failure.details.get("reason") == "binding_missing"


def _routed_storage_ref(board_id: str) -> StorageRef:
    return StorageRef(f"board:{board_id}", _ROUTED_SOURCE)


def _missing_runtime_state(
    board_id: str,
    *,
    generation: str | None,
) -> GraphRuntimeState:
    return GraphRuntimeState.from_observation(
        board_id=board_id,
        storage_ref=_routed_storage_ref(board_id),
        state=GraphRuntimeObservationState.PROVIDER_UNAVAILABLE,
        generation=generation,
        reason_code=_BINDING_MISSING_REASON,
        observed_at=datetime.now(UTC),
        details={"source": _ROUTED_SOURCE},
    )


def _missing_footprint(board_id: str) -> GraphStorageFootprint:
    return GraphStorageFootprint(
        board_id=board_id,
        storage_ref=_routed_storage_ref(board_id),
        status="unavailable",
        source=_ROUTED_SOURCE,
        unavailable_reason=_BINDING_MISSING_REASON,
    )


def _missing_purge_result(board_id: str, *, reason: str) -> GraphPurgeResult:
    return GraphPurgeResult(
        board_id=board_id,
        removed=False,
        not_found=False,
        status="failed",
        reason=reason,
        backend=None,
        error_code=_BINDING_MISSING_REASON,
    )


def _erase_succeeded(result: GraphPurgeResult) -> bool:
    return result.error_code is None and result.status in {"erased", "not_found"}


def _failed_erase_result(
    board_id: str,
    *,
    reason: str,
    failure: Exception,
) -> GraphPurgeResult:
    return GraphPurgeResult(
        board_id=board_id,
        removed=False,
        not_found=False,
        status="failed",
        reason=reason,
        backend=None,
        error_code=type(failure).__name__,
    )


def _aggregate_privacy_erase(
    board_id: str,
    *,
    reason: str,
    results: tuple[GraphPurgeResult, GraphPurgeResult],
) -> GraphPurgeResult:
    removed = any(result.removed for result in results)
    if not all(_erase_succeeded(result) for result in results):
        return GraphPurgeResult(
            board_id=board_id,
            removed=removed,
            not_found=False,
            status="failed",
            reason=reason,
            backend=None,
            error_code="privacy_erase_incomplete",
        )
    not_found = all(result.not_found for result in results)
    return GraphPurgeResult(
        board_id=board_id,
        removed=removed,
        not_found=not_found,
        status="not_found" if not_found else "erased",
        reason=reason,
        backend=None,
        error_code=None,
    )


class CommunityRoutedSemanticGraphStore:
    """Route every ``SemanticGraphStore`` method by immutable Board binding."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        ladybug: SemanticGraphStore,
        grafx: SemanticGraphStore,
        operation_window: BoardGraphOperationWindowFactory,
        revalidate_write_fence: BoardGraphWriteFenceRevalidator | None = None,
        mutation_recorder: BoardRolloutMutationRecorder | None = None,
    ) -> None:
        self._resolver = resolver
        self._ladybug = ladybug
        self._grafx = grafx
        self._operation_window = operation_window
        self._revalidate_write_fence = revalidate_write_fence or (
            lambda _board_id, _phase: None
        )
        self._mutation_recorder = mutation_recorder

    def _provider(self, board_id: str) -> SemanticGraphStore:
        snapshot = self._resolver.acquire_board_route(board_id)
        return _select_board_provider(
            snapshot,
            board_id=board_id,
            ladybug=self._ladybug,
            grafx=self._grafx,
        )

    def _mutation_provider(
        self,
        board_id: str,
        *,
        phase: str,
    ) -> tuple[CommunityGraphRouteSnapshot, SemanticGraphStore]:
        snapshot = self._resolver.acquire_board_route(board_id)
        provider = _select_board_provider(
            snapshot,
            board_id=board_id,
            ladybug=self._ladybug,
            grafx=self._grafx,
        )
        self._revalidate_write_fence(board_id, phase)
        return snapshot, provider

    def _invoke_mutation(
        self,
        board_id: str,
        *,
        phase: str,
        family: str,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
        operation: Callable[[SemanticGraphStore], _ResultT],
    ) -> _ResultT:
        with self._operation_window(board_id):
            snapshot, provider = self._mutation_provider(board_id, phase=phase)
            recorder = self._mutation_recorder
            if recorder is None:
                return operation(provider)
            return invoke_captured_auto_commit(
                lambda: operation(provider),
                recorder=recorder,
                board_id=board_id,
                backend=snapshot.backend,
                binding_sha256=snapshot.binding_sha256,
                family=family,
                args=args,
                kwargs=kwargs,
            )

    def find_by_topic(
        self,
        board_id: str,
        node_type: str,
        topic: str,
        filters: QueryFilters,
    ) -> list[list]:
        with self._operation_window(board_id):
            return self._provider(board_id).find_by_topic(
                board_id, node_type, topic, filters
            )

    def find_by_artifact(
        self,
        board_id: str,
        artifact_id: str,
        filters: QueryFilters,
        *,
        graph_layer: str = "all",
        include_code_traceability: bool = True,
    ) -> list[list]:
        with self._operation_window(board_id):
            return self._provider(board_id).find_by_artifact(
                board_id,
                artifact_id,
                filters,
                graph_layer=graph_layer,
                include_code_traceability=include_code_traceability,
            )

    def traverse_supersedence(
        self,
        board_id: str,
        decision_id: str,
        max_depth: int = 10,
        node_type: str = "Decision",
    ) -> list[list]:
        with self._operation_window(board_id):
            return self._provider(board_id).traverse_supersedence(
                board_id, decision_id, max_depth, node_type
            )

    def find_contradictions(
        self,
        board_id: str,
        node_id: str | None,
        limit: int,
    ) -> list[list]:
        with self._operation_window(board_id):
            return self._provider(board_id).find_contradictions(
                board_id, node_id, limit
            )

    def vector_search(
        self,
        board_id: str,
        node_type: str,
        query_vec: list[float],
        top_k: int,
        min_similarity: float,
        *,
        include_superseded: bool = False,
        graph_layer: str = "all",
    ) -> list[dict]:
        with self._operation_window(board_id):
            return self._provider(board_id).vector_search(
                board_id,
                node_type,
                query_vec,
                top_k,
                min_similarity,
                include_superseded=include_superseded,
                graph_layer=graph_layer,
            )

    def find_active_by_source_ref(
        self,
        board_id: str,
        node_type: str,
        source_artifact_ref: str,
    ) -> dict[str, Any] | None:
        with self._operation_window(board_id):
            return self._provider(board_id).find_active_by_source_ref(
                board_id, node_type, source_artifact_ref
            )

    def get_constraint_detail(
        self,
        board_id: str,
        constraint_id: str,
    ) -> tuple[list[list], list[list], list[list]]:
        with self._operation_window(board_id):
            return self._provider(board_id).get_constraint_detail(
                board_id, constraint_id
            )

    def get_alternatives(
        self,
        board_id: str,
        decision_id: str,
        limit: int,
    ) -> list[list]:
        with self._operation_window(board_id):
            return self._provider(board_id).get_alternatives(
                board_id, decision_id, limit
            )

    def get_learnings_for_area(
        self,
        board_id: str,
        area: str,
        filters: QueryFilters,
    ) -> list[list]:
        with self._operation_window(board_id):
            return self._provider(board_id).get_learnings_for_area(
                board_id, area, filters
            )

    def get_schema_version(self, board_id: str) -> str | None:
        with self._operation_window(board_id):
            return self._provider(board_id).get_schema_version(board_id)

    def get_schema_info(
        self,
        board_id: str,
        *,
        include_internal: bool = False,
    ) -> dict:
        with self._operation_window(board_id):
            return self._provider(board_id).get_schema_info(
                board_id, include_internal=include_internal
            )

    def list_schema_objects(self, board_id: str) -> tuple[str, ...]:
        with self._operation_window(board_id):
            return self._provider(board_id).list_schema_objects(board_id)

    def list_node_properties(
        self,
        board_id: str,
        node_type: str,
    ) -> tuple[str, ...]:
        with self._operation_window(board_id):
            return self._provider(board_id).list_node_properties(board_id, node_type)

    def capabilities(self) -> GraphCapabilities:
        ladybug = self._ladybug.capabilities()
        grafx = self._grafx.capabilities()
        return GraphCapabilities(
            indexed_similarity=(
                ladybug.indexed_similarity and grafx.indexed_similarity
            ),
            schema_introspection=(
                ladybug.schema_introspection and grafx.schema_introspection
            ),
            mutable_indexed_attributes=(
                ladybug.mutable_indexed_attributes and grafx.mutable_indexed_attributes
            ),
        )

    def create_node(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        self._invoke_mutation(
            board_id,
            phase="graph_store_create_node",
            family="create_node",
            args=(node_type, node_id, attrs),
            kwargs={},
            operation=lambda provider: provider.create_node(
                board_id, node_type, node_id, attrs
            ),
        )

    def create_edge(
        self,
        board_id: str,
        edge_type: str,
        from_id: str,
        to_id: str,
        attrs: dict[str, Any] | None = None,
        *,
        from_type: str | None = None,
        to_type: str | None = None,
    ) -> None:
        self._invoke_mutation(
            board_id,
            phase="graph_store_create_edge",
            family="create_edge",
            args=(edge_type, from_id, to_id, attrs),
            kwargs={"from_type": from_type, "to_type": to_type},
            operation=lambda provider: provider.create_edge(
                board_id,
                edge_type,
                from_id,
                to_id,
                attrs,
                from_type=from_type,
                to_type=to_type,
            ),
        )

    def update_node(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        self._invoke_mutation(
            board_id,
            phase="graph_store_update_node",
            family="update_node",
            args=(node_type, node_id, attrs),
            kwargs={},
            operation=lambda provider: provider.update_node(
                board_id, node_type, node_id, attrs
            ),
        )

    def mark_superseded(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        *,
        superseded_by: str,
        superseded_at: str,
        revocation_reason: str,
    ) -> None:
        self._invoke_mutation(
            board_id,
            phase="graph_store_mark_superseded",
            family="mark_superseded",
            args=(node_type, node_id),
            kwargs={
                "superseded_by": superseded_by,
                "superseded_at": superseded_at,
                "revocation_reason": revocation_reason,
            },
            operation=lambda provider: provider.mark_superseded(
                board_id,
                node_type,
                node_id,
                superseded_by=superseded_by,
                superseded_at=superseded_at,
                revocation_reason=revocation_reason,
            ),
        )

    def edge_exists(
        self,
        board_id: str,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
        rule_id: str | None = None,
    ) -> bool:
        with self._operation_window(board_id):
            return self._provider(board_id).edge_exists(
                board_id,
                edge_type,
                from_type,
                to_type,
                from_id,
                to_id,
                rule_id,
            )

    def find_node_types(self, board_id: str, node_id: str) -> tuple[str, ...]:
        with self._operation_window(board_id):
            return self._provider(board_id).find_node_types(board_id, node_id)

    def increment_attestation(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        *,
        attested_at: str,
    ) -> None:
        self._invoke_mutation(
            board_id,
            phase="graph_store_increment_attestation",
            family="increment_attestation",
            args=(node_type, node_id),
            kwargs={"attested_at": attested_at},
            operation=lambda provider: provider.increment_attestation(
                board_id, node_type, node_id, attested_at=attested_at
            ),
        )

    def delete_nodes_by_session(self, board_id: str, session_id: str) -> int:
        return self._invoke_mutation(
            board_id,
            phase="graph_store_delete_nodes_by_session",
            family="delete_nodes_by_session",
            args=(session_id,),
            kwargs={},
            operation=lambda provider: provider.delete_nodes_by_session(
                board_id, session_id
            ),
        )

    def delete_edges_by_session(self, board_id: str, session_id: str) -> int:
        return self._invoke_mutation(
            board_id,
            phase="graph_store_delete_edges_by_session",
            family="delete_edges_by_session",
            args=(session_id,),
            kwargs={},
            operation=lambda provider: provider.delete_edges_by_session(
                board_id, session_id
            ),
        )

    def bootstrap(self, board_id: str) -> None:
        self._invoke_mutation(
            board_id,
            phase="graph_store_bootstrap",
            family="bootstrap",
            args=(),
            kwargs={},
            operation=lambda provider: provider.bootstrap(board_id),
        )


class CommunityRoutedCypherExecutor:
    """Route the read-only Cypher port without advertising backend variance."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        ladybug: _PairedCypherExecutor,
        grafx: _PairedCypherExecutor,
        operation_window: BoardGraphOperationWindowFactory,
    ) -> None:
        self._resolver = resolver
        self._ladybug = ladybug
        self._grafx = grafx
        self._operation_window = operation_window

    def _provider(self, board_id: str) -> _PairedCypherExecutor:
        return _select_board_provider(
            self._resolver.acquire_board_route(board_id),
            board_id=board_id,
            ladybug=self._ladybug,
            grafx=self._grafx,
        )

    def execute_read_only(
        self,
        board_id: str,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int = 1000,
    ) -> dict:
        with self._operation_window(board_id):
            return self._provider(board_id).execute_read_only(
                board_id, cypher, params, max_rows=max_rows
            )

    def execute_read_only_pair(
        self,
        board_id: str,
        primary_cypher: str,
        comparison_cypher: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int = 1000,
    ) -> dict[str, dict[str, Any]]:
        with self._operation_window(board_id):
            return self._provider(board_id).execute_read_only_pair(
                board_id,
                primary_cypher,
                comparison_cypher,
                params,
                max_rows=max_rows,
            )

    def is_supported(self) -> bool:
        ladybug = bool(self._ladybug.is_supported())
        grafx = bool(self._grafx.is_supported())
        return ladybug and grafx


class CommunityRoutedGraphSchemaManager:
    """Route each complete asynchronous schema call inside one Board window."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        ladybug: GraphSchemaManager,
        grafx: GraphSchemaManager,
        operation_window: BoardGraphOperationWindowFactory,
        revalidate_write_fence: BoardGraphWriteFenceRevalidator | None = None,
    ) -> None:
        self._resolver = resolver
        self._ladybug = ladybug
        self._grafx = grafx
        self._operation_window = operation_window
        self._revalidate_write_fence = revalidate_write_fence or (
            lambda _board_id, _phase: None
        )

    def _provider(self, board_id: str) -> GraphSchemaManager:
        return _select_board_provider(
            self._resolver.acquire_board_route(board_id),
            board_id=board_id,
            ladybug=self._ladybug,
            grafx=self._grafx,
        )

    def _mutation_provider(
        self,
        board_id: str,
        *,
        phase: str,
    ) -> GraphSchemaManager:
        provider = self._provider(board_id)
        self._revalidate_write_fence(board_id, phase)
        return provider

    async def ensure_bootstrapped(self, board_id: str) -> None:
        with self._operation_window(board_id):
            await self._mutation_provider(
                board_id,
                phase="graph_schema_ensure_bootstrapped",
            ).ensure_bootstrapped(board_id)

    async def migrate(self, board_id: str) -> dict[str, Any]:
        with self._operation_window(board_id):
            return await self._mutation_provider(
                board_id,
                phase="graph_schema_migrate",
            ).migrate(board_id)

    async def current_version(self, board_id: str) -> str:
        with self._operation_window(board_id):
            return await self._provider(board_id).current_version(board_id)

    async def validate(self, board_id: str) -> SchemaValidationResult:
        with self._operation_window(board_id):
            return await self._provider(board_id).validate(board_id)


class CommunityRoutedGraphRuntimeStore:
    """Route Board diagnosis and destructive runtime receipts fail-closed."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        ladybug: GraphRuntimeStore,
        grafx: GraphRuntimeStore,
        operation_window: BoardGraphOperationWindowFactory,
        mutation_window: BoardStorageMutationWindowFactory,
        ladybug_purge_unguarded: _BoardMutationOperation,
        grafx_purge_unguarded: _BoardMutationOperation,
        ladybug_erase_unguarded: _BoardMutationOperation,
        grafx_erase_unguarded: _BoardMutationOperation,
        rollout_erase_unguarded: _BoardMutationOperation | None = None,
        rollout_finalize_erase_unguarded: _BoardMutationOperation | None = None,
        rollout_write_fence: Callable[[str, str, CommunityGraphRouteSnapshot], None]
        | None = None,
    ) -> None:
        self._resolver = resolver
        self._ladybug = ladybug
        self._grafx = grafx
        self._operation_window = operation_window
        self._mutation_window = mutation_window
        self._ladybug_purge_unguarded = ladybug_purge_unguarded
        self._grafx_purge_unguarded = grafx_purge_unguarded
        self._ladybug_erase_unguarded = ladybug_erase_unguarded
        self._grafx_erase_unguarded = grafx_erase_unguarded
        self._rollout_erase_unguarded = rollout_erase_unguarded
        self._rollout_finalize_erase_unguarded = rollout_finalize_erase_unguarded
        self._rollout_write_fence = rollout_write_fence

    @staticmethod
    def _aggregate_privacy_results(
        board_id: str,
        *,
        reason: str,
        results: Sequence[GraphPurgeResult],
    ) -> GraphPurgeResult:
        """Fold rollout plus physical erasure receipts into the stable contract."""

        removed = any(result.removed for result in results)
        if not all(_erase_succeeded(result) for result in results):
            return GraphPurgeResult(
                board_id=board_id,
                removed=removed,
                not_found=False,
                status="failed",
                reason=reason,
                backend=None,
                error_code="privacy_erase_incomplete",
            )
        not_found = all(result.not_found for result in results)
        return GraphPurgeResult(
            board_id=board_id,
            removed=removed,
            not_found=not_found,
            status="not_found" if not_found else "erased",
            reason=reason,
            backend=None,
            error_code=None,
        )

    def _provider(self, board_id: str) -> GraphRuntimeStore:
        return _select_board_provider(
            self._resolver.inspect_board_route(board_id),
            board_id=board_id,
            ladybug=self._ladybug,
            grafx=self._grafx,
        )

    def graph_state(
        self,
        board_id: str,
        *,
        generation: str | None = None,
    ) -> GraphRuntimeState:
        with self._operation_window(board_id):
            try:
                provider = self._provider(board_id)
            except GraphCapabilityUnavailable as failure:
                if _is_missing_binding(failure):
                    return _missing_runtime_state(board_id, generation=generation)
                raise
            return provider.graph_state(board_id, generation=generation)

    def exists(self, board_id: str) -> bool:
        with self._operation_window(board_id):
            try:
                provider = self._provider(board_id)
            except GraphCapabilityUnavailable as failure:
                if _is_missing_binding(failure):
                    return False
                raise
            return provider.exists(board_id)

    def purge_board_graph(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        with self._mutation_window(board_id, phase="purge_board_graph"):
            try:
                snapshot = self._resolver.inspect_board_route(board_id)
            except GraphCapabilityUnavailable as failure:
                if _is_missing_binding(failure):
                    return _missing_purge_result(board_id, reason=reason)
                raise
            operation = _select_board_provider(
                snapshot,
                board_id=board_id,
                ladybug=self._ladybug_purge_unguarded,
                grafx=self._grafx_purge_unguarded,
            )
            write_fence = self._rollout_write_fence
            if write_fence is not None:
                write_fence(board_id, "purge_board_graph", snapshot)
            return operation(board_id, reason=reason)

    def erase_board_graph(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        with self._mutation_window(board_id, phase="erase_board_graph"):
            results: list[GraphPurgeResult] = []
            rollout_erase = self._rollout_erase_unguarded
            if rollout_erase is not None:
                try:
                    rollout_result = rollout_erase(board_id, reason=reason)
                except Exception as failure:  # noqa: BLE001 - fail closed before sweep
                    rollout_result = _failed_erase_result(
                        board_id,
                        reason=reason,
                        failure=failure,
                    )
                results.append(rollout_result)
                if not _erase_succeeded(rollout_result):
                    return self._aggregate_privacy_results(
                        board_id,
                        reason=reason,
                        results=results,
                    )

            try:
                snapshot = self._resolver.inspect_board_route(board_id)
            except GraphCapabilityUnavailable as failure:
                if not _is_missing_binding(failure):
                    raise
                operations = (
                    self._ladybug_erase_unguarded,
                    self._grafx_erase_unguarded,
                )
            else:
                routed = _select_board_provider(
                    snapshot,
                    board_id=board_id,
                    ladybug=self._ladybug_erase_unguarded,
                    grafx=self._grafx_erase_unguarded,
                )
                alternate = (
                    self._grafx_erase_unguarded
                    if snapshot.backend == "ladybug"
                    else self._ladybug_erase_unguarded
                )
                operations = (routed, alternate)

            # Privacy is an administrative all-storage sweep, not backend
            # fallback.  Both physical backends are attempted under this one
            # guard, including after a previous attempt removed the binding.
            for operation in operations:
                try:
                    results.append(operation(board_id, reason=reason))
                except Exception as failure:  # noqa: BLE001 - sweep peer after failure
                    results.append(
                        _failed_erase_result(
                            board_id,
                            reason=reason,
                            failure=failure,
                        )
                    )

            # The durable ``erased`` rollout tombstone remains present until
            # *both* physical stores have proved success.  This prevents a
            # partial privacy sweep from silently re-enabling graph writes.
            if not all(_erase_succeeded(result) for result in results):
                return self._aggregate_privacy_results(
                    board_id,
                    reason=reason,
                    results=results,
                )

            rollout_finalize = self._rollout_finalize_erase_unguarded
            if rollout_finalize is not None:
                try:
                    results.append(rollout_finalize(board_id, reason=reason))
                except Exception as failure:  # noqa: BLE001 - aggregate receipt
                    results.append(
                        _failed_erase_result(
                            board_id,
                            reason=reason,
                            failure=failure,
                        )
                    )
            return self._aggregate_privacy_results(
                board_id,
                reason=reason,
                results=results,
            )

    def footprint(self, board_id: str) -> GraphStorageFootprint:
        with self._operation_window(board_id):
            try:
                provider = self._provider(board_id)
            except GraphCapabilityUnavailable as failure:
                if _is_missing_binding(failure):
                    return _missing_footprint(board_id)
                raise
            return provider.footprint(board_id)

    def budget_snapshot(self) -> GraphRuntimeBudgetSnapshot:
        return GraphRuntimeBudgetSnapshot(
            source="runtime_capability",
            status="unavailable",
            unavailable_reason="routed_budget_incomplete",
        )


class CommunityRoutedGraphRecovery:
    """Route WAL-only recovery from an inspected binding under mutation guard."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        ladybug_recovery_unguarded: _BoardRecoveryOperation,
        grafx_recovery_unguarded: _BoardRecoveryOperation,
        mutation_window: BoardStorageMutationWindowFactory,
    ) -> None:
        self._resolver = resolver
        self._ladybug_recovery_unguarded = ladybug_recovery_unguarded
        self._grafx_recovery_unguarded = grafx_recovery_unguarded
        self._mutation_window = mutation_window

    def _operation(self, board_id: str) -> _BoardRecoveryOperation:
        return _select_board_provider(
            self._resolver.inspect_board_route(board_id),
            board_id=board_id,
            ladybug=self._ladybug_recovery_unguarded,
            grafx=self._grafx_recovery_unguarded,
        )

    async def recover_wal_only(self, board_id: str) -> WalRecoveryReport:
        with self._mutation_window(board_id, phase="recover_wal_only"):
            try:
                operation = self._operation(board_id)
            except GraphCapabilityUnavailable as failure:
                if _is_missing_binding(failure):
                    return WalRecoveryReport(
                        board_id=board_id,
                        status="failed",
                        main_untouched=True,
                        reason=_BINDING_MISSING_REASON,
                    )
                raise
            return await operation(board_id)


__all__ = [
    "BoardGraphOperationWindowFactory",
    "BoardStorageMutationWindowFactory",
    "CommunityRoutedCypherExecutor",
    "CommunityRoutedGraphRecovery",
    "CommunityRoutedGraphRuntimeStore",
    "CommunityRoutedGraphSchemaManager",
    "CommunityRoutedSemanticGraphStore",
]
