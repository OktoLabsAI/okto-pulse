"""Coherent Board-only composition for immutable Community graph routes.

The bundle in this module is deliberately not the edition composition root.  It
builds the complete Board provider set and exposes the three shared routing
objects which an umbrella Board+Global composition must reuse.  Importing or
constructing it does not create a binding, open a database, or initialize a
schema; ``initialize_board_route`` is the only first-boot door.

Every routed call owns a small operation-local route session.  The first
``inspect`` or ``acquire`` pins one immutable snapshot and every backend-local
resolver in that call receives that exact object.  Revalidation deliberately
bypasses the pin and reads persisted authority again.  The Grafx pool is shared
and unbounded: ordinary synchronous providers cannot return a lease alongside
their Core result, while the transaction provider takes its own lease for the
whole engine scope.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from okto_grafx.errors import GrafxSchemaVersionMismatch
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
)
from okto_pulse.core.kg.interfaces.graph_lifecycle import (
    GraphHandle,
    GraphLifecycleStepResult,
    PurgeReport,
    RebuildReport,
)
from okto_pulse.core.kg.interfaces.graph_recovery import WalRecoveryReport
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeObservationState,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef
from okto_pulse.core.services.application_kg import (
    revalidate_board_graph_write_lease,
)

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.grafx_board_storage import (
    grafx_board_storage_ref,
)
from okto_pulse.community.adapters.grafx_cypher_executor import (
    CommunityGrafxCypherExecutor,
)
from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
    GrafxDatabasePoolError,
)
from okto_pulse.community.adapters.grafx_graph_lifecycle import (
    CommunityGrafxGraphLifecycle,
)
from okto_pulse.community.adapters.grafx_graph_recovery import (
    CommunityGrafxGraphRecovery,
)
from okto_pulse.community.adapters.grafx_graph_runtime_store import (
    CommunityGrafxGraphRuntimeStore,
)
from okto_pulse.community.adapters.grafx_graph_schema_manager import (
    CommunityGrafxGraphSchemaManager,
)
from okto_pulse.community.adapters.grafx_graph_store import (
    CommunityGrafxGraphStore,
)
from okto_pulse.community.adapters.grafx_schema_bootstrap import (
    read_current_grafx_schema_version,
    validate_current_grafx_schema,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_rollout_comparison import (
    CommunityBoardGraphShadowCycleAdapter,
)
from okto_pulse.community.adapters.graph_rollout_coordinator import (
    CommunityBoardGraphRolloutCoordinator,
)
from okto_pulse.community.adapters.graph_rollout_journal import (
    CommunityGraphRolloutJournal,
    CommunityGraphRolloutMutationRecorder,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteCandidate,
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)
from okto_pulse.community.adapters.kg_wal_recovery import CommunityGraphRecovery
from okto_pulse.community.adapters.kuzu_cypher_executor import (
    CommunityKuzuCypherExecutor,
)
from okto_pulse.community.adapters.kuzu_graph_runtime_store import (
    CommunityKuzuGraphRuntimeStore,
)
from okto_pulse.community.adapters.kuzu_graph_schema_manager import (
    CommunityKuzuGraphSchemaManager,
)
from okto_pulse.community.adapters.kuzu_graph_store import CommunityKuzuGraphStore
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    CommunityKuzuGraphTransaction,
)
from okto_pulse.community.adapters.routed_board_graph_facades import (
    CommunityRoutedCypherExecutor,
    CommunityRoutedGraphRecovery,
    CommunityRoutedGraphRuntimeStore,
    CommunityRoutedGraphSchemaManager,
    CommunityRoutedSemanticGraphStore,
)
from okto_pulse.community.adapters.routed_graph_lifecycle import (
    CommunityRoutedGraphLifecycle,
)
from okto_pulse.community.adapters.routed_graph_transaction import (
    CommunityRoutedGraphTransaction,
)
from okto_pulse.community.config import (
    validate_grafx_descriptor_revalidation,
    validate_grafx_page_size,
)

GrafxConnector = Callable[..., Any]
_SessionStatus = Literal["unresolved", "missing", "snapshot"]
_ROLLOUT_ADMIN_MUTATION_PHASES = frozenset(
    {
        "graph_schema_ensure_bootstrapped",
        "graph_schema_migrate",
        "graph_lifecycle_rebuild",
        "graph_lifecycle_purge",
        "purge_board_graph",
        "graph_recovery_ladybug",
        "graph_recovery_grafx",
    }
)
_ROLLOUT_INSPECT_ONLY_ADMIN_PHASES = frozenset(
    {"graph_recovery_ladybug", "graph_recovery_grafx"}
)


@dataclass(frozen=True, slots=True)
class _BoardRouteSession:
    board_id: str
    status: _SessionStatus = "unresolved"
    snapshot: CommunityGraphRouteSnapshot | None = None
    physical: bool = False
    authority_erased: bool = False


def _route_failure(reason: str, *, board_id: str) -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "The routed Community Board graph operation was refused.",
        details={
            "operation": "route_board_graph_composition",
            "reason": reason,
            "scope": "board",
            "scope_id": board_id,
        },
    )


class CommunityBoardRouteSessionResolver(CommunityGraphRouteResolver):
    """A normal route resolver with operation-local Board snapshot pinning."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._board_route_sessions: ContextVar[tuple[_BoardRouteSession, ...]] = (
            ContextVar(
                f"community_board_route_sessions_{id(self)}",
                default=(),
            )
        )
        self._board_route_fresh_read: ContextVar[bool] = ContextVar(
            f"community_board_route_fresh_read_{id(self)}",
            default=False,
        )

    @contextmanager
    def board_route_session(self, board_id: str) -> Iterator[None]:
        if type(board_id) is not str or not board_id:
            raise ValueError("board_id must be non-empty text")
        sessions = self._board_route_sessions.get()
        token = self._board_route_sessions.set(
            (*sessions, _BoardRouteSession(board_id=board_id))
        )
        try:
            yield
        finally:
            self._board_route_sessions.reset(token)

    def _session(self, board_id: str) -> _BoardRouteSession | None:
        sessions = self._board_route_sessions.get()
        if not sessions or sessions[-1].board_id != board_id:
            return None
        return sessions[-1]

    def _set_session(self, state: _BoardRouteSession) -> None:
        sessions = self._board_route_sessions.get()
        if not sessions or sessions[-1].board_id != state.board_id:
            raise _route_failure("board_route_session_missing", board_id=state.board_id)
        self._board_route_sessions.set((*sessions[:-1], state))

    def _cache_missing(self, board_id: str) -> None:
        state = self._session(board_id)
        if state is not None:
            self._set_session(replace(state, status="missing"))

    def _cache_snapshot(
        self,
        board_id: str,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        physical: bool,
    ) -> CommunityGraphRouteSnapshot:
        state = self._session(board_id)
        if state is None:
            return snapshot
        if state.status == "snapshot":
            assert state.snapshot is not None
            if state.snapshot != snapshot:
                raise _route_failure("graph_route_snapshot_mismatch", board_id=board_id)
            self._set_session(
                replace(
                    state,
                    physical=state.physical or physical,
                )
            )
            return state.snapshot
        self._set_session(
            replace(
                state,
                status="snapshot",
                snapshot=snapshot,
                physical=state.physical or physical,
            )
        )
        return snapshot

    def inspect_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        if self._board_route_fresh_read.get():
            return super().inspect_board_route(board_id)
        state = self._session(board_id)
        if state is not None:
            if state.status == "snapshot":
                assert state.snapshot is not None
                return state.snapshot
            if state.status == "missing":
                raise _route_failure("binding_missing", board_id=board_id)
        try:
            snapshot = super().inspect_board_route(board_id)
        except GraphCapabilityUnavailable as failure:
            if failure.details.get("reason") == "binding_missing":
                self._cache_missing(board_id)
            raise
        return self._cache_snapshot(board_id, snapshot, physical=False)

    def acquire_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        if self._board_route_fresh_read.get():
            return super().acquire_board_route(board_id)
        state = self._session(board_id)
        if state is not None:
            if state.status == "snapshot" and state.physical:
                assert state.snapshot is not None
                return state.snapshot
            if state.status == "missing":
                raise _route_failure("binding_missing", board_id=board_id)
        try:
            snapshot = super().acquire_board_route(board_id)
        except GraphCapabilityUnavailable as failure:
            if failure.details.get("reason") == "binding_missing":
                self._cache_missing(board_id)
            raise
        return self._cache_snapshot(board_id, snapshot, physical=True)

    def revalidate_snapshot(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        require_physical: bool = False,
    ) -> CommunityGraphRouteSnapshot:
        # Base revalidation calls the virtual acquire/inspect doors.  A fresh
        # marker makes those doors bypass this session instead of comparing the
        # snapshot to itself.
        token = self._board_route_fresh_read.set(True)
        try:
            return super().revalidate_snapshot(
                snapshot,
                require_physical=require_physical,
            )
        finally:
            self._board_route_fresh_read.reset(token)

    def current_board_snapshot(
        self,
        board_id: str,
        *,
        require_physical: bool,
    ) -> CommunityGraphRouteSnapshot | None:
        state = self._session(board_id)
        if state is None:
            raise _route_failure("board_route_session_missing", board_id=board_id)
        if state.status == "missing":
            return None
        return (
            self.acquire_board_route(board_id)
            if require_physical
            else self.inspect_board_route(board_id)
        )

    def require_exact_session_snapshot(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        require_physical: bool,
    ) -> CommunityGraphRouteSnapshot:
        current = self.current_board_snapshot(
            snapshot.scope_id,
            require_physical=require_physical,
        )
        if current is None or current is not snapshot:
            # Identity, not only equality, is the pinning contract inside one
            # operation. Persisted revalidation below still compares values.
            raise _route_failure(
                "graph_route_operation_snapshot_not_pinned",
                board_id=snapshot.scope_id,
            )
        return self.revalidate_snapshot(
            snapshot,
            require_physical=require_physical,
        )

    def revalidate_session_authority(
        self,
        board_id: str,
        *,
        require_physical: bool,
        allow_erased: bool = False,
    ) -> CommunityGraphRouteSnapshot | None:
        state = self._session(board_id)
        if state is None:
            raise _route_failure("board_route_session_missing", board_id=board_id)
        if state.authority_erased and allow_erased:
            return state.snapshot
        if state.status == "snapshot":
            assert state.snapshot is not None
            return self.revalidate_snapshot(
                state.snapshot,
                require_physical=require_physical,
            )

        token = self._board_route_fresh_read.set(True)
        try:
            try:
                super().inspect_board_route(board_id)
            except GraphCapabilityUnavailable as failure:
                if failure.details.get("reason") == "binding_missing":
                    return None
                raise
        finally:
            self._board_route_fresh_read.reset(token)
        raise _route_failure("graph_route_missing_authority_changed", board_id=board_id)

    def mark_session_authority_erased(self, board_id: str) -> None:
        state = self._session(board_id)
        if state is None:
            raise _route_failure("board_route_session_missing", board_id=board_id)
        self._set_session(replace(state, authority_erased=True))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(Path(os.path.abspath(left)))) == os.path.normcase(
        str(Path(os.path.abspath(right)))
    )


class _GrafxBoardAccess:
    def __init__(
        self,
        resolver: CommunityBoardRouteSessionResolver,
        pool: CommunityGrafxDatabasePool,
        binding_store: CommunityGraphBackendBindingStore,
        rollout_mutation_recorder: CommunityGraphRolloutMutationRecorder,
        *,
        configured_page_size: int,
        connect: GrafxConnector | None,
    ) -> None:
        self.resolver = resolver
        self.pool = pool
        self.binding_store = binding_store
        self.rollout_mutation_recorder = rollout_mutation_recorder
        self.configured_page_size = validate_grafx_page_size(configured_page_size)
        self.connect = connect

    def _snapshot(
        self,
        board_id: str,
        *,
        require_physical: bool,
    ) -> CommunityGraphRouteSnapshot:
        snapshot = self.resolver.current_board_snapshot(
            board_id,
            require_physical=require_physical,
        )
        if (
            snapshot is None
            or snapshot.backend != "grafx"
            or snapshot.page_size is None
        ):
            raise _route_failure("grafx_board_route_required", board_id=board_id)
        return snapshot

    def database(self, board_id: str):
        snapshot = self._snapshot(board_id, require_physical=True)
        assert snapshot.page_size is not None
        database = self.pool.get(
            snapshot.active_path,
            page_size=snapshot.page_size,
        )
        self.resolver.admit_grafx_route(
            snapshot,
            database,
            operation="resolve_routed_board_grafx_database",
        )
        return database

    def path(self, board_id: str) -> Path:
        return self._snapshot(board_id, require_physical=False).active_path

    def board_root(self, board_id: str) -> Path:
        return self.binding_store.board_ladybug_path(board_id).parent

    def admission(self, board_id: str, database: Any) -> None:
        snapshot = self._snapshot(board_id, require_physical=True)
        self.resolver.admit_grafx_route(
            snapshot,
            database,
            operation="admit_routed_board_grafx_provider",
        )

    def write_fence(self, board_id: str, phase: str) -> None:
        revalidate_board_graph_write_lease(board_id, failure_phase=phase)
        snapshot = self._snapshot(board_id, require_physical=True)
        self.resolver.revalidate_snapshot(snapshot, require_physical=True)
        self.rollout_mutation_recorder.close_rollback_before_write_if_active(
            board_id,
            snapshot.binding_sha256,
            snapshot.backend,
        )

    def runtime_fence(self, board_id: str, phase: str) -> None:
        revalidate_board_graph_write_lease(board_id, failure_phase=phase)
        snapshot = self.resolver.revalidate_session_authority(
            board_id,
            require_physical=phase != "privacy_erase",
            allow_erased=phase == "privacy_erase",
        )
        if snapshot is not None and snapshot.backend not in {"ladybug", "grafx"}:
            raise _route_failure("board_route_backend_invalid", board_id=board_id)

    def _board_pool_paths(self, board_id: str) -> tuple[Path, ...]:
        grafx_root = self.binding_store.board_grafx_path(
            board_id, "generation-1"
        ).parent
        selected: list[Path] = []
        for raw in self.pool.pooled_paths():
            candidate = Path(raw)
            try:
                candidate.relative_to(grafx_root)
            except ValueError:
                continue
            selected.append(candidate)
        return tuple(selected)

    def close(self, board_id: str | None) -> None:
        if board_id is None:
            boards_root = self.binding_store.root / "boards"
            paths = []
            for raw in self.pool.pooled_paths():
                candidate = Path(raw)
                try:
                    candidate.relative_to(boards_root)
                except ValueError:
                    continue
                paths.append(candidate)
        else:
            paths = list(self._board_pool_paths(board_id))
        failures: list[BaseException] = []
        for path in paths:
            try:
                self.pool.close(path)
            except BaseException as failure:  # noqa: BLE001 - account for every handle
                failures.append(failure)
        if failures:
            primary = failures[0]
            for secondary in failures[1:]:
                primary.add_note(
                    "closing another routed Grafx Board handle also failed: "
                    f"{type(secondary).__name__}: {secondary}"
                )
            raise primary

    def open_for_adoption(self, path: Path):
        try:
            return self.pool.get(path, page_size=self.configured_page_size)
        except GrafxDatabasePoolError as failure:
            cause = failure.__cause__
            if not isinstance(cause, GrafxSchemaVersionMismatch):
                raise
            if cause.details.get("field") != "page_size":
                raise
            stored = cause.details.get("stored")
            if type(stored) is not int:
                raise
            page_size = validate_grafx_page_size(stored)
            return self.pool.get(path, page_size=page_size)

    def open_temporary(self, path: Path):
        snapshot = self._snapshot(path.parent.parent.name, require_physical=True)
        if not _same_path(snapshot.active_path, path) or snapshot.page_size is None:
            raise _route_failure(
                "grafx_recovery_path_mismatch",
                board_id=snapshot.scope_id,
            )
        return self.pool.open_unpooled(
            path,
            page_size=snapshot.page_size,
            connect=self.connect,
        )


class _LadybugRuntimeMutations:
    def __init__(
        self,
        resolver: CommunityBoardRouteSessionResolver,
        runtime: CommunityKuzuGraphRuntimeStore,
        path_guard: Callable[[str], None],
    ) -> None:
        self.resolver = resolver
        self.runtime = runtime
        self.path_guard = path_guard

    def _revalidate(self, board_id: str, *, phase: str) -> None:
        revalidate_board_graph_write_lease(board_id, failure_phase=phase)
        snapshot = self.resolver.revalidate_session_authority(
            board_id,
            require_physical=False,
            allow_erased=True,
        )
        if snapshot is not None and snapshot.backend not in {"ladybug", "grafx"}:
            raise _route_failure("board_route_backend_invalid", board_id=board_id)
        self.path_guard(board_id)

    def purge(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        before = self.runtime.graph_state(board_id)
        try:
            self._revalidate(board_id, phase="runtime_purge_ladybug")
            affected, _quarantine = (
                kg_runtime.purge_board_graph_storage_with_receipt_unguarded(
                    board_id,
                    reason=reason,
                )
            )
        except Exception as failure:  # noqa: BLE001 - return Core failure receipt
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=CommunityKuzuGraphRuntimeStore._BACKEND,
                error_code=type(failure).__name__,
            )
        after = self.runtime.graph_state(board_id)
        if after.normalized_state is not GraphRuntimeObservationState.CONFIRMED_ABSENT:
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=CommunityKuzuGraphRuntimeStore._BACKEND,
                error_code=(
                    "purge_did_not_remove_existing_graph"
                    if before.exists
                    else "purge_absence_unverified"
                ),
            )
        return GraphPurgeResult(
            board_id=board_id,
            removed=bool(affected),
            not_found=not bool(affected),
            status="purged" if affected else "not_found",
            reason=reason,
            backend=CommunityKuzuGraphRuntimeStore._BACKEND,
        )

    def erase(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        before = self.runtime.graph_state(board_id)
        try:
            self._revalidate(board_id, phase="runtime_privacy_erase_ladybug")
            affected = kg_runtime.erase_board_graph_storage_for_privacy_unguarded(
                board_id,
                reason=reason,
            )
        except Exception as failure:  # noqa: BLE001 - return Core failure receipt
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=CommunityKuzuGraphRuntimeStore._BACKEND,
                error_code=type(failure).__name__,
            )
        after = self.runtime.graph_state(board_id)
        if after.normalized_state is not GraphRuntimeObservationState.CONFIRMED_ABSENT:
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=CommunityKuzuGraphRuntimeStore._BACKEND,
                error_code="physical_erasure_absence_unverified",
            )
        return GraphPurgeResult(
            board_id=board_id,
            removed=bool(affected),
            not_found=not before.exists and not bool(affected),
            status="erased" if affected else "not_found",
            reason=reason,
            backend=CommunityKuzuGraphRuntimeStore._BACKEND,
        )


@dataclass(frozen=True, slots=True)
class CommunityRoutedBoardGraphComposition:
    """All Board graph ports plus the shared physical routing identities."""

    binding_store: CommunityGraphBackendBindingStore
    resolver: CommunityBoardRouteSessionResolver
    grafx_pool: CommunityGrafxDatabasePool
    graph_store: CommunityRoutedSemanticGraphStore
    cypher_executor: CommunityRoutedCypherExecutor
    graph_transaction: CommunityRoutedGraphTransaction
    graph_schema_manager: CommunityRoutedGraphSchemaManager
    graph_lifecycle: CommunityRoutedGraphLifecycle
    graph_runtime_store: CommunityRoutedGraphRuntimeStore
    graph_recovery: CommunityRoutedGraphRecovery
    graph_rollout_coordinator: CommunityBoardGraphRolloutCoordinator
    _initialize_physical: Callable[[CommunityGraphRouteCandidate], object | None]
    _rematerialize_physical: Callable[[CommunityGraphRouteCandidate], object | None]

    def _require_route_materialization_allowed(self, board_id: str) -> None:
        """Refuse every route-creation door while privacy erasure is durable."""

        journal = CommunityGraphRolloutJournal(
            self.binding_store.root,
            board_id,
        )
        # Preserve the route resolver's existing empty-board and filesystem
        # alias diagnostics when no rollout storage exists. ``lexists`` still
        # sends a broken/aliased rollout root through the journal's fail-closed
        # layout validation instead of treating it as finalized absence.
        if not os.path.lexists(journal.rollout_root):
            return
        rollout = journal.read_if_exists()
        if rollout is not None and rollout.state == "erased":
            raise _route_failure(
                "graph_rollout_privacy_tombstone_active",
                board_id=board_id,
            )

    def initialize_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        """Create/adopt and publish one Board route, only when explicitly called."""

        phase = "initialize_board_route"
        from okto_pulse.community.adapters.ladybug_writer import (
            ladybug_writer_scope,
        )

        # Privacy invalidation enters this same writer authority before its
        # exclusive close window. Retain the writer across the tombstone check,
        # discovery/materialization, publication and revalidation so privacy
        # cannot enter between them. A first Ladybug bootstrap registers a
        # normal close-guard reader, so this door intentionally retains the
        # common writer facet without entering the exclusive close facet.
        with ladybug_writer_scope(scope=board_id, phase=phase):
            revalidate_board_graph_write_lease(
                board_id,
                failure_phase=phase,
            )
            self._require_route_materialization_allowed(board_id)
            snapshot = self.resolver.initialize_board_route(
                board_id,
                create_physical=self._initialize_physical,
            )
            self.resolver.revalidate_snapshot(snapshot, require_physical=True)
        return snapshot

    def adopt_existing_board_route(
        self,
        board_id: str,
    ) -> CommunityGraphRouteSnapshot | None:
        """Adopt physical storage without creating an absent Board target."""

        phase = "adopt_existing_board_route"
        from okto_pulse.community.adapters.ladybug_writer import (
            ladybug_writer_scope,
        )

        with ladybug_writer_scope(scope=board_id, phase=phase):
            revalidate_board_graph_write_lease(
                board_id,
                failure_phase=phase,
            )
            self._require_route_materialization_allowed(board_id)
            return self.resolver.adopt_existing_board_route(board_id)

    def rematerialize_board_route(
        self,
        board_id: str,
    ) -> CommunityGraphRouteSnapshot:
        """Explicitly recreate the exact target authorized by a rebuild."""

        phase = "rematerialize_board_route_after_purge"
        with kg_runtime.board_storage_mutation_window(board_id, phase=phase):
            revalidate_board_graph_write_lease(board_id, failure_phase=phase)
            self._require_route_materialization_allowed(board_id)
            snapshot = self.resolver.rematerialize_board_route(
                board_id,
                create_physical=self._rematerialize_physical,
            )
            self.resolver.revalidate_snapshot(snapshot, require_physical=True)
        return snapshot

    def registry_providers(self) -> dict[str, Any]:
        """Return only the unchanged Core Board registry slots."""

        return {
            "graph_store": self.graph_store,
            "cypher_executor": self.cypher_executor,
            "graph_transaction": self.graph_transaction,
            "graph_schema_manager": self.graph_schema_manager,
            "graph_lifecycle": self.graph_lifecycle,
            "graph_runtime_store": self.graph_runtime_store,
            "graph_recovery": self.graph_recovery,
        }


def _validated_shared_components(
    *,
    root: Path | None,
    binding_store: CommunityGraphBackendBindingStore | None,
    resolver: CommunityBoardRouteSessionResolver | None,
    grafx_pool: CommunityGrafxDatabasePool | None,
) -> tuple[
    CommunityGraphBackendBindingStore | None,
    CommunityBoardRouteSessionResolver | None,
    CommunityGrafxDatabasePool | None,
]:
    supplied = (binding_store, resolver, grafx_pool)
    if all(value is None for value in supplied):
        return None, None, None
    if any(value is None for value in supplied):
        raise ValueError(
            "binding_store, resolver and grafx_pool must be supplied together"
        )
    assert binding_store is not None
    assert resolver is not None
    assert grafx_pool is not None
    if not isinstance(resolver, CommunityBoardRouteSessionResolver):
        raise TypeError("resolver must be a CommunityBoardRouteSessionResolver")
    if getattr(resolver, "_store", None) is not binding_store:
        raise ValueError("resolver must own the supplied binding_store")
    if not _same_path(binding_store.root, grafx_pool._root):
        raise ValueError("binding_store and grafx_pool roots must match")
    if root is not None and not _same_path(root, binding_store.root):
        raise ValueError("supplied root does not match shared graph components")
    if getattr(grafx_pool, "_max_entries", object()) is not None:
        raise ValueError("the shared Grafx pool must be unbounded for Board providers")
    return binding_store, resolver, grafx_pool


def build_community_routed_board_graph_composition(
    *,
    settings: Any,
    kg_base_dir: str | os.PathLike[str] | None = None,
    binding_store: CommunityGraphBackendBindingStore | None = None,
    resolver: CommunityBoardRouteSessionResolver | None = None,
    grafx_pool: CommunityGrafxDatabasePool | None = None,
    grafx_connect: GrafxConnector | None = None,
) -> CommunityRoutedBoardGraphComposition:
    """Build the routed Board bundle, optionally over prebuilt shared objects.

    When shared objects are supplied all three are mandatory and their identity,
    root and unbounded-pool policy are validated.  This lets the umbrella
    Board+Global composition reuse exactly one store, resolver and pool instead
    of constructing lookalikes.
    """

    configured_root = Path(
        os.fspath(kg_base_dir if kg_base_dir is not None else settings.kg_base_dir)
    ).expanduser()
    configured_page_size = validate_grafx_page_size(settings.kg_grafx_page_size)
    configured_descriptor_revalidation = validate_grafx_descriptor_revalidation(
        getattr(settings, "kg_grafx_descriptor_revalidation", "generation")
    )
    board_backend = settings.kg_graph_backend
    global_backend = settings.kg_global_graph_backend
    local_adoption_opener: list[Callable[[Path], Any]] = [
        lambda _path: (_ for _ in ()).throw(RuntimeError("Grafx opener not composed"))
    ]

    shared_store, shared_resolver, shared_pool = _validated_shared_components(
        root=configured_root,
        binding_store=binding_store,
        resolver=resolver,
        grafx_pool=grafx_pool,
    )
    if shared_store is None:
        binding_store = CommunityGraphBackendBindingStore(configured_root)
        grafx_pool = CommunityGrafxDatabasePool(
            binding_store.root,
            connect=grafx_connect,
            max_entries=None,
            descriptor_revalidation=configured_descriptor_revalidation,
        )
        resolver = CommunityBoardRouteSessionResolver(
            binding_store,
            board_backend=board_backend,
            global_backend=global_backend,
            grafx_page_size=configured_page_size,
            # The closure is installed just below, after its access object is
            # available. A small mutable cell avoids constructing a second
            # resolver merely to inject the opener.
            open_grafx_database=lambda path: local_adoption_opener[0](path),
        )
    else:
        binding_store = shared_store
        resolver = shared_resolver
        grafx_pool = shared_pool

    assert binding_store is not None
    assert resolver is not None
    assert grafx_pool is not None
    if grafx_pool.descriptor_revalidation != configured_descriptor_revalidation:
        raise ValueError(
            "the shared Grafx pool descriptor revalidation policy must match settings"
        )
    rollout_mutation_recorder = CommunityGraphRolloutMutationRecorder(
        binding_store.root
    )
    if getattr(resolver, "_board_backend", None) != board_backend:
        raise ValueError("shared resolver Board backend does not match settings")
    if getattr(resolver, "_global_backend", None) != global_backend:
        raise ValueError("shared resolver Global backend does not match settings")
    if getattr(resolver, "_grafx_page_size", None) != configured_page_size:
        raise ValueError("shared resolver Grafx page size does not match settings")

    connector = grafx_connect
    if connector is None:
        connector = getattr(grafx_pool, "_connect", None)
    access = _GrafxBoardAccess(
        resolver,
        grafx_pool,
        binding_store,
        rollout_mutation_recorder,
        configured_page_size=configured_page_size,
        connect=connector,
    )
    if shared_store is None:
        local_adoption_opener[0] = access.open_for_adoption
    else:
        # A prebuilt resolver can only be safely reused when its physical
        # adoption door is rebound to the exact shared pool supplied here.
        resolver._open_grafx_database = access.open_for_adoption

    def rollout_administrative_write_fence(
        board_id: str,
        phase: str,
        snapshot: CommunityGraphRouteSnapshot | None = None,
    ) -> None:
        """Fence non-logical mutations against a stale rollout checkpoint."""

        revalidate_board_graph_write_lease(board_id, failure_phase=phase)
        require_physical = phase not in _ROLLOUT_INSPECT_ONLY_ADMIN_PHASES
        observed = snapshot or resolver.current_board_snapshot(
            board_id,
            require_physical=require_physical,
        )
        if observed is None:
            raise _route_failure("board_route_required", board_id=board_id)
        resolver.require_exact_session_snapshot(
            observed,
            require_physical=require_physical,
        )
        if phase not in _ROLLOUT_ADMIN_MUTATION_PHASES:
            return
        if observed.backend == "grafx":
            rollout_mutation_recorder.close_rollback_before_write_if_active(
                board_id,
                observed.binding_sha256,
                "grafx",
            )
            return

        # The callback runs before a Ladybug administrative operation and has
        # no matching post-call seam.  Retaining this record as ``prepared`` is
        # intentional: the next fixed full-state snapshot resolves whether the
        # operation changed data, while its allocation moves the high-water so
        # a stale candidate cannot be cut over.
        rollout_mutation_recorder.prepare_mutation(
            board_id=board_id,
            binding_sha256=observed.binding_sha256,
            backend="ladybug",
            transaction_id=f"admin-{secrets.token_hex(16)}",
            family="administrative_write",
            payload={"phase": phase},
        )

    def invalidate_rollout_for_privacy(
        board_id: str,
        *,
        reason: str,
    ) -> GraphPurgeResult:
        """Persist the privacy tombstone before either backend is touched."""

        revalidate_board_graph_write_lease(
            board_id,
            failure_phase="privacy_invalidate_graph_rollout",
        )
        journal = CommunityGraphRolloutJournal(binding_store.root, board_id)
        current = journal.read_if_exists()
        if current is None:
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=True,
                status="not_found",
                reason=reason,
                backend="rollout",
            )
        already_invalidated = current.state == "erased"
        journal.close_for_privacy(expected_version=current.state_version)
        return GraphPurgeResult(
            board_id=board_id,
            removed=not already_invalidated,
            not_found=already_invalidated,
            status="not_found" if already_invalidated else "erased",
            reason=reason,
            backend="rollout",
        )

    def finalize_rollout_privacy_storage(
        board_id: str,
        *,
        reason: str,
    ) -> GraphPurgeResult:
        """Remove rollout bytes only after both physical erasures succeeded."""

        journal = CommunityGraphRolloutJournal(binding_store.root, board_id)
        proof = journal.erase_privacy_storage(
            before_mutation=lambda: revalidate_board_graph_write_lease(
                board_id,
                failure_phase="privacy_finalize_graph_rollout",
            )
        )
        removed = proof.files_removed > 0 or proof.directories_removed > 0
        return GraphPurgeResult(
            board_id=board_id,
            removed=removed,
            not_found=not removed,
            status="erased" if removed else "not_found",
            reason=reason,
            backend="rollout",
        )

    def require_ladybug_runtime_path(board_id: str) -> None:
        expected = binding_store.board_ladybug_path(board_id)
        try:
            observed = kg_runtime.board_kuzu_path(board_id)
        except Exception as failure:
            raise _route_failure(
                "ladybug_runtime_path_unavailable",
                board_id=board_id,
            ) from failure
        if not _same_path(expected, observed):
            raise _route_failure(
                "ladybug_runtime_path_mismatch",
                board_id=board_id,
            )

    @contextmanager
    def board_route_session(board_id: str) -> Iterator[None]:
        with resolver.board_route_session(board_id):
            try:
                snapshot = resolver.inspect_board_route(board_id)
            except GraphCapabilityUnavailable as failure:
                if failure.details.get("reason") != "binding_missing":
                    raise
            else:
                if snapshot.backend == "ladybug":
                    require_ladybug_runtime_path(board_id)
            yield

    @contextmanager
    def operation_window(board_id: str) -> Iterator[None]:
        with (
            kg_runtime.board_graph_operation_window(board_id),
            board_route_session(board_id),
        ):
            yield

    @contextmanager
    def mutation_window(board_id: str, *, phase: str) -> Iterator[None]:
        with (
            kg_runtime.board_storage_mutation_window(board_id, phase=phase),
            board_route_session(board_id),
        ):
            revalidate_board_graph_write_lease(board_id, failure_phase=phase)
            yield

    @contextmanager
    def lifecycle_mutation_window(board_id: str, *, phase: str) -> Iterator[None]:
        with board_route_session(board_id):
            snapshot = resolver.inspect_board_route(board_id)
            # Core's logical Board writer lease and Ladybug's process-wide
            # native single-writer constraint are distinct authorities.  Only
            # the Ladybug route needs the native (logically re-entrant) gate;
            # Grafx retains the backend-neutral exclusive close window.
            physical_window = (
                kg_runtime.board_storage_mutation_window
                if snapshot.backend == "ladybug"
                else kg_runtime.board_storage_mutation_window_unguarded
            )
            with physical_window(board_id, phase=phase):
                yield

    ladybug_store = CommunityKuzuGraphStore()
    ladybug_cypher = CommunityKuzuCypherExecutor()
    ladybug_transaction = CommunityKuzuGraphTransaction()
    ladybug_schema = CommunityKuzuGraphSchemaManager()
    ladybug_runtime = CommunityKuzuGraphRuntimeStore()
    ladybug_mutations = _LadybugRuntimeMutations(
        resolver,
        ladybug_runtime,
        require_ladybug_runtime_path,
    )
    ladybug_recovery = CommunityGraphRecovery()

    grafx_store = CommunityGrafxGraphStore(access.database, access.write_fence)
    grafx_cypher = CommunityGrafxCypherExecutor(access.database)
    grafx_schema = CommunityGrafxGraphSchemaManager(
        access.database,
        access.write_fence,
        admission=access.admission,
    )
    grafx_runtime = CommunityGrafxGraphRuntimeStore(
        access.path,
        access.close,
        access.runtime_fence,
        board_storage_root_resolver=access.board_root,
        configured_max_bytes=lambda: int(
            getattr(settings, "kg_ladybug_max_db_size_gb", 2) * 1024**3
        ),
    )
    grafx_lifecycle = CommunityGrafxGraphLifecycle(
        access.database,
        access.path,
        access.close,
        access.write_fence,
        admission=access.admission,
    )
    grafx_recovery = CommunityGrafxGraphRecovery(
        quarantine_root=binding_store.root / "quarantine",
        database_path_resolver=access.path,
        open_database=access.open_temporary,
        close_board=lambda board_id: access.close(board_id),
        revalidate_fence=access.runtime_fence,
        mutation_guard=lambda _board_id: nullcontext(),
    )

    def require_ladybug_snapshot(
        snapshot: CommunityGraphRouteSnapshot,
        *,
        require_physical: bool,
    ) -> str:
        if snapshot.scope != "board" or snapshot.backend != "ladybug":
            raise _route_failure(
                "ladybug_board_route_required",
                board_id=snapshot.scope_id,
            )
        expected = binding_store.board_ladybug_path(snapshot.scope_id)
        if not _same_path(snapshot.active_path, expected):
            raise _route_failure(
                "ladybug_board_path_mismatch",
                board_id=snapshot.scope_id,
            )
        require_ladybug_runtime_path(snapshot.scope_id)
        resolver.require_exact_session_snapshot(
            snapshot,
            require_physical=require_physical,
        )
        return snapshot.scope_id

    async def ladybug_open(snapshot: CommunityGraphRouteSnapshot) -> GraphHandle:
        board_id = require_ladybug_snapshot(snapshot, require_physical=True)
        with kg_runtime.registered_raw_connection(board_id) as (_database, connection):
            result = connection.execute(
                "CALL SHOW_TABLES() WHERE name = 'BoardMeta' RETURN name"
            )
            try:
                if not result.has_next():
                    raise GraphCorruption(
                        "The routed Ladybug Board graph has no BoardMeta table.",
                        details={
                            "operation": "open_routed_ladybug_board",
                            "reason": "board_meta_missing",
                            "board_id": board_id,
                        },
                    )
            finally:
                result.close()
            result = connection.execute(
                "MATCH (m:BoardMeta {board_id: $bid}) RETURN m.schema_version",
                {"bid": board_id},
            )
            try:
                if not result.has_next():
                    raise GraphCorruption(
                        "The routed Ladybug Board graph has no BoardMeta row.",
                        details={
                            "operation": "open_routed_ladybug_board",
                            "reason": "board_meta_row_missing",
                            "board_id": board_id,
                        },
                    )
                row = result.get_next()
                if not row or not row[0]:
                    raise GraphCorruption(
                        "The routed Ladybug Board graph has no schema version.",
                        details={
                            "operation": "open_routed_ladybug_board",
                            "reason": "board_meta_schema_version_missing",
                            "board_id": board_id,
                        },
                    )
            finally:
                result.close()
        return GraphHandle(
            board_id=board_id,
            storage_ref=StorageRef(f"board:{board_id}", "community_local_graph"),
            opened=True,
            status="opened",
            locked=False,
            quarantined=False,
        )

    async def ladybug_close(snapshot: CommunityGraphRouteSnapshot) -> None:
        # The outer lifecycle mutation window already drained readers and
        # closed both the connection pool and cached Database.
        require_ladybug_snapshot(snapshot, require_physical=True)

    async def ladybug_rebuild(
        snapshot: CommunityGraphRouteSnapshot,
    ) -> RebuildReport:
        board_id = require_ladybug_snapshot(snapshot, require_physical=True)
        try:
            kg_runtime.ensure_board_graph_bootstrapped_unguarded(board_id)
        except Exception as failure:  # noqa: BLE001 - structured lifecycle evidence
            return RebuildReport(
                board_id=board_id,
                status="failed",
                steps=("close_all_connections",),
                reason=str(failure),
            )
        return RebuildReport(
            board_id=board_id,
            status="rebuilt",
            steps=(
                "close_all_connections",
                "ensure_board_graph_bootstrapped",
            ),
        )

    async def ladybug_purge(
        snapshot: CommunityGraphRouteSnapshot,
        *,
        reason: str,
    ) -> PurgeReport:
        board_id = require_ladybug_snapshot(snapshot, require_physical=True)
        affected, quarantine = (
            kg_runtime.purge_board_graph_storage_with_receipt_unguarded(
                board_id,
                reason=reason,
            )
        )
        return PurgeReport(
            board_id=board_id,
            status="purged" if affected else "noop",
            reason=reason,
            affected_storage_refs=tuple(
                StorageRef(
                    f"board:{board_id}:artifact:{index}",
                    "community_local_graph",
                )
                for index, _path in enumerate(affected)
            ),
            quarantined=bool(affected),
            quarantine_ref=quarantine,
        )

    def ladybug_step(
        snapshot: CommunityGraphRouteSnapshot,
        graph_type: str,
        step: str,
    ) -> GraphLifecycleStepResult:
        board_id = require_ladybug_snapshot(snapshot, require_physical=True)
        result = kg_runtime.apply_ladybug_lifecycle_step_unguarded(
            board_id,
            graph_type,
            step,
        )
        return GraphLifecycleStepResult(
            ok=bool(getattr(result, "ok", False)),
            detail=getattr(result, "detail", None),
        )

    async def ladybug_close_all() -> None:
        from okto_pulse.community.adapters.graph_connection_pool import (
            close_all_board_connections,
        )

        close_all_board_connections()
        # This closes only the per-board Ladybug cache.  The Global singleton
        # lives in another module and is intentionally untouched here.
        kg_runtime.close_board_db_cache(board_id=None)

    async def grafx_open(snapshot: CommunityGraphRouteSnapshot) -> GraphHandle:
        resolver.require_exact_session_snapshot(snapshot, require_physical=True)
        database = access.database(snapshot.scope_id)
        validate_current_grafx_schema(database)
        if not read_current_grafx_schema_version(database):
            raise GraphCorruption(
                "The routed Grafx Board graph has no BoardMeta schema version.",
                details={
                    "operation": "open_routed_grafx_board",
                    "reason": "board_meta_missing",
                    "board_id": snapshot.scope_id,
                },
            )
        opened = not bool(getattr(database, "closed", False))
        return GraphHandle(
            board_id=snapshot.scope_id,
            storage_ref=grafx_board_storage_ref(snapshot.scope_id),
            opened=opened,
            status="opened" if opened else "absent",
            locked=False,
            quarantined=False,
        )

    async def grafx_close(snapshot: CommunityGraphRouteSnapshot) -> None:
        resolver.require_exact_session_snapshot(snapshot, require_physical=True)
        await grafx_lifecycle.close(snapshot.scope_id)

    async def grafx_rebuild(snapshot: CommunityGraphRouteSnapshot) -> RebuildReport:
        resolver.require_exact_session_snapshot(snapshot, require_physical=True)
        return await grafx_lifecycle.rebuild(snapshot.scope_id)

    async def grafx_purge(
        snapshot: CommunityGraphRouteSnapshot,
        *,
        reason: str,
    ) -> PurgeReport:
        resolver.require_exact_session_snapshot(snapshot, require_physical=True)
        return await grafx_lifecycle.purge(snapshot.scope_id, reason=reason)

    def grafx_step(
        snapshot: CommunityGraphRouteSnapshot,
        graph_type: str,
        step: str,
    ) -> GraphLifecycleStepResult:
        resolver.require_exact_session_snapshot(snapshot, require_physical=True)
        return grafx_lifecycle.apply_step(snapshot.scope_id, graph_type, step)

    async def grafx_close_all() -> None:
        access.close(None)

    async def ladybug_recover(board_id: str) -> WalRecoveryReport:
        snapshot = resolver.current_board_snapshot(board_id, require_physical=False)
        if snapshot is None:
            raise _route_failure("board_route_required", board_id=board_id)
        rollout_administrative_write_fence(
            board_id,
            "graph_recovery_ladybug",
            snapshot,
        )
        require_ladybug_runtime_path(board_id)
        return await ladybug_recovery.recover_wal_only_unguarded(board_id)

    async def grafx_recover(board_id: str) -> WalRecoveryReport:
        snapshot = resolver.current_board_snapshot(board_id, require_physical=False)
        if snapshot is None:
            raise _route_failure("board_route_required", board_id=board_id)
        rollout_administrative_write_fence(
            board_id,
            "graph_recovery_grafx",
            snapshot,
        )
        return await grafx_recovery.recover_wal_only(board_id)

    def grafx_erase(board_id: str, *, reason: str) -> GraphPurgeResult:
        result = grafx_runtime.erase_board_graph(board_id, reason=reason)
        if result.error_code is None and result.status in {"erased", "not_found"}:
            resolver.mark_session_authority_erased(board_id)
        return result

    graph_store = CommunityRoutedSemanticGraphStore(
        resolver,
        ladybug=ladybug_store,
        grafx=grafx_store,
        operation_window=operation_window,
        revalidate_write_fence=lambda board_id, phase: (
            revalidate_board_graph_write_lease(board_id, failure_phase=phase)
        ),
        mutation_recorder=rollout_mutation_recorder,
    )
    cypher_executor = CommunityRoutedCypherExecutor(
        resolver,
        ladybug=ladybug_cypher,
        grafx=grafx_cypher,
        operation_window=operation_window,
    )
    graph_schema_manager = CommunityRoutedGraphSchemaManager(
        resolver,
        ladybug=ladybug_schema,
        grafx=grafx_schema,
        operation_window=operation_window,
        revalidate_write_fence=rollout_administrative_write_fence,
    )
    graph_transaction = CommunityRoutedGraphTransaction(
        resolver,
        ladybug=ladybug_transaction,
        grafx_pool=grafx_pool,
        # A GraphTransaction is opened in the async orchestration context but
        # its blocking engine work (including terminal commit/rollback) runs
        # in a worker thread.  The generic operation_window also owns a
        # ContextVar route-session token, which cannot legally be reset from
        # that copied worker context.  Transactions already pin and
        # revalidate their immutable snapshot explicitly, so retain only the
        # thread-neutral physical close guard for their full lifetime.
        operation_window=kg_runtime.board_graph_operation_window,
        mutation_recorder=rollout_mutation_recorder,
    )
    graph_lifecycle = CommunityRoutedGraphLifecycle(
        resolver,
        operation_window=operation_window,
        mutation_window_unguarded=lifecycle_mutation_window,
        revalidate_write_fence=rollout_administrative_write_fence,
        ladybug_open_unguarded=ladybug_open,
        ladybug_close_unguarded=ladybug_close,
        ladybug_rebuild_unguarded=ladybug_rebuild,
        ladybug_purge_unguarded=ladybug_purge,
        ladybug_apply_step_unguarded=ladybug_step,
        ladybug_close_all_unguarded=ladybug_close_all,
        grafx_open_unguarded=grafx_open,
        grafx_close_unguarded=grafx_close,
        grafx_rebuild_unguarded=grafx_rebuild,
        grafx_purge_unguarded=grafx_purge,
        grafx_apply_step_unguarded=grafx_step,
        grafx_close_all_unguarded=grafx_close_all,
    )
    graph_runtime_store = CommunityRoutedGraphRuntimeStore(
        resolver,
        ladybug=ladybug_runtime,
        grafx=grafx_runtime,
        operation_window=operation_window,
        mutation_window=mutation_window,
        ladybug_purge_unguarded=ladybug_mutations.purge,
        grafx_purge_unguarded=grafx_runtime.purge_board_graph,
        ladybug_erase_unguarded=ladybug_mutations.erase,
        grafx_erase_unguarded=grafx_erase,
        rollout_erase_unguarded=invalidate_rollout_for_privacy,
        rollout_finalize_erase_unguarded=finalize_rollout_privacy_storage,
        rollout_write_fence=rollout_administrative_write_fence,
    )
    graph_recovery = CommunityRoutedGraphRecovery(
        resolver,
        ladybug_recovery_unguarded=ladybug_recover,
        grafx_recovery_unguarded=grafx_recover,
        mutation_window=mutation_window,
    )
    graph_rollout_coordinator = CommunityBoardGraphRolloutCoordinator(
        binding_store,
        CommunityBoardGraphShadowCycleAdapter(connector=connector),
        mutation_window=mutation_window,
        grafx_page_size=configured_page_size,
    )

    def create_board_physical(
        candidate: CommunityGraphRouteCandidate,
        *,
        close_window_owned: bool,
    ) -> object | None:
        if candidate.scope != "board":
            raise _route_failure(
                "board_initialization_candidate_scope_invalid",
                board_id=candidate.scope_id,
            )
        if candidate.backend == "ladybug":
            expected = binding_store.board_ladybug_path(candidate.scope_id)
            if not _same_path(candidate.binding_path, expected):
                raise _route_failure(
                    "ladybug_initialization_path_mismatch",
                    board_id=candidate.scope_id,
                )
            require_ladybug_runtime_path(candidate.scope_id)
            revalidate_board_graph_write_lease(
                candidate.scope_id,
                failure_phase=(
                    "rematerialize_board_route_after_purge"
                    if close_window_owned
                    else "initialize_board_route"
                ),
            )
            if close_window_owned:
                handle = kg_runtime.rematerialize_board_graph_unguarded(
                    candidate.scope_id
                )
            else:
                from okto_pulse.community.adapters.ladybug_writer import (
                    ladybug_writer_scope,
                )

                with ladybug_writer_scope(
                    scope=candidate.scope_id,
                    phase="initialize_board_route",
                ):
                    handle = kg_runtime.bootstrap_board_graph(candidate.scope_id)
            if not _same_path(handle.path, candidate.binding_path):
                raise _route_failure(
                    "ladybug_initialization_result_mismatch",
                    board_id=candidate.scope_id,
                )
            return None
        if candidate.backend != "grafx" or candidate.page_size is None:
            raise _route_failure(
                "board_initialization_backend_invalid",
                board_id=candidate.scope_id,
            )
        revalidate_board_graph_write_lease(
            candidate.scope_id,
            failure_phase=(
                "rematerialize_board_route_after_purge"
                if close_window_owned
                else "initialize_board_route"
            ),
        )
        return grafx_pool.get(
            candidate.binding_path,
            page_size=candidate.page_size,
        )

    def initialize_physical(candidate: CommunityGraphRouteCandidate) -> object | None:
        return create_board_physical(candidate, close_window_owned=False)

    def rematerialize_physical(
        candidate: CommunityGraphRouteCandidate,
    ) -> object | None:
        return create_board_physical(candidate, close_window_owned=True)

    return CommunityRoutedBoardGraphComposition(
        binding_store=binding_store,
        resolver=resolver,
        grafx_pool=grafx_pool,
        graph_store=graph_store,
        cypher_executor=cypher_executor,
        graph_transaction=graph_transaction,
        graph_schema_manager=graph_schema_manager,
        graph_lifecycle=graph_lifecycle,
        graph_runtime_store=graph_runtime_store,
        graph_recovery=graph_recovery,
        graph_rollout_coordinator=graph_rollout_coordinator,
        _initialize_physical=initialize_physical,
        _rematerialize_physical=rematerialize_physical,
    )


__all__ = [
    "CommunityBoardRouteSessionResolver",
    "CommunityRoutedBoardGraphComposition",
    "build_community_routed_board_graph_composition",
]
