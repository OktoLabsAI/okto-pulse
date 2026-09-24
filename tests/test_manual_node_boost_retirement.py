"""Manual relevance tuning is absent before authorization, graph or audit work."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.router import api_router
from okto_pulse.community.adapters.sqlalchemy_kg_governance import CommunitySqlAlchemyKGGovernanceStore


def test_retired_manual_boost_is_404_without_resolving_dependencies():
    def forbidden():
        pytest.fail("retired boost resolved authority or storage")

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_principal] = forbidden
    app.dependency_overrides[get_unit_of_work] = forbidden
    assert "/api/v1/kg/boards/{board_id}/nodes/{node_id}/boost" not in app.openapi()["paths"]
    with TestClient(app) as client:
        response = client.post("/api/v1/kg/boards/board/nodes/node/boost")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert not hasattr(CommunitySqlAlchemyKGGovernanceStore, "add_boost_audit")
