"""F4: tuning has no public REST route, irrespective of caller or payload."""
from importlib.util import find_spec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.router import api_router


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("payload", [{}, {"kg_grafx_options": {"max_result_rows": 100}}, {"kg_decay_tick_interval_minutes": 5}])
def test_tuning_route_is_absent_before_authority_storage_and_effects(method, payload):
    def forbidden():
        pytest.fail("Retired tuning resolved an application dependency")

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_principal] = forbidden
    app.dependency_overrides[get_unit_of_work] = forbidden
    assert "/api/v1/settings/runtime" not in app.openapi()["paths"]
    with TestClient(app) as client:
        response = client.request(method, "/api/v1/settings/runtime", json=payload)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_retired_router_is_not_distributed():
    assert find_spec("okto_pulse.community.api.settings") is None
