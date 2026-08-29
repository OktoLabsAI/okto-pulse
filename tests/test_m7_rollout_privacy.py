"""M-PULSE-7 privacy ordering across rollout and physical graph storage."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.interfaces.graph_runtime_store import GraphPurgeResult

from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteSnapshot,
)
from okto_pulse.community.adapters.routed_board_graph_facades import (
    CommunityRoutedGraphRuntimeStore,
)

_BOARD_ID = "board-privacy"


def _route(*, backend: str = "grafx") -> CommunityGraphRouteSnapshot:
    path = Path("C:/m7-privacy") / _BOARD_ID / backend
    return CommunityGraphRouteSnapshot(
        scope="board",
        scope_id=_BOARD_ID,
        backend=backend,  # type: ignore[arg-type]
        generation="generation-1",
        binding_path=path,
        anchor_path=path,
        active_path=path,
        page_size=8192 if backend == "grafx" else None,
        binding_sha256="b" * 64,
        route_sha256="r" * 64,
    )


def _missing_binding() -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "binding missing",
        details={"reason": "binding_missing", "scope": "board"},
    )


def _receipt(
    *,
    reason: str,
    backend: str,
    removed: bool,
    failed: bool = False,
) -> GraphPurgeResult:
    if failed:
        return GraphPurgeResult(
            board_id=_BOARD_ID,
            removed=removed,
            not_found=False,
            status="failed",
            reason=reason,
            backend=backend,
            error_code=f"{backend}_erase_failed",
        )
    return GraphPurgeResult(
        board_id=_BOARD_ID,
        removed=removed,
        not_found=not removed,
        status="erased" if removed else "not_found",
        reason=reason,
        backend=backend,
    )


class _Resolver:
    def __init__(
        self,
        route: CommunityGraphRouteSnapshot | Exception,
        *,
        events: list[str],
        active: list[bool],
    ) -> None:
        self.route = route
        self.events = events
        self.active = active
        self.inspect_calls = 0

    def inspect_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        assert board_id == _BOARD_ID
        assert self.active == [True]
        self.inspect_calls += 1
        self.events.append("inspect")
        if isinstance(self.route, Exception):
            raise self.route
        return self.route


def _facade(
    *,
    resolver: _Resolver,
    events: list[str],
    active: list[bool],
    rollout_erase: Any = None,
    rollout_finalize: Any = None,
    rollout_write_fence: Any = None,
    ladybug_erase: Any,
    grafx_erase: Any,
    ladybug_purge: Any = None,
    grafx_purge: Any = None,
) -> CommunityRoutedGraphRuntimeStore:
    @contextmanager
    def mutation_window(board_id: str, *, phase: str):
        assert board_id == _BOARD_ID
        assert phase in {"erase_board_graph", "purge_board_graph"}
        assert not active
        active.append(True)
        events.append("window_enter")
        try:
            yield
        finally:
            events.append("window_exit")
            active.clear()

    return CommunityRoutedGraphRuntimeStore(
        resolver,  # type: ignore[arg-type]
        ladybug=object(),  # type: ignore[arg-type]
        grafx=object(),  # type: ignore[arg-type]
        operation_window=lambda _board_id: nullcontext(),
        mutation_window=mutation_window,
        ladybug_purge_unguarded=ladybug_purge
        or (
            lambda _board_id, *, reason: _receipt(
                reason=reason, backend="ladybug", removed=False
            )
        ),
        grafx_purge_unguarded=grafx_purge
        or (
            lambda _board_id, *, reason: _receipt(
                reason=reason, backend="grafx", removed=False
            )
        ),
        ladybug_erase_unguarded=ladybug_erase,
        grafx_erase_unguarded=grafx_erase,
        rollout_erase_unguarded=rollout_erase,
        rollout_finalize_erase_unguarded=rollout_finalize,
        rollout_write_fence=rollout_write_fence,
    )


def test_purge_write_fence_runs_after_route_selection_before_physical_purge() -> None:
    events: list[str] = []
    active: list[bool] = []
    route = _route()
    resolver = _Resolver(route, events=events, active=active)

    def write_fence(
        board_id: str,
        phase: str,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> None:
        assert active == [True]
        assert board_id == _BOARD_ID
        assert phase == "purge_board_graph"
        assert snapshot is route
        events.append("write_fence")

    def grafx_purge(board_id: str, *, reason: str) -> GraphPurgeResult:
        assert active == [True]
        assert board_id == _BOARD_ID
        events.append("grafx_purge")
        return _receipt(reason=reason, backend="grafx", removed=True)

    facade = _facade(
        resolver=resolver,
        events=events,
        active=active,
        rollout_write_fence=write_fence,
        ladybug_erase=lambda *_args, **_kwargs: None,
        grafx_erase=lambda *_args, **_kwargs: None,
        grafx_purge=grafx_purge,
    )

    result = facade.purge_board_graph(_BOARD_ID, reason="manual")

    assert result.status == "erased"
    assert result.removed is True
    assert events == [
        "window_enter",
        "inspect",
        "write_fence",
        "grafx_purge",
        "window_exit",
    ]
    assert not active


def test_purge_write_fence_failure_blocks_physical_purge() -> None:
    events: list[str] = []
    active: list[bool] = []
    resolver = _Resolver(_route(), events=events, active=active)

    def failing_write_fence(
        _board_id: str,
        _phase: str,
        _snapshot: CommunityGraphRouteSnapshot,
    ) -> None:
        assert active == [True]
        events.append("write_fence")
        raise RuntimeError("rollout write fence refused purge")

    def forbidden_purge(*_args: object, **_kwargs: object) -> GraphPurgeResult:
        raise AssertionError("physical purge started after write-fence failure")

    facade = _facade(
        resolver=resolver,
        events=events,
        active=active,
        rollout_write_fence=failing_write_fence,
        ladybug_erase=lambda *_args, **_kwargs: None,
        grafx_erase=lambda *_args, **_kwargs: None,
        grafx_purge=forbidden_purge,
    )

    with pytest.raises(RuntimeError, match="write fence refused purge"):
        facade.purge_board_graph(_BOARD_ID, reason="manual")

    assert events == ["window_enter", "inspect", "write_fence", "window_exit"]
    assert not active


def test_rollout_erasure_runs_first_and_aggregates_three_receipts() -> None:
    events: list[str] = []
    active: list[bool] = []
    resolver = _Resolver(_route(), events=events, active=active)

    def erase(name: str, *, removed: bool):
        def operation(board_id: str, *, reason: str) -> GraphPurgeResult:
            assert board_id == _BOARD_ID
            assert active == [True]
            events.append(name)
            return _receipt(reason=reason, backend=name, removed=removed)

        return operation

    facade = _facade(
        resolver=resolver,
        events=events,
        active=active,
        rollout_erase=erase("rollout", removed=True),
        ladybug_erase=erase("ladybug", removed=True),
        grafx_erase=erase("grafx", removed=False),
    )

    result = facade.erase_board_graph(_BOARD_ID, reason="right_to_erasure")

    assert result == GraphPurgeResult(
        board_id=_BOARD_ID,
        removed=True,
        not_found=False,
        status="erased",
        reason="right_to_erasure",
        backend=None,
        error_code=None,
    )
    assert events == [
        "window_enter",
        "rollout",
        "inspect",
        "grafx",
        "ladybug",
        "window_exit",
    ]
    assert not active


@pytest.mark.parametrize("failure_mode", ["receipt", "exception"])
def test_rollout_erasure_failure_blocks_every_physical_backend(
    failure_mode: str,
) -> None:
    events: list[str] = []
    active: list[bool] = []
    resolver = _Resolver(_route(), events=events, active=active)

    def rollout_erase(board_id: str, *, reason: str) -> GraphPurgeResult:
        assert board_id == _BOARD_ID
        assert active == [True]
        events.append("rollout")
        if failure_mode == "exception":
            raise OSError("rollout residue remained")
        return _receipt(
            reason=reason,
            backend="rollout",
            removed=False,
            failed=True,
        )

    def forbidden_backend(*_args: object, **_kwargs: object) -> GraphPurgeResult:
        raise AssertionError("physical erasure started after rollout failure")

    facade = _facade(
        resolver=resolver,
        events=events,
        active=active,
        rollout_erase=rollout_erase,
        ladybug_erase=forbidden_backend,
        grafx_erase=forbidden_backend,
    )

    result = facade.erase_board_graph(_BOARD_ID, reason="privacy")

    assert result.status == "failed"
    assert result.error_code == "privacy_erase_incomplete"
    assert result.removed is False
    assert result.not_found is False
    assert resolver.inspect_calls == 0
    assert events == ["window_enter", "rollout", "window_exit"]
    assert not active


def test_retry_after_rollout_absence_still_sweeps_both_physical_backends() -> None:
    events: list[str] = []
    active: list[bool] = []
    resolver = _Resolver(_missing_binding(), events=events, active=active)
    present = {"rollout": True, "ladybug": True, "grafx": True}

    def erase(name: str):
        def operation(board_id: str, *, reason: str) -> GraphPurgeResult:
            assert board_id == _BOARD_ID
            assert active == [True]
            events.append(name)
            removed = present[name]
            present[name] = False
            return _receipt(reason=reason, backend=name, removed=removed)

        return operation

    facade = _facade(
        resolver=resolver,
        events=events,
        active=active,
        rollout_erase=erase("rollout"),
        ladybug_erase=erase("ladybug"),
        grafx_erase=erase("grafx"),
    )

    first = facade.erase_board_graph(_BOARD_ID, reason="privacy")
    retry = facade.erase_board_graph(_BOARD_ID, reason="privacy_retry")

    assert first.status == "erased"
    assert first.removed is True
    assert retry.status == "not_found"
    assert retry.not_found is True
    assert present == {"rollout": False, "ladybug": False, "grafx": False}
    assert events == [
        "window_enter",
        "rollout",
        "inspect",
        "ladybug",
        "grafx",
        "window_exit",
        "window_enter",
        "rollout",
        "inspect",
        "ladybug",
        "grafx",
        "window_exit",
    ]
    assert not active


def test_partial_physical_failure_keeps_tombstone_until_retry_finalizes() -> None:
    events: list[str] = []
    active: list[bool] = []
    resolver = _Resolver(_route(), events=events, active=active)
    tombstone = {"present": False}
    grafx_attempts = 0

    def invalidate(board_id: str, *, reason: str) -> GraphPurgeResult:
        assert board_id == _BOARD_ID
        events.append("invalidate")
        changed = not tombstone["present"]
        tombstone["present"] = True
        return _receipt(
            reason=reason,
            backend="rollout",
            removed=changed,
        )

    def ladybug_erase(board_id: str, *, reason: str) -> GraphPurgeResult:
        assert tombstone["present"]
        events.append("ladybug")
        return _receipt(reason=reason, backend="ladybug", removed=False)

    def grafx_erase(board_id: str, *, reason: str) -> GraphPurgeResult:
        nonlocal grafx_attempts
        assert tombstone["present"]
        events.append("grafx")
        grafx_attempts += 1
        return _receipt(
            reason=reason,
            backend="grafx",
            removed=grafx_attempts > 1,
            failed=grafx_attempts == 1,
        )

    def finalize(board_id: str, *, reason: str) -> GraphPurgeResult:
        assert board_id == _BOARD_ID
        assert tombstone["present"]
        assert grafx_attempts == 2
        events.append("finalize")
        tombstone["present"] = False
        return _receipt(reason=reason, backend="rollout", removed=True)

    facade = _facade(
        resolver=resolver,
        events=events,
        active=active,
        rollout_erase=invalidate,
        rollout_finalize=finalize,
        ladybug_erase=ladybug_erase,
        grafx_erase=grafx_erase,
    )

    first = facade.erase_board_graph(_BOARD_ID, reason="privacy")
    assert first.status == "failed"
    assert first.error_code == "privacy_erase_incomplete"
    assert tombstone["present"] is True
    assert "finalize" not in events

    retry = facade.erase_board_graph(_BOARD_ID, reason="privacy_retry")
    assert retry.status == "erased"
    assert retry.error_code is None
    assert tombstone["present"] is False
    assert events == [
        "window_enter",
        "invalidate",
        "inspect",
        "grafx",
        "ladybug",
        "window_exit",
        "window_enter",
        "invalidate",
        "inspect",
        "grafx",
        "ladybug",
        "finalize",
        "window_exit",
    ]
