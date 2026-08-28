"""Backend-neutral routing for the Community Global Discovery graph.

The persisted Global binding is the only backend authority.  Every public
operation receives one immutable :class:`CommunityGraphRouteSnapshot` and
keeps the same backend, anchor, generation and Grafx geometry until it is
terminal.  Provider factories receive that snapshot explicitly; they must not
consult current settings or try the other backend.

The injected ``global_lock`` is deliberately shared by the runtime, recovery
and shutdown composition.  It must be a re-entrant lock because Core's
``post_write_verification_scope`` calls back into this runtime while retaining
the complete flush/close/reopen/readback window.  No new ``ContextVar`` is
used: the scoped provider is owned by the current thread while that shared
lock is held.

Grafx providers are supplied as operation sessions.  A session owns the pool
pin for the complete operation and exposes explicit ``*_unguarded`` lifecycle
callbacks, allowing close/reopen to rotate that pin without acquiring another
Global guard or Core writer lease.  Ladybug uses the same seam so its physical
writer/lifecycle guard can remain backend-contained.

Recovery is different from an ordinary operation: its authorized cutover
changes the active-generation pointer.  Its fence therefore accepts either
the latest accepted exact snapshot or a transition authenticated by the
injected validator for the same ``run_id``/``epoch``/``attempt_id``.  A backend
or immutable binding change is never a valid recovery transition.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar

from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryCutoverResult,
    GlobalDiscoveryRecovery,
)
from okto_pulse.core.kg.interfaces.global_discovery_runtime import (
    GlobalDiscoveryRuntime,
)
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
)
from okto_pulse.core.kg.interfaces.graph_lifecycle import GraphHandle
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeObservationState,
    GraphRuntimeState,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef

from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecoveryFenceError,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    CommunityGrafxGlobalDiscoveryFenceError,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)

_GLOBAL_SCOPE_ID = "global"
_GLOBAL_BOARD_ID = "_global"
_ROUTED_SOURCE = "community_global_discovery_routed"
_BINDING_MISSING_REASON = "graph_route_binding_missing"
_ProviderT = TypeVar("_ProviderT")
_RecoveryResultT = TypeVar("_RecoveryResultT")


class GlobalDiscoverySharedLock(Protocol):
    """The one re-entrant lock shared by runtime, recovery and shutdown."""

    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> object: ...


RuntimeStateCallback = Callable[
    [CommunityGraphRouteSnapshot, str | None],
    GraphRuntimeState,
]
MaterializationPathsCallback = Callable[
    [CommunityGraphRouteSnapshot],
    tuple[Path, ...],
]
StandaloneCloseCallback = Callable[[CommunityGraphRouteSnapshot], None]
StandalonePurgeCallback = Callable[
    [CommunityGraphRouteSnapshot, str],
    GraphPurgeResult,
]
StandalonePrivacyEraseCallback = Callable[
    [
        CommunityGraphRouteSnapshot,
        str,
        str,
        tuple[str, ...] | None,
    ],
    dict[str, object],
]


@dataclass(frozen=True, slots=True)
class CommunityGlobalDiscoveryRuntimeOperationSession:
    """One snapshot-pinned physical runtime session.

    A Grafx implementation retains its pool lease until the surrounding
    session factory exits.  The lifecycle callbacks are intentionally named
    ``*_unguarded``: this router already owns the shared Global lock and only
    revalidates (never acquires) the Core writer fence.
    """

    runtime: GlobalDiscoveryRuntime
    post_write_verification_scope_unguarded: Callable[[], AbstractContextManager[None]]
    flush_after_write_batch_unguarded: Callable[[], None]
    close_unguarded: Callable[[], None]
    purge_unguarded: Callable[[str], GraphPurgeResult]
    erase_storage_for_privacy_unguarded: Callable[
        [str, str, tuple[str, ...] | None],
        dict[str, object],
    ]


class GlobalRuntimeSessionFactory(Protocol):
    """Build a provider fixed to exactly the supplied persisted route."""

    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> AbstractContextManager[CommunityGlobalDiscoveryRuntimeOperationSession]: ...


class RecoveryAttemptReconciliation(Protocol):
    """Backend-neutral retention result consumed structurally by the worker."""

    @property
    def quarantined_ids(self) -> tuple[str, ...]: ...

    @property
    def retained_ids(self) -> tuple[str, ...]: ...

    @property
    def deleted_ids(self) -> tuple[str, ...]: ...


class RecoveryWorkerExtensionProvider(GlobalDiscoveryRecovery, Protocol):
    """Recovery leaf including the optional methods required by the worker."""

    def reconcile_attempt_artifacts(
        self,
        *,
        run_id: str,
        known_attempt_ids: tuple[str, ...],
        now: datetime,
        fence_check: Callable[[], None],
    ) -> RecoveryAttemptReconciliation: ...

    def reconcile_attempt_terminal_truth(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        expected_live_sha256: str,
        boards: tuple[GlobalDiscoveryBoardSeed, ...],
        fence_check: Callable[[], None],
    ) -> GlobalDiscoveryCutoverResult | None: ...

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
    ) -> GlobalDiscoveryCutoverResult | None: ...


class RecoveryProviderFactory(Protocol):
    """Build a non-settings recovery provider fixed to one route anchor."""

    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> RecoveryWorkerExtensionProvider: ...


class RecoveryRouteTransitionValidator(Protocol):
    """Authenticate one active-generation transition for this exact attempt."""

    def __call__(
        self,
        *,
        initial: CommunityGraphRouteSnapshot,
        previous: CommunityGraphRouteSnapshot,
        observed: CommunityGraphRouteSnapshot,
        run_id: str,
        epoch: int,
        attempt_id: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class _RuntimeBackend:
    session_factory: GlobalRuntimeSessionFactory
    state: RuntimeStateCallback
    materialization_paths: MaterializationPathsCallback
    close_unguarded: StandaloneCloseCallback
    purge_unguarded: StandalonePurgeCallback
    privacy_erase_unguarded: StandalonePrivacyEraseCallback


@dataclass(slots=True)
class _ActiveVerificationScope:
    owner_thread: int
    snapshot: CommunityGraphRouteSnapshot
    session: CommunityGlobalDiscoveryRuntimeOperationSession
    depth: int = 1


def _invalid_global_snapshot(
    snapshot: CommunityGraphRouteSnapshot,
    *,
    reason: str,
) -> GraphCorruption:
    return GraphCorruption(
        "The routed Community Global Discovery snapshot is inconsistent.",
        details={
            "operation": "route_global_discovery",
            "reason": reason,
            "scope": snapshot.scope,
            "scope_id": snapshot.scope_id,
            "backend": snapshot.backend,
            "generation": snapshot.generation,
        },
    )


def _require_global_snapshot(snapshot: CommunityGraphRouteSnapshot) -> None:
    if snapshot.scope != "global" or snapshot.scope_id != _GLOBAL_SCOPE_ID:
        raise _invalid_global_snapshot(
            snapshot,
            reason="graph_route_snapshot_scope_invalid",
        )
    if snapshot.backend not in {"ladybug", "grafx"}:
        raise _invalid_global_snapshot(
            snapshot,
            reason="graph_route_snapshot_backend_invalid",
        )
    if snapshot.backend == "grafx" and snapshot.page_size is None:
        raise _invalid_global_snapshot(
            snapshot,
            reason="grafx_route_page_size_missing",
        )


def _is_missing_binding(failure: GraphCapabilityUnavailable) -> bool:
    return failure.details.get("reason") == "binding_missing"


def _missing_runtime_state(*, generation: str | None) -> GraphRuntimeState:
    return GraphRuntimeState.from_observation(
        board_id=_GLOBAL_BOARD_ID,
        storage_ref=StorageRef("global-discovery", _ROUTED_SOURCE),
        state=GraphRuntimeObservationState.PROVIDER_UNAVAILABLE,
        generation=generation,
        reason_code=_BINDING_MISSING_REASON,
        observed_at=datetime.now(UTC),
        details={"source": _ROUTED_SOURCE},
    )


def _select_backend(
    snapshot: CommunityGraphRouteSnapshot,
    *,
    ladybug: _ProviderT,
    grafx: _ProviderT,
) -> _ProviderT:
    _require_global_snapshot(snapshot)
    return ladybug if snapshot.backend == "ladybug" else grafx


def _immutable_recovery_binding_matches(
    initial: CommunityGraphRouteSnapshot,
    observed: CommunityGraphRouteSnapshot,
) -> bool:
    """Allow only active-pointer fields to change during one recovery."""

    return (
        observed.scope == initial.scope
        and observed.scope_id == initial.scope_id
        and observed.backend == initial.backend
        and observed.generation == initial.generation
        and observed.binding_path == initial.binding_path
        and observed.anchor_path == initial.anchor_path
        and observed.page_size == initial.page_size
        and observed.binding_sha256 == initial.binding_sha256
    )


class CommunityRoutedGlobalDiscoveryRuntime:
    """Route the complete Core Global Discovery runtime by persisted binding."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        global_lock: GlobalDiscoverySharedLock,
        revalidate_write_fence: Callable[[str], None],
        statement_is_write: Callable[[str], bool],
        ladybug_session_factory: GlobalRuntimeSessionFactory,
        grafx_session_factory: GlobalRuntimeSessionFactory,
        ladybug_state: RuntimeStateCallback,
        grafx_state: RuntimeStateCallback,
        ladybug_materialization_paths: MaterializationPathsCallback,
        grafx_materialization_paths: MaterializationPathsCallback,
        ladybug_close_unguarded: StandaloneCloseCallback,
        grafx_close_unguarded: StandaloneCloseCallback,
        ladybug_purge_unguarded: StandalonePurgeCallback,
        grafx_purge_unguarded: StandalonePurgeCallback,
        ladybug_privacy_erase_unguarded: StandalonePrivacyEraseCallback,
        grafx_privacy_erase_unguarded: StandalonePrivacyEraseCallback,
    ) -> None:
        self._resolver = resolver
        self._global_lock = global_lock
        self._revalidate_write_fence = revalidate_write_fence
        self._statement_is_write = statement_is_write
        self._ladybug = _RuntimeBackend(
            session_factory=ladybug_session_factory,
            state=ladybug_state,
            materialization_paths=ladybug_materialization_paths,
            close_unguarded=ladybug_close_unguarded,
            purge_unguarded=ladybug_purge_unguarded,
            privacy_erase_unguarded=ladybug_privacy_erase_unguarded,
        )
        self._grafx = _RuntimeBackend(
            session_factory=grafx_session_factory,
            state=grafx_state,
            materialization_paths=grafx_materialization_paths,
            close_unguarded=grafx_close_unguarded,
            purge_unguarded=grafx_purge_unguarded,
            privacy_erase_unguarded=grafx_privacy_erase_unguarded,
        )
        self._active_verification: _ActiveVerificationScope | None = None

    def _backend(self, snapshot: CommunityGraphRouteSnapshot) -> _RuntimeBackend:
        return _select_backend(
            snapshot,
            ladybug=self._ladybug,
            grafx=self._grafx,
        )

    def _active_for_current_thread(self) -> _ActiveVerificationScope | None:
        active = self._active_verification
        if active is None:
            return None
        if active.owner_thread != threading.get_ident():
            # The shared RLock prevents a foreign thread from reaching this
            # state.  Seeing it anyway means composition supplied a non-
            # re-entrant/non-exclusive object and must fail closed.
            raise RuntimeError("global_discovery_shared_lock_ownership_invalid")
        return active

    def _acquire_live_snapshot(self) -> CommunityGraphRouteSnapshot:
        active = self._active_for_current_thread()
        if active is not None:
            self._resolver.revalidate_snapshot(
                active.snapshot,
                require_physical=True,
            )
            return active.snapshot
        snapshot = self._resolver.acquire_global_route()
        _require_global_snapshot(snapshot)
        return snapshot

    def _inspect_snapshot(self) -> CommunityGraphRouteSnapshot:
        active = self._active_for_current_thread()
        if active is not None:
            self._resolver.revalidate_snapshot(
                active.snapshot,
                require_physical=False,
            )
            return active.snapshot
        snapshot = self._resolver.inspect_global_route()
        _require_global_snapshot(snapshot)
        return snapshot

    def _revalidate_dispatch(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        phase: str,
        write: bool,
        require_physical: bool,
    ) -> None:
        if write:
            # Revalidation only.  The caller owns the Core global writer lease;
            # this adapter must never open another writer scope.
            self._revalidate_write_fence(phase)
        self._resolver.revalidate_snapshot(
            snapshot,
            require_physical=require_physical,
        )

    def _call_runtime(
        self,
        method_name: str,
        *args: object,
        phase: str,
        write: bool,
        **kwargs: object,
    ) -> Any:
        with self._global_lock:
            snapshot = self._acquire_live_snapshot()
            active = self._active_for_current_thread()
            if active is not None:
                self._revalidate_dispatch(
                    snapshot,
                    phase=phase,
                    write=write,
                    require_physical=True,
                )
                method = getattr(active.session.runtime, method_name)
                return method(*args, **kwargs)

            backend = self._backend(snapshot)
            with backend.session_factory(snapshot) as session:
                self._revalidate_dispatch(
                    snapshot,
                    phase=phase,
                    write=write,
                    require_physical=True,
                )
                method = getattr(session.runtime, method_name)
                return method(*args, **kwargs)

    def state(self, *, generation: str | None = None) -> GraphRuntimeState:
        """Inspect the persisted binding and physical metadata without opening."""

        with self._global_lock:
            try:
                snapshot = self._inspect_snapshot()
            except GraphCapabilityUnavailable as failure:
                if _is_missing_binding(failure):
                    return _missing_runtime_state(generation=generation)
                raise
            # State callbacks are deliberately separate from operation-session
            # factories.  A Grafx pool acquisition here would turn health into
            # an implicit open/bootstrap path.
            self._resolver.revalidate_snapshot(snapshot, require_physical=False)
            return self._backend(snapshot).state(snapshot, generation)

    def materialization_observation_paths(self) -> tuple[Path, ...]:
        """Preserve Community health's metadata-only observation extension."""

        with self._global_lock:
            try:
                snapshot = self._inspect_snapshot()
            except GraphCapabilityUnavailable as failure:
                if _is_missing_binding(failure):
                    return ()
                raise
            self._resolver.revalidate_snapshot(snapshot, require_physical=False)
            return self._backend(snapshot).materialization_paths(snapshot)

    def bootstrap(self) -> GraphHandle:
        """Bootstrap one bound route, rematerializing its exact target if absent.

        Purge deliberately preserves the immutable backend binding while it
        quarantines the selected physical layout.  Bootstrap is the explicit
        lifecycle door that makes that already-bound target usable again; it
        must therefore inspect (not acquire) the route before the physical
        provider creates the same path.  Ordinary reads and writes still use
        ``_acquire_live_snapshot`` and remain fail-closed while it is absent.
        """

        with self._global_lock:
            snapshot = self._inspect_snapshot()
            active = self._active_for_current_thread()
            self._revalidate_dispatch(
                snapshot,
                phase="global_bootstrap",
                write=True,
                require_physical=False,
            )
            if active is not None:
                handle = active.session.runtime.bootstrap()
            else:
                with self._backend(snapshot).session_factory(snapshot) as session:
                    handle = session.runtime.bootstrap()
            # Publication is terminal only when the exact bound target now
            # exists and no binding/active-pointer cutover occurred meanwhile.
            self._revalidate_dispatch(
                snapshot,
                phase="global_bootstrap",
                write=True,
                require_physical=True,
            )
            return handle

    def ensure_layer_schema(self) -> tuple[str, ...]:
        return self._call_runtime(
            "ensure_layer_schema",
            phase="ensure_layer_schema",
            write=True,
        )

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        write = self._statement_is_write(statement)
        return self._call_runtime(
            "execute",
            statement,
            params,
            phase=("global_statement_write" if write else "global_statement_read"),
            write=write,
        )

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
        # Ladybug's connection-local LOAD VECTOR writes its WAL even though the
        # semantic operation is a read.  Route both backends through the writer
        # lane so the public contract cannot vary with the selected engine.
        return self._call_runtime(
            "search_decision_digests",
            query_vector,
            board_ids=board_ids,
            graph_layer=graph_layer,
            top_k=top_k,
            min_similarity=min_similarity,
            exhaustive=exhaustive,
            phase="global_digest_search",
            write=True,
        )

    def list_schema_objects(self) -> tuple[str, ...]:
        return self._call_runtime(
            "list_schema_objects",
            phase="global_schema_objects",
            write=False,
        )

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
        self._call_runtime(
            "upsert_board_summary",
            board_id=board_id,
            name=name,
            summary=summary,
            summary_embedding=summary_embedding,
            decision_count=decision_count,
            synced_at=synced_at,
            phase="upsert_board_summary",
            write=True,
        )

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
        return self._call_runtime(
            "upsert_decision_digest",
            digest_id=digest_id,
            board_id=board_id,
            original_node_id=original_node_id,
            title=title,
            summary=summary,
            node_type=node_type,
            graph_layer=graph_layer,
            embedding=embedding,
            created_at=created_at,
            phase="upsert_decision_digest",
            write=True,
        )

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
        return self._call_runtime(
            "replace_decision_digest_identity",
            digest_id=digest_id,
            board_id=board_id,
            original_node_id=original_node_id,
            title=title,
            summary=summary,
            node_type=node_type,
            graph_layer=graph_layer,
            embedding=embedding,
            created_at=created_at,
            phase="replace_decision_digest_identity",
            write=True,
        )

    def delete_decision_digests_guarded(
        self,
        *,
        board_id: str,
        original_node_ids: tuple[str, ...],
        include_malformed: bool = False,
    ) -> int:
        return self._call_runtime(
            "delete_decision_digests_guarded",
            board_id=board_id,
            original_node_ids=original_node_ids,
            include_malformed=include_malformed,
            phase="delete_decision_digests_guarded",
            write=True,
        )

    def delete_decision_digests_for_absent_sources(
        self,
        *,
        board_id: str,
        original_node_ids: tuple[str, ...],
        include_malformed: bool = False,
    ) -> int:
        return self._call_runtime(
            "delete_decision_digests_for_absent_sources",
            board_id=board_id,
            original_node_ids=original_node_ids,
            include_malformed=include_malformed,
            phase="delete_decision_digests_for_absent_sources",
            write=True,
        )

    def link_board_digest(self, *, board_id: str, digest_id: str) -> None:
        self._call_runtime(
            "link_board_digest",
            board_id=board_id,
            digest_id=digest_id,
            phase="link_board_digest",
            write=True,
        )

    def normalize_board_digest_link(
        self,
        *,
        board_id: str,
        digest_id: str,
    ) -> int:
        return self._call_runtime(
            "normalize_board_digest_link",
            board_id=board_id,
            digest_id=digest_id,
            phase="normalize_board_digest_link",
            write=True,
        )

    def delete_invalid_board_digest_links(
        self,
        *,
        board_id: str,
        expected_digest_ids: tuple[str, ...],
    ) -> int:
        return self._call_runtime(
            "delete_invalid_board_digest_links",
            board_id=board_id,
            expected_digest_ids=expected_digest_ids,
            phase="delete_invalid_board_digest_links",
            write=True,
        )

    @contextmanager
    def post_write_verification_scope(self) -> Iterator[None]:
        """Pin one route/provider across flush, close/reopen and fresh reads."""

        with self._global_lock:
            active = self._active_for_current_thread()
            if active is not None:
                self._revalidate_dispatch(
                    active.snapshot,
                    phase="post_write_verification",
                    write=True,
                    require_physical=True,
                )
                active.depth += 1
                try:
                    yield
                finally:
                    active.depth -= 1
                return

            snapshot = self._acquire_live_snapshot()
            backend = self._backend(snapshot)
            with backend.session_factory(snapshot) as session:
                self._revalidate_dispatch(
                    snapshot,
                    phase="post_write_verification",
                    write=True,
                    require_physical=True,
                )
                with session.post_write_verification_scope_unguarded():
                    scoped = _ActiveVerificationScope(
                        owner_thread=threading.get_ident(),
                        snapshot=snapshot,
                        session=session,
                    )
                    self._active_verification = scoped
                    try:
                        yield
                    finally:
                        self._active_verification = None

    def flush_after_write_batch(self) -> None:
        with self._global_lock:
            snapshot = self._acquire_live_snapshot()
            active = self._active_for_current_thread()
            if active is not None:
                self._revalidate_dispatch(
                    snapshot,
                    phase="flush_after_write_batch",
                    write=True,
                    require_physical=True,
                )
                active.session.flush_after_write_batch_unguarded()
                return
            with self._backend(snapshot).session_factory(snapshot) as session:
                self._revalidate_dispatch(
                    snapshot,
                    phase="flush_after_write_batch",
                    write=True,
                    require_physical=True,
                )
                session.flush_after_write_batch_unguarded()

    def close(self) -> None:
        with self._global_lock:
            snapshot = self._inspect_snapshot()
            active = self._active_for_current_thread()
            self._revalidate_dispatch(
                snapshot,
                phase="close_global_discovery",
                write=True,
                require_physical=False,
            )
            if active is not None:
                active.session.close_unguarded()
                return
            self._backend(snapshot).close_unguarded(snapshot)

    def purge(self, *, reason: str = "manual") -> GraphPurgeResult:
        with self._global_lock:
            snapshot = self._inspect_snapshot()
            active = self._active_for_current_thread()
            self._revalidate_dispatch(
                snapshot,
                phase="purge_global_discovery",
                write=True,
                require_physical=False,
            )
            if active is not None:
                return active.session.purge_unguarded(reason)
            return self._backend(snapshot).purge_unguarded(snapshot, reason)

    def erase_storage_for_privacy(
        self,
        *,
        board_id: str,
        reason: str,
        survivor_board_ids: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        with self._global_lock:
            snapshot = self._inspect_snapshot()
            active = self._active_for_current_thread()
            self._revalidate_dispatch(
                snapshot,
                phase="privacy_erase_global_discovery",
                write=True,
                require_physical=False,
            )
            if active is not None:
                return active.session.erase_storage_for_privacy_unguarded(
                    board_id,
                    reason,
                    survivor_board_ids,
                )
            return self._backend(snapshot).privacy_erase_unguarded(
                snapshot,
                board_id,
                reason,
                survivor_board_ids,
            )


class CommunityRoutedGlobalDiscoveryRecovery:
    """Route Global recovery while retaining one authenticated attempt route."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        global_lock: GlobalDiscoverySharedLock,
        ladybug_factory: RecoveryProviderFactory,
        grafx_factory: RecoveryProviderFactory,
        validate_authenticated_transition: RecoveryRouteTransitionValidator,
    ) -> None:
        self._resolver = resolver
        self._global_lock = global_lock
        self._ladybug_factory = ladybug_factory
        self._grafx_factory = grafx_factory
        self._validate_authenticated_transition = validate_authenticated_transition

    def _snapshot(self) -> CommunityGraphRouteSnapshot:
        snapshot = self._resolver.inspect_global_route()
        _require_global_snapshot(snapshot)
        return snapshot

    def _provider(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> RecoveryWorkerExtensionProvider:
        factory = _select_backend(
            snapshot,
            ladybug=self._ladybug_factory,
            grafx=self._grafx_factory,
        )
        return factory(snapshot)

    def inspect_live_artifact(self) -> GlobalDiscoveryArtifactSnapshot:
        with self._global_lock:
            snapshot = self._snapshot()
            self._resolver.revalidate_snapshot(snapshot, require_physical=False)
            return self._provider(snapshot).inspect_live_artifact()

    def current_snapshot_fingerprint(self) -> str:
        with self._global_lock:
            snapshot = self._snapshot()
            self._resolver.revalidate_snapshot(snapshot, require_physical=False)
            return self._provider(snapshot).current_snapshot_fingerprint()

    def _run_recovery_operation(
        self,
        *,
        run_id: str,
        epoch: int | None,
        attempt_id: str | None,
        fence_check: Callable[[], None],
        allow_authenticated_transition: bool,
        invoke: Callable[
            [RecoveryWorkerExtensionProvider, Callable[[], None]],
            _RecoveryResultT,
        ],
    ) -> _RecoveryResultT:
        with self._global_lock:
            initial = self._snapshot()
            accepted = initial
            provider = self._provider(initial)

            def failure_details(reason: str) -> dict[str, object]:
                details: dict[str, object] = {
                    "operation": "route_global_discovery_recovery",
                    "reason": reason,
                    "run_id": run_id,
                }
                if epoch is not None:
                    details["epoch"] = epoch
                if attempt_id is not None:
                    details["attempt_id"] = attempt_id
                return details

            def routed_fence() -> None:
                nonlocal accepted
                # The original callback is the Core-owned writer authority.
                # Calling it is revalidation, never acquisition.
                fence_check()
                observed = self._snapshot()
                if observed == accepted:
                    return
                if not _immutable_recovery_binding_matches(initial, observed):
                    raise GraphCapabilityUnavailable(
                        "The Global Discovery recovery binding changed.",
                        details=failure_details("recovery_binding_changed"),
                    )
                if not allow_authenticated_transition:
                    raise GraphCapabilityUnavailable(
                        "The Global Discovery recovery route changed during an "
                        "exact-route operation.",
                        details=failure_details(
                            "recovery_route_transition_not_allowed"
                        ),
                    )
                if epoch is None or attempt_id is None:
                    raise AssertionError(
                        "authenticated recovery transition requires attempt identity"
                    )
                authenticated = self._validate_authenticated_transition(
                    initial=initial,
                    previous=accepted,
                    observed=observed,
                    run_id=run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                )
                if not authenticated:
                    raise GraphCapabilityUnavailable(
                        "The Global Discovery recovery route transition was refused.",
                        details=failure_details(
                            "recovery_route_transition_unauthenticated"
                        ),
                    )
                accepted = observed

            # Refuse a stale writer or route before the physical provider sees
            # the operation, then give it the same combined check for every
            # mutation and terminal publication phase.
            routed_fence()
            try:
                return invoke(provider, routed_fence)
            except CommunityGrafxGlobalDiscoveryFenceError as failure:
                # The existing worker consumes the Community fence envelope;
                # keep that stable while hiding the selected engine.
                raise CommunityGlobalDiscoveryRecoveryFenceError(
                    failure.original
                ) from failure

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
        return self._run_recovery_operation(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            fence_check=fence_check,
            allow_authenticated_transition=True,
            invoke=lambda provider, routed_fence: (
                provider.rebuild_candidate_and_cutover(
                    run_id=run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                    expected_live_sha256=expected_live_sha256,
                    boards=boards,
                    fence_check=routed_fence,
                )
            ),
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
        return self._run_recovery_operation(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            fence_check=fence_check,
            allow_authenticated_transition=True,
            invoke=lambda provider, routed_fence: provider.recover_and_cutover(
                run_id=run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=expected_live_sha256,
                boards=boards,
                fence_check=routed_fence,
            ),
        )

    def reconcile_attempt_artifacts(
        self,
        *,
        run_id: str,
        known_attempt_ids: tuple[str, ...],
        now: datetime,
        fence_check: Callable[[], None],
    ) -> RecoveryAttemptReconciliation:
        return self._run_recovery_operation(
            run_id=run_id,
            epoch=None,
            attempt_id=None,
            fence_check=fence_check,
            allow_authenticated_transition=False,
            invoke=lambda provider, routed_fence: provider.reconcile_attempt_artifacts(
                run_id=run_id,
                known_attempt_ids=known_attempt_ids,
                now=now,
                fence_check=routed_fence,
            ),
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
        return self._run_recovery_operation(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            fence_check=fence_check,
            allow_authenticated_transition=False,
            invoke=lambda provider, routed_fence: (
                provider.reconcile_attempt_terminal_truth(
                    run_id=run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                    expected_live_sha256=expected_live_sha256,
                    boards=boards,
                    fence_check=routed_fence,
                )
            ),
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
        return self._run_recovery_operation(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            fence_check=fence_check,
            allow_authenticated_transition=False,
            invoke=lambda provider, routed_fence: (
                provider.reconcile_predecessor_and_complete(
                    run_id=run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                    ancestry=ancestry,
                    expected_live_sha256=expected_live_sha256,
                    boards=boards,
                    fence_check=routed_fence,
                )
            ),
        )


__all__ = [
    "CommunityGlobalDiscoveryRuntimeOperationSession",
    "CommunityRoutedGlobalDiscoveryRecovery",
    "CommunityRoutedGlobalDiscoveryRuntime",
    "GlobalDiscoverySharedLock",
    "GlobalRuntimeSessionFactory",
    "RecoveryProviderFactory",
    "RecoveryRouteTransitionValidator",
]
