"""F4: graph storage administration has no public exploration fallback."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api import kg_exploration as exploration
from okto_pulse.community.api.router import api_router


@pytest.mark.parametrize("board", ["missing", "foreign", "owned"])
@pytest.mark.parametrize("suffix", ["search/prepare", "history/activate", "history/prune"])
@pytest.mark.parametrize("body", [{}, {
    "node_type": "Decision", "node_types": ["Decision"], "reason": "legacy request",
    "acknowledge_one_way": True, "acknowledge_history_loss": True,
    "before": "a" * 32 + ":" + "0" * 15 + "1",
}])
def test_removed_administration_is_absent_before_authority_and_storage(monkeypatch, board, suffix, body):
    def forbidden(*args, **kwargs):
        pytest.fail("Retired exploration write reached authority or storage")

    monkeypatch.setattr(exploration, "get_kg_registry", forbidden)
    app = FastAPI()
    app.include_router(api_router)
    for dependency in (exploration.kg.require_kg_board_actor,
                       exploration.kg.require_kg_board_writer_actor,
                       exploration.kg.get_unit_of_work):
        app.dependency_overrides[dependency] = forbidden
    with TestClient(app) as client:
        response = client.post(f"/api/v1/kg/boards/{board}/exploration/{suffix}", json=body)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert f"/api/v1/kg/boards/{{board_id}}/exploration/{suffix}" not in app.openapi()["paths"]


def test_handlers_models_and_admin_dispatch_are_removed():
    for name in ("prepare_search", "activate_history", "history_prune", "invoke_admin",
                 "PrepareRequest", "HistoryActivation", "HistoryPrune"):
        assert not hasattr(exploration, name)
    app = FastAPI()
    app.include_router(api_router)
    schemas = app.openapi()["components"]["schemas"]
    assert not {"PrepareRequest", "HistoryActivation", "HistoryPrune"}.intersection(schemas)


@pytest.mark.parametrize("board", ["missing", "foreign", "owned"])
@pytest.mark.parametrize("body", [{}, {"all_boards": True, "force": True}])
def test_schema_migration_has_no_public_rest_fallback(monkeypatch, board, body):
    def forbidden(*args, **kwargs):
        pytest.fail("Retired schema migration reached authority or storage")

    kg = exploration.kg
    assert not hasattr(kg, "post_migrate_schema")
    assert not hasattr(kg, "MigrateSchemaResponse")
    app = FastAPI()
    app.include_router(api_router)
    for dependency in (kg.require_kg_board_actor, kg.require_kg_board_writer_actor, kg.get_unit_of_work):
        app.dependency_overrides[dependency] = forbidden
    with TestClient(app) as client:
        response = client.post(f"/api/v1/kg/{board}/migrate-schema", json=body)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "/api/v1/kg/{board_id}/migrate-schema" not in app.openapi()["paths"]
    assert "MigrateSchemaResponse" not in app.openapi()["components"]["schemas"]
