"""Retired guideline compatibility routes are absent before dependencies run."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.guidelines import router
from okto_pulse.community.api.deps import get_unit_of_work


@pytest.mark.parametrize('method,path', [
    ('PATCH', '/guidelines/example'),
    ('DELETE', '/guidelines/example'),
    ('PATCH', '/boards/example/guidelines/example'),
    ('POST', '/guidelines/import'),
])
def test_removed_route_cannot_resolve_mutation_dependencies(method, path):
    app = FastAPI()
    app.include_router(router)
    def forbidden():
        pytest.fail('Removed route resolved mutation dependencies')
    app.dependency_overrides[get_unit_of_work] = forbidden
    response = TestClient(app).request(method, path, json={})
    assert response.status_code == 405


def test_only_governed_import_export_routes_remain():
    paths = {route.path for route in router.routes}
    assert '/guidelines/export' not in paths
    assert '/guidelines/import' not in paths
