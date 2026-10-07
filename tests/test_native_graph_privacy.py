"""Native graph privacy fences, physical receipts and retry semantics."""

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
    revalidate_write_fence: Any = None,
    grafx_erase: Any,
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
        grafx=object(),  # type: ignore[arg-type]
        operation_window=lambda _board_id: nullcontext(),
        mutation_window=mutation_window,
        grafx_purge_unguarded=grafx_purge
        or (
            lambda _board_id, *, reason: _receipt(
                reason=reason, backend="grafx", removed=False
            )
        ),
        grafx_erase_unguarded=grafx_erase,
        revalidate_write_fence=revalidate_write_fence,
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
        revalidate_write_fence=write_fence,
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
        raise RuntimeError("write fence refused purge")

    def forbidden_purge(*_args: object, **_kwargs: object) -> GraphPurgeResult:
        raise AssertionError("physical purge started after write-fence failure")

    facade = _facade(
        resolver=resolver,
        events=events,
        active=active,
        revalidate_write_fence=failing_write_fence,
        grafx_erase=lambda *_args, **_kwargs: None,
        grafx_purge=forbidden_purge,
    )

    with pytest.raises(RuntimeError, match="write fence refused purge"):
        facade.purge_board_graph(_BOARD_ID, reason="manual")

    assert events == ["window_enter", "inspect", "write_fence", "window_exit"]
    assert not active


@pytest.mark.parametrize("removed", [False, True])
def test_physical_erasure_normalizes_receipt(removed: bool) -> None:
    events, active = [], []
    resolver = _Resolver(_route(), events=events, active=active)

    def erase(board_id, *, reason):
        assert board_id == _BOARD_ID
        assert active == [True]
        events.append("grafx")
        return _receipt(reason=reason, backend="grafx", removed=removed)

    facade = _facade(resolver=resolver, events=events, active=active, grafx_erase=erase)
    result = facade.erase_board_graph(_BOARD_ID, reason="privacy")
    assert result.removed is removed
    assert result.not_found is (not removed)
    assert result.status == ("erased" if removed else "not_found")
    assert result.reason == "privacy"
    assert events == ["window_enter", "inspect", "grafx", "window_exit"]
    assert not active


@pytest.mark.parametrize("failure_mode", ["receipt", "exception"])
def test_failed_physical_erasure_is_not_reported_as_absent(failure_mode: str) -> None:
    events, active = [], []
    resolver = _Resolver(_route(), events=events, active=active)

    def erase(board_id, *, reason):
        assert board_id == _BOARD_ID
        assert active == [True]
        events.append("grafx")
        if failure_mode == "exception":
            raise OSError("physical residue remained")
        return _receipt(reason=reason, backend="grafx", removed=False, failed=True)

    facade = _facade(resolver=resolver, events=events, active=active, grafx_erase=erase)
    result = facade.erase_board_graph(_BOARD_ID, reason="privacy")
    assert result.status == "failed"
    assert result.error_code == "privacy_erase_incomplete"
    assert not result.removed and not result.not_found
    assert events == ["window_enter", "inspect", "grafx", "window_exit"]
    assert not active


def test_missing_binding_retry_still_checks_physical_storage() -> None:
    events, active = [], []
    resolver = _Resolver(_missing_binding(), events=events, active=active)
    present = True

    def erase(board_id, *, reason):
        nonlocal present
        assert board_id == _BOARD_ID
        assert active == [True]
        events.append("grafx")
        removed, present = present, False
        return _receipt(reason=reason, backend="grafx", removed=removed)

    facade = _facade(resolver=resolver, events=events, active=active, grafx_erase=erase)
    first = facade.erase_board_graph(_BOARD_ID, reason="privacy")
    retry = facade.erase_board_graph(_BOARD_ID, reason="privacy_retry")
    assert first.status == "erased" and first.removed
    assert retry.status == "not_found" and retry.not_found
    assert events == ["window_enter", "inspect", "grafx", "window_exit"] * 2
    assert not active


def test_partial_physical_failure_can_be_retried() -> None:
    events, active = [], []
    resolver = _Resolver(_route(), events=events, active=active)
    attempts = 0

    def erase(board_id, *, reason):
        nonlocal attempts
        assert board_id == _BOARD_ID
        assert active == [True]
        attempts += 1
        return _receipt(reason=reason, backend="grafx",
                        removed=attempts > 1, failed=attempts == 1)

    facade = _facade(resolver=resolver, events=events, active=active, grafx_erase=erase)
    first = facade.erase_board_graph(_BOARD_ID, reason="privacy")
    assert first.status == "failed"
    assert first.error_code == "privacy_erase_incomplete"
    retry = facade.erase_board_graph(_BOARD_ID, reason="privacy_retry")
    assert retry.status == "erased" and retry.error_code is None
    assert attempts == 2 and not active
