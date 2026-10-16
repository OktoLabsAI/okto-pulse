"""Dedicated Sprint routes are absent, including direct legacy calls (BASE T33/T34)."""

import importlib.util

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.router import api_router


ROUTES = (
    ("GET", "/boards/board/sprints"),
    ("GET", "/boards/board/specs/spec/sprints"),
    ("POST", "/boards/board/specs/spec/sprints"),
    ("GET", "/boards/board/specs/spec/sprints/suggest"),
    ("GET", "/sprints/source"),
    ("PATCH", "/sprints/source"),
    ("DELETE", "/sprints/source"),
    ("POST", "/sprints/source/move"),
    ("POST", "/sprints/source/evaluations"),
    ("POST", "/sprints/source/assign-tasks"),
    ("POST", "/sprints/source/unassign-tasks"),
    ("GET", "/sprints/source/history"),
)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api_router)

    def forbidden_dependency():
        pytest.fail("Retired route reached authentication or persistence")

    app.dependency_overrides[require_user] = forbidden_dependency
    app.dependency_overrides[get_unit_of_work] = forbidden_dependency
    with TestClient(app) as transport:
        yield transport


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("payload", ({"title": "Legacy", "spec_id": "spec", "card_ids": ["card"]}, {}))
def test_direct_legacy_route_is_missing_before_dependencies(client, method, path, payload):
    response = client.request(method, f"/api/v1{path}", json=payload)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_dedicated_router_and_request_schemas_are_not_shipped(client):
    assert importlib.util.find_spec("okto_pulse.community.api.sprints") is None
    document = client.app.openapi()
    for path in document["paths"]:
        assert not path.startswith("/api/v1/sprints/")
        assert not path.startswith("/api/v1/boards/{board_id}/specs/{spec_id}/sprints")
        assert path != "/api/v1/boards/{board_id}/sprints"
    # Analytics/generic Sprint consumers are separate remaining cleanup; do not
    # claim their schema absence from this dedicated CRUD retirement.
    for schema in ("SprintCreate", "SprintUpdate", "SprintMove"):
        assert schema not in document["components"]["schemas"]
