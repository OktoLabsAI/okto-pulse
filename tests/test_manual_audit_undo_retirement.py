"""F4: retired manual undo is absent before authentication or storage access."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.router import api_router
from okto_pulse.community.adapters.sqlalchemy_kg_governance import CommunitySqlAlchemyKGGovernanceStore
from okto_pulse.community.adapters.sqlalchemy_audit_repo import CommunityAuditRepository


@pytest.mark.parametrize("force", [False, True])
def test_undo_is_absent_before_authority_and_storage(force):
    def forbidden():
        pytest.fail("retired undo resolved application dependencies")

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_principal] = forbidden
    app.dependency_overrides[get_unit_of_work] = forbidden
    assert "/api/v1/kg/boards/{board_id}/audit/{session_id}/undo" not in app.openapi()["paths"]
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/kg/boards/board/audit/session/undo",
            params={"force": str(force).lower()}, json={"force": force},
        )
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_no_hidden_undo_store_but_shared_audit_adapter_remains():
    assert not hasattr(CommunitySqlAlchemyKGGovernanceStore, "get_undo_fact")
    assert not hasattr(CommunitySqlAlchemyKGGovernanceStore, "mark_session_undone")
    assert callable(CommunityAuditRepository.mark_audit_undone)
