"""Backend-neutral Board ``GraphLifecycle`` routing.

The persisted binding is the only routing authority.  Every Board call enters
one lifecycle window, resolves one immutable route, and gives that exact
snapshot to a backend-contained callback.  The callbacks are deliberately
named ``*_unguarded``: they may touch physical handles, but they must not
acquire a Board operation window or writer fence themselves.

Ordinary opens use the shared operation window.  Close/rebuild/purge and the
destructive close/reopen durability probe use an injected exclusive lifecycle
window which also **must not** acquire the Core writer fence.  The public
router revalidates the already-owned fence and the exact physical snapshot
immediately before dispatch, avoiding both nested writer acquisition and a
route change between validation and mutation.

``close(None)`` is the protocol's process-wide Board cleanup operation.  It
does not route Global Discovery and does not guess which boards exist: both
backend Board-pool cleanup callbacks are started exactly once.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, TypeVar

from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption
from okto_pulse.core.kg.interfaces.graph_lifecycle import (
    GraphHandle,
    GraphLifecycleStepResult,
    PurgeReport,
    RebuildReport,
)
from okto_pulse.core.kg.safe_write_lifecycle import (
    STEP_CHECKPOINT,
    STEP_CLOSE_REOPEN_PROBE,
    STEP_FLUSH,
    STEP_FSYNC,
)

from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)

_AsyncResultT = TypeVar("_AsyncResultT")
_PhysicalAsyncResult = _AsyncResultT | Awaitable[_AsyncResultT]

_OpenCallback = Callable[
    [CommunityGraphRouteSnapshot],
    _PhysicalAsyncResult[GraphHandle],
]
_CloseCallback = Callable[
    [CommunityGraphRouteSnapshot],
    _PhysicalAsyncResult[None],
]
_RebuildCallback = Callable[
    [CommunityGraphRouteSnapshot],
    _PhysicalAsyncResult[RebuildReport],
]
_PurgeCallback = Callable[
    [CommunityGraphRouteSnapshot, str],
    _PhysicalAsyncResult[PurgeReport],
]
_StepCallback = Callable[
    [CommunityGraphRouteSnapshot, str, str],
    GraphLifecycleStepResult,
]
_CloseAllCallback = Callable[[], _PhysicalAsyncResult[None]]

_KNOWN_STEPS = frozenset(
    {
        STEP_CHECKPOINT,
        STEP_FLUSH,
        STEP_FSYNC,
        STEP_CLOSE_REOPEN_PROBE,
    }
)


class BoardGraphOperationWindowFactory(Protocol):
    """Enter the reader side of the Board lifecycle guard."""

    def __call__(self, board_id: str) -> AbstractContextManager[None]: ...


class BoardGraphLifecycleMutationWindowUnguardedFactory(Protocol):
    """Enter an exclusive Board lifecycle window without acquiring writer.

    The public routed lifecycle is the only allowed caller.  Its caller must
    already own the Core writer lease; the router revalidates that lease after
    the window is entered and before any physical callback is dispatched.
    """

    def __call__(
        self,
        board_id: str,
        *,
        phase: str,
    ) -> AbstractContextManager[None]: ...


class BoardGraphWriteFenceRevalidator(Protocol):
    """Fail closed unless the current caller still owns the Board writer."""

    def __call__(self, board_id: str, phase: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _PhysicalLifecycleCallbacks:
    """Backend-contained primitives; intentionally not a public Core port."""

    open_unguarded: _OpenCallback
    close_unguarded: _CloseCallback
    rebuild_unguarded: _RebuildCallback
    purge_unguarded: _PurgeCallback
    apply_step_unguarded: _StepCallback
    close_all_unguarded: _CloseAllCallback


def _invalid_snapshot(
    snapshot: CommunityGraphRouteSnapshot,
    *,
    board_id: str,
    reason: str,
) -> GraphCorruption:
    return GraphCorruption(
        "The routed Community graph lifecycle snapshot is inconsistent.",
        details={
            "operation": "route_board_graph_lifecycle",
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


def _require_board_id(board_id: object) -> str:
    if type(board_id) is not str or not board_id:
        raise ValueError("board_id must be non-empty text")
    return board_id


async def _await_physical(result: _PhysicalAsyncResult[_AsyncResultT]) -> _AsyncResultT:
    if inspect.isawaitable(result):
        return await result
    return result


async def _invoke_physical(
    callback: Callable[[], _PhysicalAsyncResult[_AsyncResultT]],
) -> _AsyncResultT:
    """Invoke inside a task so a synchronous failure is isolated too."""

    return await _await_physical(callback())


class CommunityRoutedGraphLifecycle:
    """Route the complete Board ``GraphLifecycle`` port by persisted binding.

    Physical callbacks are required individually so a concrete guarded
    provider cannot accidentally be registered as this public port.  The only
    methods exposed to Core are the routed methods on this class.
    """

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        operation_window: BoardGraphOperationWindowFactory,
        mutation_window_unguarded: BoardGraphLifecycleMutationWindowUnguardedFactory,
        revalidate_write_fence: BoardGraphWriteFenceRevalidator,
        ladybug_open_unguarded: _OpenCallback,
        ladybug_close_unguarded: _CloseCallback,
        ladybug_rebuild_unguarded: _RebuildCallback,
        ladybug_purge_unguarded: _PurgeCallback,
        ladybug_apply_step_unguarded: _StepCallback,
        ladybug_close_all_unguarded: _CloseAllCallback,
        grafx_open_unguarded: _OpenCallback,
        grafx_close_unguarded: _CloseCallback,
        grafx_rebuild_unguarded: _RebuildCallback,
        grafx_purge_unguarded: _PurgeCallback,
        grafx_apply_step_unguarded: _StepCallback,
        grafx_close_all_unguarded: _CloseAllCallback,
    ) -> None:
        self._resolver = resolver
        self._operation_window = operation_window
        self._mutation_window_unguarded = mutation_window_unguarded
        self._revalidate_write_fence = revalidate_write_fence
        self._ladybug = _PhysicalLifecycleCallbacks(
            open_unguarded=ladybug_open_unguarded,
            close_unguarded=ladybug_close_unguarded,
            rebuild_unguarded=ladybug_rebuild_unguarded,
            purge_unguarded=ladybug_purge_unguarded,
            apply_step_unguarded=ladybug_apply_step_unguarded,
            close_all_unguarded=ladybug_close_all_unguarded,
        )
        self._grafx = _PhysicalLifecycleCallbacks(
            open_unguarded=grafx_open_unguarded,
            close_unguarded=grafx_close_unguarded,
            rebuild_unguarded=grafx_rebuild_unguarded,
            purge_unguarded=grafx_purge_unguarded,
            apply_step_unguarded=grafx_apply_step_unguarded,
            close_all_unguarded=grafx_close_all_unguarded,
        )

    def _acquire(
        self,
        board_id: str,
    ) -> tuple[CommunityGraphRouteSnapshot, _PhysicalLifecycleCallbacks]:
        # Sole route resolution for one operation.  Revalidation below checks
        # this same snapshot and is never allowed to select a replacement.
        snapshot = self._resolver.acquire_board_route(board_id)
        _require_board_snapshot(snapshot, board_id=board_id)
        callbacks = self._ladybug if snapshot.backend == "ladybug" else self._grafx
        return snapshot, callbacks

    def _revalidate_mutation(
        self,
        board_id: str,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        phase: str,
    ) -> None:
        # Both checks happen after entering the lifecycle window.  The first
        # refuses a lost/foreign Core write lease; the second refuses a
        # binding, generation, physical target, or geometry change.
        self._revalidate_write_fence(board_id, phase)
        self._resolver.revalidate_snapshot(snapshot, require_physical=True)

    async def open(self, board_id: str) -> GraphHandle:
        """Open an already initialized, physically bound Board route.

        Route creation is intentionally not hidden here.  First boot must call
        the explicit resolver initialization seam before schema/lifecycle
        dispatch; otherwise ``acquire_board_route`` fails closed.
        """

        board_id = _require_board_id(board_id)
        with self._operation_window(board_id):
            snapshot, callbacks = self._acquire(board_id)
            # Open callbacks are non-creating physical opens.  Requiring the
            # exact live target prevents a missing database from becoming an
            # implicit empty replacement.
            self._resolver.revalidate_snapshot(snapshot, require_physical=True)
            return await _await_physical(callbacks.open_unguarded(snapshot))

    async def close(self, board_id: str | None = None) -> None:
        if board_id is None:
            await self._close_all_board_backends()
            return
        board_id = _require_board_id(board_id)
        phase = "graph_lifecycle_close"
        with self._mutation_window_unguarded(board_id, phase=phase):
            snapshot, callbacks = self._acquire(board_id)
            self._revalidate_mutation(board_id, snapshot, phase=phase)
            await _await_physical(callbacks.close_unguarded(snapshot))

    async def _close_all_board_backends(self) -> None:
        """Start both Board-pool cleanups once and report every failure.

        There is no Board id to route in ``close(None)``.  Starting both tasks
        before waiting means one backend failure cannot prevent the other
        backend from receiving its cleanup call.  These callbacks are Board
        only; Global Discovery remains outside this adapter.
        """

        tasks = (
            asyncio.create_task(_invoke_physical(self._ladybug.close_all_unguarded)),
            asyncio.create_task(_invoke_physical(self._grafx.close_all_unguarded)),
        )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if not failures:
            return
        primary = failures[0]
        for secondary in failures[1:]:
            primary.add_note(
                "closing the other routed Board backend also failed: "
                f"{type(secondary).__name__}: {secondary}"
            )
        raise primary

    async def rebuild(self, board_id: str) -> RebuildReport:
        board_id = _require_board_id(board_id)
        phase = "graph_lifecycle_rebuild"
        with self._mutation_window_unguarded(board_id, phase=phase):
            snapshot, callbacks = self._acquire(board_id)
            self._revalidate_mutation(board_id, snapshot, phase=phase)
            return await _await_physical(callbacks.rebuild_unguarded(snapshot))

    async def purge(self, board_id: str, *, reason: str) -> PurgeReport:
        board_id = _require_board_id(board_id)
        phase = "graph_lifecycle_purge"
        with self._mutation_window_unguarded(board_id, phase=phase):
            snapshot, callbacks = self._acquire(board_id)
            self._revalidate_mutation(board_id, snapshot, phase=phase)
            return await _await_physical(
                callbacks.purge_unguarded(snapshot, reason=reason)
            )

    def apply_step(
        self,
        board_id: str,
        graph_type: str,
        step: str,
    ) -> GraphLifecycleStepResult:
        if graph_type != "board_graph":
            return GraphLifecycleStepResult(
                ok=False,
                detail=f"unsupported_graph_type={graph_type}",
            )
        if step not in _KNOWN_STEPS:
            return GraphLifecycleStepResult(ok=False, detail=f"unknown_step={step}")
        board_id = _require_board_id(board_id)
        phase = f"graph_lifecycle_{step}"

        # Ladybug CHECKPOINT is unsafe beside live readers, while the
        # close/reopen probe releases handles.  Both therefore use the
        # exclusive lifecycle window.  Flush/fsync retain the shared operation
        # window.  No physical callback may acquire another guard internally.
        window = (
            self._mutation_window_unguarded(board_id, phase=phase)
            if step in {STEP_CHECKPOINT, STEP_CLOSE_REOPEN_PROBE}
            else self._operation_window(board_id)
        )
        with window:
            snapshot, callbacks = self._acquire(board_id)
            self._revalidate_mutation(board_id, snapshot, phase=phase)
            return callbacks.apply_step_unguarded(snapshot, graph_type, step)


__all__ = ["CommunityRoutedGraphLifecycle"]
