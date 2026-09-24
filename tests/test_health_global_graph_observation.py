"""Health Global reads must not open writable participants or omit deadlines."""

import threading
from types import SimpleNamespace

import okto_grafx
import pytest

from okto_pulse.community.adapters.grafx_database_pool import CommunityGrafxDatabasePool
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.graph_route_resolver import CommunityGraphRouteResolver
from okto_pulse.community.adapters.routed_global_graph_composition import build_community_routed_global_graph_composition
from okto_pulse.community.adapters.routed_graph_composition import build_community_routed_graph_composition
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.kg import interfaces
from okto_pulse.core.kg.global_discovery.layer_parity import collect_digest_layer_mismatch_inputs


@pytest.fixture
def global_observation_bundle(tmp_path):
    root = tmp_path / "graph"
    # Explicit fixture bootstrap uses a standalone supplied administration fence.
    # The Health composition below retains its real Core writer fence throughout.
    store = CommunityGraphBackendBindingStore(root)
    setup = build_community_routed_global_graph_composition(
        binding_store=store,
        resolver=CommunityGraphRouteResolver(store, board_backend="grafx", global_backend="grafx", grafx_page_size=4096),
        grafx_pool=CommunityGrafxDatabasePool(root), global_lock=threading.RLock(),
        revalidate_write_fence=lambda _phase: None,
    )
    try:
        setup.initialize_global_route()
    finally:
        setup.close_all_on_shutdown()

    class Clock:
        now = 100.0
        advance = False

        def monotonic(self):
            if self.advance:
                self.now += 1.0
            return self.now

    clock = Clock()
    opened_modes = []

    def connect(*args, **kwargs):
        opened_modes.append(kwargs.get("read_only", False))
        database = okto_grafx.connect(*args, **kwargs)
        database._clock = clock
        return database

    bundle = build_community_routed_graph_composition(settings=SimpleNamespace(
        kg_base_dir=str(root), data_dir=str(tmp_path),
        kg_graph_backend="grafx", kg_global_graph_backend="grafx", kg_grafx_page_size=4096,
    ), grafx_connect=connect)
    try:
        yield bundle, clock, opened_modes
    finally:
        bundle.global_graph.close_all_on_shutdown()


def test_cold_global_health_never_opens_a_writable_participant(global_observation_bundle):
    bundle, _clock, opened_modes = global_observation_bundle
    with bundle.board.graph_health_observation.scope("board"):
        try:
            bundle.global_graph.runtime.execute("MATCH (n:DecisionDigest) RETURN count(n)")
        except GraphError:
            pass  # A cold read that requires maintenance must stay unavailable.
    assert all(opened_modes), f"Health opened native participants with read_only={opened_modes}"


def test_warm_global_health_receives_native_deadline(global_observation_bundle):
    bundle, clock, _opened_modes = global_observation_bundle
    query = "MATCH (n:DecisionDigest) RETURN count(n)"
    for _ in range(2):
        assert bundle.global_graph.runtime.execute(query).rows == ((0,),)
    clock.advance = True
    with bundle.board.graph_health_observation.scope("board"), pytest.raises(GraphError) as failure:
        bundle.global_graph.runtime.execute(query)
    assert failure.value.details["backend_error_code"] == "query_deadline_exceeded"
    assert bundle.global_graph.runtime.execute(query).rows == ((0,),)


@pytest.mark.parametrize("warm", [False, True])
def test_health_parity_distinguishes_unobserved_from_confirmed_empty(global_observation_bundle, monkeypatch, warm):
    bundle, _clock, opened_modes = global_observation_bundle
    if warm:
        for _ in range(2):
            bundle.global_graph.runtime.execute("MATCH (n:DecisionDigest) RETURN count(n)")
    opened_before = list(opened_modes)
    monkeypatch.setattr(interfaces, "get_kg_registry", lambda: SimpleNamespace(
        require_global_discovery_runtime=lambda: bundle.global_graph.runtime,
    ))
    with bundle.board.graph_health_observation.scope("board"):
        result = collect_digest_layer_mismatch_inputs("board")
    assert result["status"] == ("available" if warm else "unavailable")
    assert result["reason"] == ("no_digests" if warm else "global_discovery_read_failed")
    assert opened_modes == opened_before
