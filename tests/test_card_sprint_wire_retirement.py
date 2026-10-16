"""Old Card link requests reject before a use case or persistence operation."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_principal, require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.router import api_router


class NoPersistence:
    def __getattr__(self, name):
        pytest.fail(f"Invalid Card input reached persistence: {name}")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_user] = lambda: "actor"
    app.dependency_overrides[require_principal] = lambda: NoPersistence()
    app.dependency_overrides[get_unit_of_work] = lambda: NoPersistence()
    with TestClient(app) as transport:
        yield transport


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/boards/board/cards"),
    ("PATCH", "/api/v1/cards/card"),
])
@pytest.mark.parametrize("value", [None, "", "legacy"])
def test_retired_card_input_is_explicit_422_before_writer(client, method, path, value):
    response = client.request(method, path, json={"title": "Task", "sprint_id": value})
    assert response.status_code == 422
    assert "card_sprint_link_retired" in response.text


def test_openapi_card_contracts_do_not_publish_sprint_link(client):
    schemas = client.app.openapi()["components"]["schemas"]
    for name in ("CardCreate", "CardUpdate", "CardResponse", "CardSummaryForSpec"):
        assert "sprint_id" not in schemas[name]["properties"]
