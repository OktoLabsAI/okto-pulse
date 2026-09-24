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


def _install_large_global_manifest(bundle):
    import shutil
    from okto_pulse.community.adapters.global_discovery_layout import (
        generation_graph_path, switch_active_generation, write_generation_manifest,
    )

    anchor = bundle.global_graph.resolver.inspect_global_route().anchor_path
    target = generation_graph_path(anchor, "gdr_health_metadata_volume")
    shutil.copytree(anchor, target)
    digest, _ = write_generation_manifest(
        anchor, generation_id="gdr_health_metadata_volume",
        payload={"historical_metadata": "x" * (4 * 1024 * 1024)},
    )
    switch_active_generation(anchor, generation_id="gdr_health_metadata_volume", manifest_sha256=digest)


def test_global_health_bounds_authenticated_manifest_reads(global_observation_bundle):
    bundle, _clock, opened_modes = global_observation_bundle
    _install_large_global_manifest(bundle)
    assert bundle.global_graph.runtime.state().state.value == "present_readable_candidate"
    with bundle.board.graph_health_observation.scope("board", timeout_seconds=5):
        try:
            observed = bundle.global_graph.runtime.state()
        except GraphError:
            pass  # An incomplete observation must remain unavailable.
        else:
            assert observed.state.value == "present_unreadable_or_error"
    assert opened_modes == []
    assert bundle.global_graph.runtime.state().state.value == "present_readable_candidate"


def test_materialization_guard_does_not_read_unbounded_global_manifest(global_observation_bundle):
    from okto_pulse.community.adapters.materialization_health_observability import CommunityFilesystemMutationGuard

    bundle, _clock, opened_modes = global_observation_bundle
    _install_large_global_manifest(bundle)
    guard = CommunityFilesystemMutationGuard.from_runtime_stores(
        board_store=SimpleNamespace(materialization_observation_paths=lambda _board: ()),
        discovery_store=bundle.global_graph.runtime,
        graph_health_observation=bundle.board.graph_health_observation,
    )
    observed = guard.capture("guard-global-metadata")
    assert observed.sha256 is None
    assert observed.unavailable_reason == "GraphCapabilityUnavailable"
    assert opened_modes == []


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


@pytest.mark.parametrize("query,params", [
    ("UNWIND range(1, 1001) AS n RETURN n", {}),
    ("UNWIND range(1, 600) AS n RETURN $value", {"value": "x" * 8192}),
    ("UNWIND range(1, 600) AS n RETURN $value", {"value": "é" * 4096}),
])
def test_global_health_refuses_result_volume_instead_of_returning_prefix(global_observation_bundle, query, params):
    bundle, _clock, _opened_modes = global_observation_bundle
    for _ in range(2):
        bundle.global_graph.runtime.execute("RETURN 1")
    with bundle.board.graph_health_observation.scope("board", timeout_seconds=5):
        with pytest.raises(GraphError) as failure:
            bundle.global_graph.runtime.execute(query, params)
    assert failure.value.details.get("reason") == "graph_health_result_limit_exceeded", failure.value.details


def test_global_health_accepts_exact_row_bound_and_preserves_foreground(global_observation_bundle):
    bundle, _clock, _opened_modes = global_observation_bundle
    query = "UNWIND range(1, 1000) AS n RETURN n"
    for _ in range(2):
        bundle.global_graph.runtime.execute("RETURN 1")
    with bundle.board.graph_health_observation.scope("board", timeout_seconds=5):
        observed = bundle.global_graph.runtime.execute(query)
    assert observed.rows == tuple((n,) for n in range(1, 1001))
    assert len(bundle.global_graph.runtime.execute("UNWIND range(1, 1001) AS n RETURN n").rows) == 1001


def test_global_health_closes_cursor_at_first_row_overflow(global_observation_bundle, monkeypatch):
    from okto_grafx.engine.database import QueryCursor

    bundle, _clock, _opened_modes = global_observation_bundle
    for _ in range(2):
        bundle.global_graph.runtime.execute("RETURN 1")
    original = QueryCursor.__next__
    observed = []

    def next_row(cursor):
        observed.append(cursor)
        return original(cursor)

    monkeypatch.setattr(QueryCursor, "__next__", next_row)
    with bundle.board.graph_health_observation.scope("board", timeout_seconds=5), pytest.raises(GraphError):
        bundle.global_graph.runtime.execute("UNWIND range(1, 100000) AS n RETURN n")
    assert len(observed) == 1001
    assert all(cursor.closed for cursor in observed)
