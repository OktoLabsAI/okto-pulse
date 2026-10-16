"""F4: public replay and queue readers are absent, including authenticated routes."""
from importlib.util import find_spec
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from okto_pulse.community.api.router import api_router
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.auth_deps import require_user


@pytest.mark.parametrize("board", ["missing", "foreign", "owned"])
@pytest.mark.parametrize("method,suffix", [
    ("GET", "pending"), ("GET", "pending/tree"),
    ("POST", "pending/queue-1/retry"), ("POST", "deterministic-projection/repair"),
])
def test_manual_queue_routes_are_absent_before_auth_and_storage(board, method, suffix):
    def forbidden():
        pytest.fail("Retired operation resolved authority or storage")
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_user] = forbidden
    app.dependency_overrides[get_unit_of_work] = forbidden
    assert not any("/pending" in p or "deterministic-projection" in p for p in app.openapi()["paths"] if p.startswith("/api/v1/kg/boards/"))
    with TestClient(app) as client:
        response = client.request(method, f"/api/v1/kg/boards/{board}/{suffix}", json={"recursive": True, "spec_ids": ["foreign"]})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_repair_module_and_adapter_controls_are_not_distributed():
    from okto_pulse.community.adapters.kg_operational import CommunitySqlAlchemyKGOperationalReadModel, CommunitySqlAlchemyKGWorkerQueue
    assert find_spec("okto_pulse.community.api.kg_projection_repair") is None
    assert not hasattr(CommunitySqlAlchemyKGOperationalReadModel, "build_pending_tree")
    assert not hasattr(CommunitySqlAlchemyKGOperationalReadModel, "list_pending_entries")
    assert not hasattr(CommunitySqlAlchemyKGWorkerQueue, "retry_pending_entry")
    assert hasattr(CommunitySqlAlchemyKGWorkerQueue, "reprocess_dead_letter_rows")
