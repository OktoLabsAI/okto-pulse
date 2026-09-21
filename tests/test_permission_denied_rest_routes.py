"""REST mappings for permission-aware Card mutations."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api import boards as boards_api
from okto_pulse.community.api import cards as cards_api
from okto_pulse.community.api.auth_deps import (
    get_realm_id,
    require_principal,
    require_user,
)
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import PermissionDeniedError
from okto_pulse.core.ports.authentication import Principal


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(boards_api.router, prefix="/boards")
    app.include_router(cards_api.router, prefix="/cards")
    app.dependency_overrides[require_user] = lambda: "actor"
    app.dependency_overrides[get_realm_id] = lambda: "local"
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject="actor",
        realm_id="local",
        actor_kind="human",
    )
    app.dependency_overrides[get_unit_of_work] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


_CARD_ROUTES = (
    (
        boards_api.CreateCardInBoardUseCase,
        "POST",
        "/boards/board-1/cards",
        {"title": "Card"},
    ),
    (
        cards_api.UpdateCardUseCase,
        "PATCH",
        "/cards/card-1",
        {"title": "Updated"},
    ),
    (cards_api.DeleteCardUseCase, "DELETE", "/cards/card-1", None),
    (
        cards_api.AddCardDependencyUseCase,
        "POST",
        "/cards/card-1/dependencies/card-2",
        None,
    ),
    (
        cards_api.RemoveCardDependencyUseCase,
        "DELETE",
        "/cards/card-1/dependencies/card-2",
        None,
    ),
)


@pytest.mark.parametrize(("use_case", "method", "path", "body"), _CARD_ROUTES)
def test_card_mutations_map_permission_denial_to_structured_403(
    client: TestClient,
    use_case: type,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    denial = PermissionDeniedError(
        json.dumps({"required_permission": "card:write", "resource_id": "card-1"})
    )

    with patch.object(use_case, "execute", new=AsyncMock(side_effect=denial)):
        response = client.request(method, path, json=body)

    assert response.status_code == 403
    assert response.json()["detail"]["required_permission"] == "card:write"
