"""Health must not turn a refused read participant into checkpoint maintenance."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from okto_grafx.errors import GrafxUnsupportedOperation
from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters.grafx_database_pool import GrafxDatabasePoolError
from okto_pulse.community.adapters.routed_board_graph_composition import _GrafxBoardAccess
from okto_pulse.community.adapters import routed_board_graph_composition as composition
from okto_pulse.core.kg import interfaces
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.services.kg_health_service import (
    _GRAPH_HEALTH_PROBE,
    _PARITY_HEALTH_PROBE,
    _aggregate_graph_metrics,
    _run_health_probe_step,
)
from okto_pulse.core.services import kg_health_service as health


@pytest.fixture
def routed_access(tmp_path):
    effects = []
    snapshot = SimpleNamespace(scope_id="board", backend="grafx", active_path=tmp_path / "graph",
                               page_size=4096, binding_sha256="c" * 64)

    class Database:
        def checkpoint(self):
            effects.append("checkpoint")

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(columns=["relevance_score"], rows=[[0.7]])

        @contextmanager
        def transaction(self, mode):
            assert mode == "read"
            yield self

    database = Database()

    class ReadPool:
        read_only = True

        def get(self, *_args, **_kwargs):
            if "checkpoint" not in effects:
                cause = GrafxUnsupportedOperation("checkpoint required", field="read_only_consistency")
                raise GrafxDatabasePoolError("read-only open refused", reason="pool_open_failed") from cause
            return database

    access = _GrafxBoardAccess(
        SimpleNamespace(current_board_snapshot=lambda *_args, **_kwargs: snapshot,
                        admit_grafx_route=lambda *_args, **_kwargs: None,
                        revalidate_snapshot=lambda *_args, **_kwargs: snapshot),
        SimpleNamespace(get=lambda *_args, **_kwargs: database), SimpleNamespace(),
        SimpleNamespace(close_rollback_before_write_if_active=lambda *_args: effects.append("close_rollback")),
        configured_page_size=4096, connect=None, read_pools=(ReadPool(),),
    )
    return access, effects


@pytest.mark.parametrize("probe_name", [_GRAPH_HEALTH_PROBE, _PARITY_HEALTH_PROBE])
def test_health_graph_query_does_not_checkpoint_or_close_rollback_on_read_join(routed_access, monkeypatch, probe_name):
    access, effects = routed_access
    executor = CommunityGrafxCypherExecutor(access.read_database, read_database_scope=access.read_database_scope)
    monkeypatch.setattr(interfaces, "get_kg_registry", lambda: SimpleNamespace(
        cypher_executor=executor, graph_health_observation=access))
    for _ in range(3):
        result = _run_health_probe_step(
            probe_name=probe_name,
            board_id="board",
            step_name="graph_metrics",
            build=lambda: _aggregate_graph_metrics("board"),
        )
        assert result["status"] == "unavailable"
    assert effects == [], f"Health attempted maintenance: {effects}"


def test_observation_context_restores_foreground_recovery_after_nested_failure(routed_access):
    access, effects = routed_access
    with access.scope("board"):
        with pytest.raises(ValueError), access.scope("board"):
            raise ValueError("probe failed")
        with pytest.raises(GrafxDatabasePoolError):
            access.read_database("board")
        assert effects == []
    assert access.read_database("board") is not None
    assert effects == ["close_rollback", "checkpoint"]


def test_health_context_does_not_disable_another_threads_foreground_recovery(routed_access):
    access, effects = routed_access
    with ThreadPoolExecutor(max_workers=1) as worker, access.scope("board"):
        with pytest.raises(GrafxDatabasePoolError):
            access.read_database("board")
        assert effects == []
        assert worker.submit(access.read_database, "board").result(timeout=5) is not None
        assert effects == ["close_rollback", "checkpoint"]
        # Once foreground recovery succeeds, Health can observe the reader.
        assert access.read_database("board") is not None
        assert effects == ["close_rollback", "checkpoint"]


@pytest.mark.parametrize("method", ["write_fence", "runtime_fence"])
def test_health_refuses_fences_before_mutation(routed_access, method):
    access, effects = routed_access
    with access.scope("board"), pytest.raises(GraphCapabilityUnavailable):
        getattr(access, method)("board", "probe")
    assert effects == []


def test_health_never_falls_back_to_a_writable_pool(routed_access):
    access, effects = routed_access
    access.read_pools = ()
    with access.scope("board"), pytest.raises(GraphCapabilityUnavailable):
        access.database("board")
    assert effects == []


def test_health_writable_resolver_uses_only_reader(routed_access):
    access, effects = routed_access
    with access.scope("board"), pytest.raises(GrafxDatabasePoolError):
        access.database("board")
    assert effects == []


@pytest.mark.parametrize("budget", [True, False, 0, -1, 5.1, float("inf"), float("nan"), "1"])
def test_invalid_timeout_cannot_enter_observation(routed_access, budget):
    access, effects = routed_access
    with pytest.raises(ValueError), access.scope("board", timeout_seconds=budget):
        pytest.fail("invalid scope entered")
    assert effects == []
    assert access.health_query_timeout("board") is None


def test_nested_deadline_cannot_extend_parent_and_expiry_prevents_open(routed_access, monkeypatch):
    access, effects = routed_access
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(composition, "time", SimpleNamespace(monotonic=lambda: clock.now))
    with access.scope("board", timeout_seconds=0.1):
        clock.now += 0.05
        with access.scope("board", timeout_seconds=5):
            assert access.health_query_timeout("board") == pytest.approx(0.05)
            clock.now += 0.06
            with pytest.raises(GraphCapabilityUnavailable) as failure:
                access.read_database("board")
            assert failure.value.details["reason"] == "graph_health_deadline_exceeded"
    assert effects == []
    assert access.health_query_timeout("board") is None


def test_batch_shares_deadline_and_never_returns_a_partial_prefix(routed_access, monkeypatch):
    access, effects = routed_access
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(composition, "time", SimpleNamespace(monotonic=lambda: clock.now))
    calls = []

    class Reader:
        @contextmanager
        def transaction(self, mode):
            assert mode == "read"
            yield self

        def execute(self, query, params, *, timeout_seconds):
            calls.append(timeout_seconds)
            clock.now += 0.1
            return SimpleNamespace(columns=["n"], rows=[[1]])

    access.read_pools = (SimpleNamespace(get=lambda *args, **kwargs: Reader(), read_only=True),)
    executor = CommunityGrafxCypherExecutor(
        access.read_database, read_database_scope=access.read_database_scope,
        query_timeout=access.health_query_timeout,
    )
    with access.scope("board", timeout_seconds=0.15), pytest.raises(GraphCapabilityUnavailable):
        executor.execute_read_only_batch("board", [("RETURN 1", {}, 10)] * 3)
    assert calls == pytest.approx([0.15, 0.05])
    assert effects == []


def test_managed_graph_worker_shares_one_deadline_across_steps(routed_access, monkeypatch):
    access, _effects = routed_access
    clock = SimpleNamespace(now=10.0)
    observed = []
    monkeypatch.setattr(composition, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(interfaces, "get_kg_registry", lambda: SimpleNamespace(
        graph_health_observation=access))

    def read():
        observed.append(access.health_query_timeout("worker-deadline-board"))
        clock.now += 0.3

    def build():
        for name in ("metrics", "schema"):
            health._run_health_probe_step(
                probe_name=health._GRAPH_HEALTH_PROBE, board_id="worker-deadline-board",
                step_name=name, build=read,
            )

    request = health._HealthProbeRequest(
        name=health._GRAPH_HEALTH_PROBE, board_id="worker-deadline-board",
        generation_id="deadline-test", build=build, fallback={}, ttl_s=0,
    )
    _initial, future = health._ensure_health_probe(request)
    assert future is not None
    future.result(timeout=5)
    assert observed == pytest.approx([0.35, 0.05])
