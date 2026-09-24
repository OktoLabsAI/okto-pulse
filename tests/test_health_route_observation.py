"""REST route metadata is part of bounded Health, including its worker scope."""
from contextlib import contextmanager, nullcontext
import threading
import time
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters import composition
from okto_pulse.community.api import kg_health as api
from test_health_global_graph_observation import (
    _install_large_global_manifest,
    global_observation_bundle,  # noqa: F401
)


async def request_health(monkeypatch, board_id):
    class UseCase:
        async def execute(self, command, *, actor, uow):
            assert command.board_id == board_id
            return SimpleNamespace(data={"board_id": board_id})

    # Isolate the endpoint's post-use-case metadata work. Response model
    # serialization has a separate strict contract suite.
    monkeypatch.setattr(api, "GetKgHealthUseCase", UseCase)
    monkeypatch.setattr(api, "KGHealthResponse", lambda **data: data)
    monkeypatch.setattr(api, "scheduler_control_from_request", lambda _: None)
    return await api.get_kg_health_endpoint(
        request=SimpleNamespace(), board_id=board_id, user_id="owner", db=object(),
    )


@pytest.mark.asyncio
async def test_rest_route_metadata_cannot_block_the_event_loop(tmp_path, monkeypatch):
    released = threading.Event()
    timer = threading.Timer(0.9, released.set)

    def inspect(_board):
        assert released.wait(3)
        return SimpleNamespace(active_path=tmp_path / "board", backend="grafx",
                               generation="g1", page_size=4096)

    bundle = SimpleNamespace(
        resolver=SimpleNamespace(inspect_board_route=inspect, inspect_global_route=lambda: inspect(None)),
        binding_store=SimpleNamespace(root=tmp_path),
        board=SimpleNamespace(graph_health_observation=SimpleNamespace(scope=lambda *a, **kw: nullcontext())),
    )
    monkeypatch.setattr(composition, "require_community_routed_graph_composition", lambda: bundle)
    timer.start()
    started = time.monotonic()
    try:
        result = await request_health(monkeypatch, "route-blocked")
        assert time.monotonic() - started < 0.7
        assert result["graph_storage"].board.binding_status == "unavailable"
    finally:
        released.set()
        timer.cancel()
        timer.join(timeout=2)


@pytest.mark.asyncio
async def test_rest_route_metadata_bounds_global_manifest(global_observation_bundle, monkeypatch):  # noqa: F811
    bundle, _clock, opened_modes = global_observation_bundle
    _install_large_global_manifest(bundle)
    monkeypatch.setattr(composition, "require_community_routed_graph_composition", lambda: bundle)
    result = await request_health(monkeypatch, "route-large-manifest")
    assert result["graph_storage"].global_graph.binding_status == "unavailable"
    assert opened_modes == []
    assert bundle.global_graph.runtime.state().state.value == "present_readable_candidate"


@pytest.mark.asyncio
async def test_previous_request_cannot_supply_stale_route_metadata(monkeypatch):
    from okto_pulse.community.adapters import health_route_observation as observation

    async def previous(**kwargs):
        return SimpleNamespace(value=("previous-request", "stale-route"))

    monkeypatch.setattr(observation, "run_bounded_health_probe", previous)
    result = await observation.observe_graph_route_metadata(
        "route-stale", render=lambda *args: pytest.fail("unexpected render"), unavailable="unavailable",
    )
    assert result == "unavailable"


@pytest.mark.asyncio
async def test_missing_route_observation_port_never_calls_resolver(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("metadata read without Health capability")

    bundle = SimpleNamespace(
        resolver=SimpleNamespace(inspect_board_route=forbidden, inspect_global_route=forbidden),
        binding_store=SimpleNamespace(root=tmp_path),
        board=SimpleNamespace(graph_health_observation=None),
    )
    monkeypatch.setattr(composition, "require_community_routed_graph_composition", lambda: bundle)
    result = await request_health(monkeypatch, "route-no-capability")
    assert result["graph_storage"].board.binding_status == "unavailable"
    assert result["graph_storage"].global_graph.binding_status == "unavailable"


@pytest.mark.asyncio
async def test_repeated_route_reads_share_scope_on_worker_and_preserve_identity(tmp_path, monkeypatch):
    parent_thread = threading.get_ident()
    local = threading.local()
    observed = []

    @contextmanager
    def scope(board_id, *, timeout_seconds):
        assert board_id == "route-normal"
        assert 0 < timeout_seconds <= 0.35
        local.active = True
        try:
            yield
        finally:
            local.active = False

    def inspect(kind):
        assert threading.get_ident() != parent_thread
        assert local.active
        observed.append(kind)
        return SimpleNamespace(active_path=tmp_path / kind, backend="grafx",
                               generation="g1", page_size=4096)

    bundle = SimpleNamespace(
        resolver=SimpleNamespace(inspect_board_route=lambda _: inspect("board"),
                                 inspect_global_route=lambda: inspect("global")),
        binding_store=SimpleNamespace(root=tmp_path),
        board=SimpleNamespace(graph_health_observation=SimpleNamespace(scope=scope)),
    )
    monkeypatch.setattr(composition, "require_community_routed_graph_composition", lambda: bundle)
    for _ in range(3):
        result = await request_health(monkeypatch, "route-normal")
        assert result["graph_storage"].board.generation == "g1"
        assert result["graph_storage"].global_graph.generation == "g1"
        assert not getattr(local, "active", False)
    assert observed == ["board", "global"] * 3
