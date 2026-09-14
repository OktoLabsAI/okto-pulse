"""Selected-node source navigation must preserve the KG and entity read gates."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import threading

import httpx
import pytest
from fastapi import FastAPI

from okto_pulse.community.api import kg_routes
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.permissions import PermissionSet


def make_app(monkeypatch, *, ct_allowed=True, owner_board="board-a", node_present=True):
    seen = []
    def node_detail(board_id, node_id, **kwargs):
        seen.append((board_id, node_id, kwargs.get("include_code_traceability", True), threading.current_thread().name))
        if not node_present or kwargs.get("include_code_traceability") is False:
            return None
        return {"id": node_id, "node_type": "Entity", "kind_of": "implementation_target", "source_artifact_ref": "implementation_target:target-a"}
    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: NS(get_node_detail=node_detail))
    uow = NS(
        boards=NS(get=AsyncMock(return_value=NS(owner_id="reader", realm_id=None))),
        services=NS(
            code_traceability=NS(get_target=AsyncMock(return_value=NS(board_id="board-a", card_id="test-card"))),
            cards=NS(get_card=AsyncMock(return_value=NS(board_id=owner_board, title="Test the implementation", card_type="test"))),
        ),
        commit=AsyncMock(),
    )
    actor = ActorContext("reader", "system", board_id="board-a", permissions=None if ct_allowed else PermissionSet({}))
    app = FastAPI()
    app.include_router(kg_routes.router, prefix="/api/v1")
    app.dependency_overrides[kg_routes.require_kg_board_actor] = lambda: actor
    app.dependency_overrides[kg_routes.get_unit_of_work] = lambda: uow
    return app, uow, seen


@pytest.mark.asyncio
async def test_source_endpoint_returns_authorized_owner_through_typed_contract(monkeypatch):
    app, uow, seen = make_app(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/kg/boards/board-a/nodes/n1/source")
    assert response.status_code == 200
    assert response.json() == {
        "status": "resolved", "source_artifact_ref": "implementation_target:target-a",
        "target": {"board_id": "board-a", "entity_type": "card", "entity_kind": "test",
                   "entity_id": "test-card", "title": "Test the implementation", "source_version": None},
    }
    assert len(seen) == 1 and seen[0][:3] == ("board-a", "n1", True)
    assert seen[0][3] != threading.current_thread().name
    uow.services.code_traceability.get_target.assert_awaited_once_with(board_id="board-a", target_id="target-a")
    uow.commit.assert_not_awaited()
    assert "KGNodeSourceResult" in app.openapi()["components"]["schemas"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ct_allowed,node_present", [(False, True), (True, False)])
async def test_hidden_and_missing_nodes_are_not_resolved(monkeypatch, ct_allowed, node_present):
    app, uow, seen = make_app(monkeypatch, ct_allowed=ct_allowed, node_present=node_present)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/kg/boards/board-a/nodes/n1/source")
    assert response.status_code == 404
    uow.services.code_traceability.get_target.assert_not_awaited()
    assert seen[0][2] == ct_allowed


@pytest.mark.asyncio
async def test_wrong_board_owner_has_no_metadata_leak(monkeypatch):
    app, _, _ = make_app(monkeypatch, owner_board="board-b")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/kg/boards/board-a/nodes/n1/source")
    assert response.json()["status"] == "unavailable"
    assert response.json()["target"] is None
    assert "Test the implementation" not in response.text
