"""Backend-neutral Board ``GraphTransaction`` routing.

The persisted Board binding is the only routing authority.  One lifecycle
operation window is entered before that binding is acquired and remains held
until the selected engine scope is terminal.  Grafx additionally keeps a pool
lease from before ``Database.begin`` until after the engine transaction ends;
the terminal order is therefore engine, pool lease, operation window.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from typing import Any, Protocol, Self

from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption
from okto_pulse.core.kg.interfaces.graph_transaction import (
    GraphTransaction,
    GraphTransactionScope,
)
from okto_pulse.core.services.application_kg import (
    revalidate_board_graph_write_lease,
)

from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
    GrafxDatabaseLease,
)
from okto_pulse.community.adapters.grafx_graph_transaction import (
    CommunityGrafxGraphTransaction,
)
from okto_pulse.community.adapters.graph_rollout_capture import (
    BoardRolloutMutationRecorder,
    CapturedGraphTransactionScope,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)
from okto_pulse.community.adapters.kg_runtime import board_graph_operation_window

logger = logging.getLogger(__name__)


class BoardGraphOperationWindowFactory(Protocol):
    """Open the reader side of the Board storage/lifecycle guard."""

    def __call__(self, board_id: str) -> AbstractContextManager[None]: ...


class _OperationWindowLease:
    """An entered operation window that can be released exactly once."""

    __slots__ = ("_closed", "_manager")

    def __init__(self, manager: AbstractContextManager[None]) -> None:
        self._manager = manager
        self._closed = False

    @classmethod
    def enter(
        cls,
        factory: BoardGraphOperationWindowFactory,
        board_id: str,
    ) -> _OperationWindowLease:
        manager = factory(board_id)
        manager.__enter__()
        return cls(manager)

    @property
    def closed(self) -> bool:
        return self._closed

    def release(self) -> bool:
        """Exit once, marking ownership consumed before invoking user code."""

        if self._closed:
            return False
        self._closed = True
        self._manager.__exit__(None, None, None)
        return True


def _release_during_failure(
    release: Any,
    primary: BaseException,
    *,
    resource: str,
) -> None:
    """Release a begin-owned resource without replacing the primary failure."""

    try:
        release()
    except BaseException as cleanup:  # noqa: BLE001 - preserve cancellation/primary
        primary.add_note(
            f"releasing the routed graph {resource} also failed: "
            f"{type(cleanup).__name__}: {cleanup}"
        )


def _invalid_snapshot(
    snapshot: CommunityGraphRouteSnapshot,
    *,
    board_id: str,
    reason: str,
) -> GraphCorruption:
    return GraphCorruption(
        "The routed Community graph transaction snapshot is inconsistent.",
        details={
            "operation": "begin_routed_graph_transaction",
            "reason": reason,
            "board_id": board_id,
            "scope": snapshot.scope,
            "scope_id": snapshot.scope_id,
            "backend": snapshot.backend,
            "generation": snapshot.generation,
        },
    )


def _require_board_snapshot(
    snapshot: CommunityGraphRouteSnapshot,
    *,
    board_id: str,
) -> None:
    if snapshot.scope != "board" or snapshot.scope_id != board_id:
        raise _invalid_snapshot(
            snapshot,
            board_id=board_id,
            reason="graph_route_snapshot_scope_invalid",
        )
    if snapshot.backend not in {"ladybug", "grafx"}:
        raise _invalid_snapshot(
            snapshot,
            board_id=board_id,
            reason="graph_route_snapshot_backend_invalid",
        )
    if snapshot.backend == "grafx" and snapshot.page_size is None:
        raise _invalid_snapshot(
            snapshot,
            board_id=board_id,
            reason="grafx_route_page_size_missing",
        )


class _WindowedGraphTransactionScope:
    """Delegate one Ladybug scope while retaining the outer operation window."""

    __slots__ = ("_delegate", "_terminal", "_window", "terminal_release_error")

    def __init__(
        self,
        delegate: GraphTransactionScope,
        window: _OperationWindowLease,
    ) -> None:
        self._delegate = delegate
        self._window = window
        self._terminal = False
        # Ladybug statements auto-commit.  A failure after its scope has closed
        # must not be reported as a retryable transaction failure, so a later
        # operation-window release fault is retained as resource evidence.
        self.terminal_release_error: BaseException | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def _release_after_commit(self) -> None:
        try:
            self._window.release()
        except BaseException as failure:  # noqa: BLE001 - commit already terminal
            self.terminal_release_error = failure
            logger.warning(
                "kg.routed_graph_transaction.release_failed "
                "backend=ladybug phase=commit commit_durable=true error_type=%s",
                type(failure).__name__,
                extra={
                    "event": "kg.routed_graph_transaction.release_failed",
                    "backend": "ladybug",
                    "phase": "commit",
                    "commit_durable": True,
                    "error_type": type(failure).__name__,
                },
            )

    async def commit(self) -> None:
        if self._terminal:
            return
        await self._delegate.commit()
        self._terminal = True
        self._release_after_commit()

    async def rollback(self) -> None:
        if self._terminal:
            return
        await self._delegate.rollback()
        self._terminal = True
        self._window.release()

    async def __aenter__(self) -> Self:
        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if exc and exc[0] is not None:
            await self.rollback()
        else:
            await self.commit()


class _GrafxTerminalResources:
    """Release a Grafx pin and then its Board operation window."""

    __slots__ = ("_lease", "_pin_released", "_window")

    def __init__(
        self,
        lease: GrafxDatabaseLease,
        window: _OperationWindowLease,
    ) -> None:
        self._lease = lease
        self._window = window
        self._pin_released = False

    def release(self) -> None:
        # Component flags deliberately change only after their component has
        # accepted the release.  The Grafx scope invokes this callback once;
        # retaining the later component on a prior failure is fail-closed.
        if not self._pin_released:
            self._lease.release()
            self._pin_released = True
        self._window.release()


class CommunityRoutedGraphTransaction:
    """Route ``GraphTransaction.begin`` by one immutable Board binding."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        ladybug: GraphTransaction,
        grafx_pool: CommunityGrafxDatabasePool,
        operation_window: BoardGraphOperationWindowFactory = (
            board_graph_operation_window
        ),
        mutation_recorder: BoardRolloutMutationRecorder | None = None,
    ) -> None:
        self._resolver = resolver
        self._ladybug = ladybug
        self._grafx_pool = grafx_pool
        self._operation_window = operation_window
        self._mutation_recorder = mutation_recorder

    def _capture(
        self,
        scope: GraphTransactionScope,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> GraphTransactionScope:
        recorder = self._mutation_recorder
        if recorder is None:
            return scope
        return CapturedGraphTransactionScope(
            scope,
            recorder=recorder,
            board_id=snapshot.scope_id,
            backend=snapshot.backend,
            binding_sha256=snapshot.binding_sha256,
        )

    async def begin(self, board_id: str) -> GraphTransactionScope:
        if type(board_id) is not str or not board_id:
            raise ValueError("board_id must be non-empty text")

        window = _OperationWindowLease.enter(self._operation_window, board_id)
        lease: GrafxDatabaseLease | None = None
        grafx_owns_terminal_resources = False
        try:
            # This is the sole route acquisition.  Persisted binding state is
            # authoritative; neither settings nor the other provider are ever
            # consulted as a fallback.
            snapshot = self._resolver.acquire_board_route(board_id)
            _require_board_snapshot(snapshot, board_id=board_id)

            if snapshot.backend == "ladybug":
                scope = await self._ladybug.begin(board_id)
                return self._capture(
                    _WindowedGraphTransactionScope(scope, window),
                    snapshot,
                )

            # The route validator above proves this for type checkers and for
            # runtime safety before the pool sees persisted geometry.
            assert snapshot.page_size is not None
            lease = self._grafx_pool.acquire(
                snapshot.active_path,
                page_size=snapshot.page_size,
            )
            self._resolver.admit_grafx_route(
                snapshot,
                lease.database,
                operation="begin_routed_graph_transaction",
            )
            terminal = _GrafxTerminalResources(lease, window)

            def revalidate_fence(expected_board_id: str, phase: str) -> None:
                if expected_board_id != board_id:
                    raise _invalid_snapshot(
                        snapshot,
                        board_id=board_id,
                        reason="graph_transaction_board_mismatch",
                    )
                revalidate_board_graph_write_lease(
                    board_id,
                    failure_phase=phase,
                )
                # Keep the fence at every existing statement boundary.  This door still
                # authenticates the binding and physical database, but transfers that proof
                # to the already-pinned lease instead of walking the same route twice.
                self._resolver.revalidate_pinned_grafx_board_snapshot(
                    snapshot,
                    lease.database,
                )

            def resolve_database(expected_board_id: str):
                nonlocal grafx_owns_terminal_resources
                if expected_board_id != board_id:
                    raise _invalid_snapshot(
                        snapshot,
                        board_id=board_id,
                        reason="graph_transaction_board_mismatch",
                    )
                # Ownership transfers exactly when the Grafx provider receives
                # both the already-pinned handle and its terminal callback.
                grafx_owns_terminal_resources = True
                return lease.database, terminal.release

            provider = CommunityGrafxGraphTransaction(
                database_resolver=resolve_database,
                revalidate_fence=revalidate_fence,
            )
            return self._capture(await provider.begin(board_id), snapshot)
        except BaseException as failure:
            if lease is None:
                _release_during_failure(
                    window.release,
                    failure,
                    resource="operation window",
                )
            elif not grafx_owns_terminal_resources:
                # Admission or the pre-resolver fence failed.  No engine scope
                # can still use the handle, so this begin path owns both exits.
                _release_during_failure(
                    lease.release,
                    failure,
                    resource="Grafx pool lease",
                )
                _release_during_failure(
                    window.release,
                    failure,
                    resource="operation window",
                )
            # Once transferred, CommunityGrafxGraphTransaction releases only
            # after it observes an inactive engine.  If rollback failed and the
            # engine remains active, deliberately retain both pin and window.
            raise


__all__ = ["CommunityRoutedGraphTransaction"]
