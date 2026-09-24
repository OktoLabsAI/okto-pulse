"""Health must not turn a refused read participant into checkpoint maintenance."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from okto_grafx.errors import GrafxUnsupportedOperation
from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters.grafx_database_pool import GrafxDatabasePoolError
from okto_pulse.community.adapters.routed_board_graph_composition import _GrafxBoardAccess
from okto_pulse.core.kg import interfaces
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.services.kg_health_service import (
    _GRAPH_HEALTH_PROBE,
    _PARITY_HEALTH_PROBE,
    _aggregate_graph_metrics,
    _run_health_probe_step,
)


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
