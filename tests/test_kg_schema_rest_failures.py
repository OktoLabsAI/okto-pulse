"""Schema discovery preserves neutral errors and never blocks the API loop."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from okto_pulse.community.api import kg_routes
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphError,
    GraphInvalidQuery,
    GraphUnavailable,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("board_id", ["", "test-board"])
@pytest.mark.parametrize("error_type,status", [
    (GraphCapabilityUnavailable, 503),
    (GraphUnavailable, 503),
    (GraphCorruption, 503),
    (GraphInvalidQuery, 400),
    (GraphError, 500),
])
async def test_schema_provider_errors_are_problem_details(
    monkeypatch, board_id, error_type, status,
):
    authorized = AsyncMock()
    board_access = AsyncMock()
    monkeypatch.setattr(kg_routes, "_require_kg_operation", authorized)
    monkeypatch.setattr(kg_routes, "_ensure_board_access", board_access)
    calls = []

    def refused(board, *, include_internal):
        calls.append((board, include_internal))
        raise error_type("Schema provider is unavailable")

    monkeypatch.setattr(kg_routes, "get_schema_info", refused)
    response = await kg_routes.schema_info(
        board_id=board_id, include_internal=True,
        actor=ActorContext("test-user", "rest"), uow=SimpleNamespace(),
    )
    assert response.status_code == status
    assert response.media_type == "application/problem+json"
    body = json.loads(response.body)
    assert body["type"] == f"/errors/{error_type.code}"
    assert body["title"] == error_type.code
    assert body["status"] == status
    assert calls == [(board_id or "default", True)]
    assert authorized.await_count == 2
    assert board_access.await_count == int(bool(board_id))


@pytest.mark.asyncio
async def test_schema_introspection_runs_off_loop_and_preserves_result(monkeypatch):
    monkeypatch.setattr(kg_routes, "_require_kg_operation", AsyncMock())
    monkeypatch.setattr(kg_routes, "_ensure_board_access", AsyncMock())
    event_loop_thread = threading.get_ident()
    payload = {"schema_version": "test", "stable_node_types": [{"name": "Decision"}]}

    def introspect(board, *, include_internal):
        assert threading.get_ident() != event_loop_thread
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert (board, include_internal) == ("test-board", False)
        return payload

    monkeypatch.setattr(kg_routes, "get_schema_info", introspect)
    assert await kg_routes.schema_info(
        board_id="test-board", include_internal=False,
        actor=ActorContext("test-user", "rest"), uow=SimpleNamespace(),
    ) is payload


def test_neutral_memory_pressure_retains_bounded_retry_header():
    class MemoryPressure(GraphUnavailable):
        code = "graph_memory_pressure"

    response = kg_routes._graph_problem(
        MemoryPressure("Allocation refused", details={"retry_after_ms": 1501}),
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
