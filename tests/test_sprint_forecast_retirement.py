"""F3/F5: retired forecast endpoints cannot resolve a live projection or UoW."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters import sqlalchemy_analytics_evidence
from okto_pulse.community.api import analytics_transport
from okto_pulse.community.api.analytics import router
from okto_pulse.community.api.deps import get_unit_of_work


def application():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def forbidden_uow():
        raise AssertionError("a retired forecast must not resolve persistence")

    app.dependency_overrides[get_unit_of_work] = forbidden_uow
    return app


@pytest.mark.parametrize("suffix", ["", "/export"])
@pytest.mark.parametrize("board", ["visible-board", "foreign-board"])
def test_retired_forecast_is_not_a_live_route(board, suffix):
    client = TestClient(application())
    response = client.get(
        f"/api/v1/boards/{board}/analytics/delivery-forecast{suffix}",
        params={"sprint_ids": "old-sprint", "horizon": "next_sprint"},
    )
    assert response.status_code == 404


def test_forecast_is_absent_from_openapi_dtos_and_adapter():
    schema = application().openapi()
    assert not any("delivery-forecast" in path for path in schema["paths"])
    assert not any("Forecast" in name for name in schema["components"]["schemas"])
    assert not hasattr(analytics_transport, "CanonicalDeliveryForecastResponseDTO")
    assert not hasattr(
        sqlalchemy_analytics_evidence, "CommunitySqlAlchemyDeliveryForecastEvidence"
    )
    assert not hasattr(CommunityRelationalApplicationAdapter, "delivery_forecast_read")
    assert hasattr(CommunityRelationalApplicationAdapter, "board_kg_analytics_read")
