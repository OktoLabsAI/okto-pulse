from __future__ import annotations

import asyncio
import inspect
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
)
from okto_pulse.core.kg.interfaces.graph_lifecycle import (
    GraphHandle,
    GraphLifecycle,
    GraphLifecycleStepResult,
    PurgeReport,
    RebuildReport,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef
from okto_pulse.core.kg.safe_write_lifecycle import (
    STEP_CHECKPOINT,
    STEP_CLOSE_REOPEN_PROBE,
    STEP_FLUSH,
)

from okto_pulse.community.adapters import routed_graph_lifecycle as lifecycle_module
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteSnapshot,
)
from okto_pulse.community.adapters.routed_graph_lifecycle import (
    CommunityRoutedGraphLifecycle,
)


def _snapshot(
    tmp_path: Path,
    *,
    backend: str,
    board_id: str = "board-1",
    generation: str = "generation-1",
) -> CommunityGraphRouteSnapshot:
    suffix = "graph.lbug" if backend == "ladybug" else "graph.grafx"
    active = tmp_path / "boards" / board_id / suffix
    return CommunityGraphRouteSnapshot(
        scope="board",
        scope_id=board_id,
        backend=backend,
        generation=generation,
        binding_path=active,
        anchor_path=active,
        active_path=active,
        page_size=8192 if backend == "grafx" else None,
        binding_sha256=f"binding-{backend}",
        route_sha256=f"route-{backend}-{generation}",
    )


class _Resolver:
    def __init__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        events: list[Any],
        *,
        acquire_failure: BaseException | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.events = events
        self.acquire_failure = acquire_failure
        self.acquire_count = 0
        self.revalidate_count = 0

    def acquire_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        self.acquire_count += 1
        self.events.append(("acquire", board_id, self.snapshot))
        if self.acquire_failure is not None:
            raise self.acquire_failure
        return self.snapshot

    def revalidate_snapshot(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        require_physical: bool = False,
    ) -> CommunityGraphRouteSnapshot:
        self.revalidate_count += 1
        self.events.append(("revalidate", snapshot, require_physical))
        return snapshot


def _windows(events: list[Any]):
    @contextmanager
    def operation(board_id: str):
        events.append(("operation.enter", board_id))
        try:
            yield
        finally:
            events.append(("operation.exit", board_id))

    @contextmanager
    def mutation(board_id: str, *, phase: str):
        events.append(("mutation.enter", board_id, phase))
        try:
            yield
        finally:
            events.append(("mutation.exit", board_id, phase))

    return operation, mutation


def _defaults(
    backend: str,
    events: list[Any],
) -> dict[str, Any]:
    def opened(snapshot: CommunityGraphRouteSnapshot) -> GraphHandle:
        events.append((f"{backend}.open_unguarded", snapshot))
        return GraphHandle(
            board_id=snapshot.scope_id,
            storage_ref=StorageRef(
                f"board:{snapshot.scope_id}",
                f"test_{backend}",
            ),
            opened=True,
            status="opened",
            locked=False,
            quarantined=False,
        )

    def closed(snapshot: CommunityGraphRouteSnapshot) -> None:
        events.append((f"{backend}.close_unguarded", snapshot))

    def rebuilt(snapshot: CommunityGraphRouteSnapshot) -> RebuildReport:
        events.append((f"{backend}.rebuild_unguarded", snapshot))
        return RebuildReport(board_id=snapshot.scope_id, status="rebuilt")

    def purged(snapshot: CommunityGraphRouteSnapshot, reason: str) -> PurgeReport:
        events.append((f"{backend}.purge_unguarded", snapshot, reason))
        return PurgeReport(
            board_id=snapshot.scope_id,
            status="purged",
            reason=reason,
        )

    def stepped(
        snapshot: CommunityGraphRouteSnapshot,
        graph_type: str,
        step: str,
    ) -> GraphLifecycleStepResult:
        events.append((f"{backend}.apply_step_unguarded", snapshot, graph_type, step))
        return GraphLifecycleStepResult(ok=True, detail=backend)

    def close_all() -> None:
        events.append((f"{backend}.close_all_unguarded",))

    return {
        f"{backend}_open_unguarded": opened,
        f"{backend}_close_unguarded": closed,
        f"{backend}_rebuild_unguarded": rebuilt,
        f"{backend}_purge_unguarded": purged,
        f"{backend}_apply_step_unguarded": stepped,
        f"{backend}_close_all_unguarded": close_all,
    }


def _lifecycle(
    resolver: _Resolver,
    events: list[Any],
    **overrides: Any,
) -> CommunityRoutedGraphLifecycle:
    operation, mutation = _windows(events)

    def fence(board_id: str, phase: str) -> None:
        events.append(("fence", board_id, phase))

    kwargs: dict[str, Any] = {
        "operation_window": operation,
        "mutation_window_unguarded": mutation,
        "revalidate_write_fence": fence,
        **_defaults("ladybug", events),
        **_defaults("grafx", events),
    }
    kwargs.update(overrides)
    return CommunityRoutedGraphLifecycle(resolver, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("backend", ["ladybug", "grafx"])
async def test_open_resolves_one_immutable_route_and_dispatches_exact_backend(
    tmp_path: Path,
    backend: str,
) -> None:
    events: list[Any] = []
    snapshot = _snapshot(tmp_path, backend=backend)
    resolver = _Resolver(snapshot, events)
    lifecycle = _lifecycle(resolver, events)

    handle = await lifecycle.open("board-1")

    assert handle.board_id == "board-1"
    assert resolver.acquire_count == 1
    assert resolver.revalidate_count == 1
    assert ("revalidate", snapshot, True) in events
    assert (f"{backend}.open_unguarded", snapshot) in events
    alternate = "grafx" if backend == "ladybug" else "ladybug"
    assert not any(str(event[0]).startswith(f"{alternate}.") for event in events)
    assert events[0] == ("operation.enter", "board-1")
    assert events[-1] == ("operation.exit", "board-1")


@pytest.mark.parametrize("backend", ["ladybug", "grafx"])
@pytest.mark.parametrize("operation", ["close", "rebuild", "purge"])
async def test_destructive_async_operations_revalidate_fence_and_physical_route(
    tmp_path: Path,
    backend: str,
    operation: str,
) -> None:
    events: list[Any] = []
    snapshot = _snapshot(tmp_path, backend=backend)
    resolver = _Resolver(snapshot, events)
    lifecycle = _lifecycle(resolver, events)

    if operation == "close":
        await lifecycle.close("board-1")
    elif operation == "rebuild":
        result = await lifecycle.rebuild("board-1")
        assert result.status == "rebuilt"
    else:
        result = await lifecycle.purge("board-1", reason="operator")
        assert result.status == "purged"

    phase = f"graph_lifecycle_{operation}"
    callback = f"{backend}.{operation}_unguarded"
    names = [event[0] for event in events]
    assert names == [
        "mutation.enter",
        "acquire",
        "fence",
        "revalidate",
        callback,
        "mutation.exit",
    ]
    assert events[3] == ("revalidate", snapshot, True)
    assert resolver.acquire_count == 1
    assert events[0] == ("mutation.enter", "board-1", phase)


@pytest.mark.parametrize("backend", ["ladybug", "grafx"])
def test_durability_steps_dispatch_without_nested_writer_acquisition(
    tmp_path: Path,
    backend: str,
) -> None:
    events: list[Any] = []
    snapshot = _snapshot(tmp_path, backend=backend)
    resolver = _Resolver(snapshot, events)
    lifecycle = _lifecycle(resolver, events)

    ordinary = lifecycle.apply_step("board-1", "board_graph", STEP_FLUSH)
    checkpoint = lifecycle.apply_step("board-1", "board_graph", STEP_CHECKPOINT)
    destructive = lifecycle.apply_step(
        "board-1", "board_graph", STEP_CLOSE_REOPEN_PROBE
    )

    assert ordinary.ok and checkpoint.ok and destructive.ok
    assert resolver.acquire_count == 3
    assert resolver.revalidate_count == 3
    assert [event[0] for event in events].count("operation.enter") == 1
    assert [event[0] for event in events].count("mutation.enter") == 2
    for index, event in enumerate(events):
        if event[0] == f"{backend}.apply_step_unguarded":
            assert events[index - 1] == ("revalidate", event[1], True)


async def test_route_is_not_reselected_when_resolver_preference_changes(
    tmp_path: Path,
) -> None:
    events: list[Any] = []
    ladybug = _snapshot(tmp_path, backend="ladybug", generation="g-pinned")
    grafx = _snapshot(tmp_path, backend="grafx", generation="g-new")
    resolver = _Resolver(ladybug, events)

    def pinned_open(snapshot: CommunityGraphRouteSnapshot) -> GraphHandle:
        resolver.snapshot = grafx
        events.append(("pinned.open", snapshot))
        return GraphHandle(
            board_id=snapshot.scope_id,
            storage_ref=StorageRef("board:board-1", "test"),
            opened=True,
            status="opened",
            locked=False,
            quarantined=False,
        )

    lifecycle = _lifecycle(
        resolver,
        events,
        ladybug_open_unguarded=pinned_open,
        grafx_open_unguarded=lambda _snapshot: pytest.fail("route was reselected"),
    )

    await lifecycle.open("board-1")

    assert resolver.acquire_count == 1
    assert ("pinned.open", ladybug) in events


@pytest.mark.parametrize("outcome", ["success", "failure", "cancellation"])
async def test_async_operation_window_cleanup_is_exactly_once(
    tmp_path: Path,
    outcome: str,
) -> None:
    events: list[Any] = []
    snapshot = _snapshot(tmp_path, backend="grafx")
    resolver = _Resolver(snapshot, events)
    started = asyncio.Event()
    never = asyncio.Event()

    async def callback(
        routed: CommunityGraphRouteSnapshot,
    ) -> RebuildReport:
        events.append(("callback", routed))
        started.set()
        if outcome == "failure":
            raise RuntimeError("physical failure")
        if outcome == "cancellation":
            await never.wait()
        return RebuildReport(board_id=routed.scope_id, status="rebuilt")

    lifecycle = _lifecycle(
        resolver,
        events,
        grafx_rebuild_unguarded=callback,
    )
    task = asyncio.create_task(lifecycle.rebuild("board-1"))
    await started.wait()
    if outcome == "cancellation":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "failure":
        with pytest.raises(RuntimeError, match="physical failure"):
            await task
    else:
        assert (await task).status == "rebuilt"

    names = [event[0] for event in events]
    assert names.count("mutation.enter") == 1
    assert names.count("mutation.exit") == 1
    assert names.count("callback") == 1


async def test_fence_failure_cleans_window_and_never_dispatches(
    tmp_path: Path,
) -> None:
    events: list[Any] = []
    snapshot = _snapshot(tmp_path, backend="ladybug")
    resolver = _Resolver(snapshot, events)

    def lost_fence(board_id: str, phase: str) -> None:
        events.append(("fence", board_id, phase))
        raise GraphCapabilityUnavailable(
            "lost",
            details={"reason": "writer_lease_lost"},
        )

    lifecycle = _lifecycle(
        resolver,
        events,
        revalidate_write_fence=lost_fence,
    )

    with pytest.raises(GraphCapabilityUnavailable):
        await lifecycle.purge("board-1", reason="operator")

    names = [event[0] for event in events]
    assert names == ["mutation.enter", "acquire", "fence", "mutation.exit"]


async def test_close_all_starts_both_board_backends_once_and_does_not_route(
    tmp_path: Path,
) -> None:
    events: list[Any] = []
    resolver = _Resolver(_snapshot(tmp_path, backend="ladybug"), events)

    def failed_ladybug() -> None:
        events.append(("ladybug.all",))
        raise RuntimeError("ladybug close failed")

    async def closed_grafx() -> None:
        events.append(("grafx.all",))
        await asyncio.sleep(0)

    lifecycle = _lifecycle(
        resolver,
        events,
        ladybug_close_all_unguarded=failed_ladybug,
        grafx_close_all_unguarded=closed_grafx,
    )

    with pytest.raises(RuntimeError, match="ladybug close failed"):
        await lifecycle.close(None)

    assert events.count(("ladybug.all",)) == 1
    assert events.count(("grafx.all",)) == 1
    assert resolver.acquire_count == 0
    assert not any(event[0].endswith(".enter") for event in events)


async def test_missing_first_boot_binding_fails_without_creation_or_fallback(
    tmp_path: Path,
) -> None:
    events: list[Any] = []
    failure = GraphCapabilityUnavailable(
        "missing",
        details={"reason": "binding_missing"},
    )
    resolver = _Resolver(
        _snapshot(tmp_path, backend="ladybug"),
        events,
        acquire_failure=failure,
    )
    lifecycle = _lifecycle(resolver, events)

    with pytest.raises(GraphCapabilityUnavailable) as caught:
        await lifecycle.open("board-1")

    assert caught.value is failure
    assert resolver.acquire_count == 1
    assert resolver.revalidate_count == 0
    assert [event[0] for event in events] == [
        "operation.enter",
        "acquire",
        "operation.exit",
    ]


async def test_invalid_route_scope_fails_before_physical_dispatch_and_cleans_window(
    tmp_path: Path,
) -> None:
    events: list[Any] = []
    invalid = _snapshot(tmp_path, backend="grafx", board_id="other-board")
    resolver = _Resolver(invalid, events)
    lifecycle = _lifecycle(resolver, events)

    with pytest.raises(GraphCorruption) as caught:
        await lifecycle.open("board-1")

    assert caught.value.details["reason"] == "graph_route_snapshot_scope_invalid"
    assert resolver.revalidate_count == 0
    assert not any("_unguarded" in str(event[0]) for event in events)
    assert [event[0] for event in events] == [
        "operation.enter",
        "acquire",
        "operation.exit",
    ]


def test_invalid_step_and_graph_type_do_not_resolve_or_open(tmp_path: Path) -> None:
    events: list[Any] = []
    resolver = _Resolver(_snapshot(tmp_path, backend="grafx"), events)
    lifecycle = _lifecycle(resolver, events)

    unsupported = lifecycle.apply_step("board-1", "global_discovery", STEP_FLUSH)
    unknown = lifecycle.apply_step("board-1", "board_graph", "invented")

    assert unsupported == GraphLifecycleStepResult(
        ok=False,
        detail="unsupported_graph_type=global_discovery",
    )
    assert unknown == GraphLifecycleStepResult(
        ok=False,
        detail="unknown_step=invented",
    )
    assert resolver.acquire_count == 0
    assert events == []


def test_only_router_is_public_and_no_guarded_provider_can_be_composed() -> None:
    assert lifecycle_module.__all__ == ["CommunityRoutedGraphLifecycle"]
    signature = inspect.signature(CommunityRoutedGraphLifecycle)
    physical_names = {
        name for name in signature.parameters if name.startswith(("ladybug_", "grafx_"))
    }
    assert physical_names
    assert all(name.endswith("_unguarded") for name in physical_names)

    source = inspect.getsource(lifecycle_module)
    for forbidden in (
        "initialize_board_route",
        "inspect_board_route",
        "get_settings",
        "ladybug_writer_scope",
        "CommunityKuzuGraphLifecycle",
        "CommunityGrafxGraphLifecycle",
    ):
        assert forbidden not in source


def test_router_satisfies_canonical_core_graph_lifecycle_protocol(
    tmp_path: Path,
) -> None:
    events: list[Any] = []
    resolver = _Resolver(_snapshot(tmp_path, backend="ladybug"), events)
    lifecycle = _lifecycle(resolver, events)

    assert isinstance(lifecycle, GraphLifecycle)
