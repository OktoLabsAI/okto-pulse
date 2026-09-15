from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import threading

import pytest

from okto_pulse.community.api import kg_routes


@pytest.mark.asyncio
async def test_subgraph_phase_timings_do_not_change_results(monkeypatch, caplog):
    nodes = [{"id": "node-1", "node_type": "Entity", "title": "private title"}]
    edges = [{"source": "node-1", "target": "node-1"}]
    metadata = {"edge_read_status": "ok", "edge_tables_failed": 0}
    monkeypatch.setattr(kg_routes, "_code_traceability_kg_read_access",
                        AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: SimpleNamespace(
        get_all_nodes=lambda *args, **kwargs: nodes))
    monkeypatch.setattr(kg_routes, "_fetch_edges_for_nodes", lambda *args, **kwargs: (edges, metadata))

    async def run(operation, **kwargs):
        return operation()

    monkeypatch.setattr(kg_routes, "run_blocking_graph_io", run)
    caplog.set_level("INFO", logger=kg_routes.logger.name)
    result = await kg_routes.get_subgraph(
        "board-test", center="", depth=2, limit=500, cursor="",
        min_relevance=0.0, type="", graph_layer="canonical", actor=object(), uow=object())
    assert result["nodes"] == nodes
    assert result["edges"] == edges
    records = [r.getMessage() for r in caplog.records if "kg.read.phase" in r.getMessage()]
    assert len(records) == 4
    for record, phase in zip(records, ("authority", "dispatch", "nodes", "edges"), strict=True):
        assert f"operation=subgraph phase={phase} duration_ms=" in record
    assert "private title" not in caplog.text
    assert "node-1" not in caplog.text


@pytest.mark.asyncio
async def test_authority_failure_does_not_reach_graph_or_log_success(monkeypatch, caplog):
    failure = RuntimeError("private failure content")
    monkeypatch.setattr(kg_routes, "_code_traceability_kg_read_access", AsyncMock(side_effect=failure))
    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: object())
    caplog.set_level("INFO", logger=kg_routes.logger.name)
    with pytest.raises(RuntimeError) as caught:
        await kg_routes.get_subgraph(
            "board-test", center="", depth=2, limit=500, cursor="",
            min_relevance=0.0, type="", graph_layer="canonical", actor=object(), uow=object())
    assert caught.value is failure
    assert "kg.read.phase" not in caplog.text
    assert "private failure content" not in caplog.text


def test_phase_timer_uses_elapsed_monotonic_time(monkeypatch, caplog):
    monkeypatch.setattr(kg_routes, "perf_counter", lambda: 10.125)
    caplog.set_level("INFO", logger=kg_routes.logger.name)
    assert kg_routes._record_read_phase("board-test", "stats", "schema", 10.0) == 10.125
    assert "phase=schema duration_ms=125.0" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["cursor", "center", "edges", "authority"])
async def test_only_paged_node_value_errors_are_cursor_failures(monkeypatch, failure_phase):
    failure = ValueError("test failure")
    loop_thread = threading.get_ident()
    monkeypatch.setattr(kg_routes, "_require_kg_operation", AsyncMock())

    def fail(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        raise failure

    monkeypatch.setattr(kg_routes, "_code_traceability_kg_read_access", AsyncMock(
        return_value=SimpleNamespace(allowed=True),
        side_effect=failure if failure_phase == "authority" else None))
    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: SimpleNamespace(
        get_all_nodes=fail if failure_phase == "cursor" else lambda *a, **kw: [],
        get_related_context=fail))
    monkeypatch.setattr(kg_routes, "_fetch_edges_for_nodes", fail)

    async def invoke():
        return await kg_routes.get_subgraph(
            "board-test", center="center-id" if failure_phase == "center" else "",
            depth=2, limit=500, cursor="invalid", min_relevance=0.0,
            type="", graph_layer="canonical", actor=object(), uow=object())

    if failure_phase == "cursor":
        result = await invoke()
        assert result.status_code == 410
        assert json.loads(result.body)["type"] == "/errors/invalid_cursor"
    else:
        with pytest.raises(ValueError) as caught:
            await invoke()
        assert caught.value is failure


@pytest.mark.asyncio
async def test_stats_phases_run_off_loop_and_preserve_visibility(monkeypatch, caplog):
    loop_thread = threading.get_ident()
    calls = []

    def read(name, result):
        def operation(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            if name != "schema":
                assert kwargs["include_code_traceability"] is False
            calls.append(name)
            return result
        return operation

    monkeypatch.setattr(kg_routes, "_code_traceability_kg_read_access",
                        AsyncMock(return_value=SimpleNamespace(allowed=False)))
    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: SimpleNamespace(
        get_schema_version=read("schema", 7),
        get_all_nodes=read("nodes", [{"source_confidence": 0.8, "relevance_score": 0.6}]),
    ))
    monkeypatch.setattr(kg_routes, "_count_nodes_by_type", read("node_counts", {"Entity": 1}))
    monkeypatch.setattr(kg_routes, "_count_edges_by_type", read("edge_counts", (
        {"relates_to": 2}, {"edge_read_status": "ok"})))
    caplog.set_level("INFO", logger=kg_routes.logger.name)
    result = await kg_routes.get_stats(
        "board-test", min_relevance=0.0, graph_layer="canonical", actor=object(), uow=object())
    assert calls == ["schema", "nodes", "node_counts", "edge_counts"]
    assert result["avg_confidence"] == 0.8
    assert result["avg_relevance"] == 0.6
    assert result["node_counts_by_type"] == {"Entity": 1}
    assert result["edge_counts_by_type"] == {"relates_to": 2}
    phases = [record.getMessage().split("phase=")[1].split()[0]
              for record in caplog.records if "kg.read.phase" in record.getMessage()]
    assert phases == ["authority", "dispatch", "schema", "nodes", "node_counts", "edge_counts"]
