"""Synchronous REST graph reads cannot occupy the ASGI event-loop thread."""

import asyncio
from contextvars import ContextVar
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from okto_pulse.community.api import kg_routes
from okto_pulse.core.application.use_cases.base import ActorContext


@pytest.fixture
def admission(monkeypatch):
    # This suite isolates dispatch; real permission/CT policy has its own suite.
    monkeypatch.setattr(kg_routes, "_require_kg_operation", AsyncMock())
    monkeypatch.setattr(kg_routes, "_code_traceability_kg_read_access", AsyncMock(return_value=SimpleNamespace(allowed=True)))


def _request(route):
    common = {"board_id": "board", "actor": ActorContext("operator", "rest"), "uow": object()}
    kwargs = {
        "list_nodes": dict(type="Entity", min_confidence=0.2, min_relevance=0.3, limit=7, cursor="cursor", graph_layer="canonical"),
        "get_node_detail": dict(node_id="node"),
        "find_similar": dict(topic="topic", top_k=7, min_similarity=0.4),
        "get_supersedence": dict(decision_id="node"),
        "find_contradictions": dict(node_id="node", limit=7),
        "cypher_query": dict(cypher="RETURN $value", params={"value": 7}, max_rows=7, timeout_ms=321, include_working=False),
    }[route]
    return getattr(kg_routes, route)(**common, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("route,methods", [
    ("list_nodes", ["get_all_nodes", "count_all_nodes"]),
    ("get_node_detail", ["get_node_detail"]),
    ("find_similar", ["find_similar_decisions"]),
    ("get_supersedence", ["get_supersedence_chain"]),
    ("find_contradictions", ["find_contradictions"]),
    ("cypher_query", ["execute_cypher_read_only"]),
])
async def test_all_read_routes_dispatch_preserving_context_and_arguments(admission, monkeypatch, route, methods):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    request_scope = ContextVar("request_scope", default=None)
    token = request_scope.set("board-authority")
    observed = []
    result = {"id": "node"}

    def operation(name):
        def run(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            assert request_scope.get() == "board-authority"
            # A queued loop callback must run while the native call is pending.
            release = threading.Event()
            loop.call_soon_threadsafe(release.set)
            assert release.wait(2), "event loop blocked behind native read"
            observed.append((name, threading.get_ident(), args, kwargs))
            if name == "count_all_nodes":
                return 1
            if name in {"get_node_detail", "get_supersedence_chain", "execute_cypher_read_only"}:
                return result
            return [result]
        return run

    service = SimpleNamespace(**{name: operation(name) for name in methods})
    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: service)
    if route == "cypher_query":
        monkeypatch.setattr(kg_routes, "execute_cypher_read_only", operation(methods[0]))
    try:
        payload = await _request(route)
    finally:
        request_scope.reset(token)
    assert [call[0] for call in observed] == methods
    assert len({call[1] for call in observed}) == 1
    assert all(call[2][0] == "board" for call in observed)
    if route == "list_nodes":
        assert payload["nodes"] == [result] and payload["total_hint"] == 1
        assert observed[0][3] == dict(min_confidence=0.2, min_relevance=0.3, max_rows=7, cursor="cursor", node_type="Entity", graph_layer="canonical")
        assert observed[1][3] == dict(min_confidence=0.2, min_relevance=0.3, node_type="Entity", graph_layer="canonical")
    elif route == "find_similar":
        assert payload == {"results": [result], "total": 1}
        assert observed[0][2] == ("board", "topic")
        assert observed[0][3] == dict(top_k=7, min_similarity=0.4)
    elif route == "find_contradictions":
        assert payload == {"contradictions": [result], "total": 1}
        assert observed[0][3] == dict(node_id="node", max_rows=7)
    else:
        assert payload is result
        if route == "cypher_query":
            assert observed[0][2] == ("board", "RETURN $value", {"value": 7})
            assert observed[0][3] == dict(max_rows=7, timeout_ms=321, include_working=False)
        else:
            assert observed[0][2] == ("board", "node")


@pytest.mark.asyncio
async def test_cancelled_read_drains_native_ownership_before_returning(admission, monkeypatch):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    closed = threading.Event()

    def native(*_args, **_kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5), "test did not release native read"
            return {"id": "node"}
        finally:
            closed.set()

    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: SimpleNamespace(get_node_detail=native))
    task = asyncio.create_task(_request("get_node_detail"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not closed.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
