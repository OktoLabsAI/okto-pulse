"""Removed analytics endpoints and adapters cannot bypass historical archives."""
from importlib.util import find_spec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.analytics import router
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.adapters.sqlalchemy_analytics_read import CommunitySqlAlchemyAnalyticsReader
from okto_pulse.core.ports.analytics_read import AnalyticsQuery


@pytest.mark.parametrize('board_id', ['visible', 'foreign'])
@pytest.mark.parametrize('suffix', ['sprints', 'sprint/legacy', 'sprints/export', 'sprint/legacy/export'])
def test_sprint_analytics_routes_are_absent_without_opening_uow(board_id, suffix):
    app = FastAPI()
    app.include_router(router)
    def forbidden():
        raise AssertionError('retired route must not open a UoW')
    app.dependency_overrides[require_user] = lambda: 'owner'
    app.dependency_overrides[get_unit_of_work] = forbidden
    with TestClient(app) as client:
        assert client.get(f'/boards/{board_id}/analytics/{suffix}').status_code == 404
    assert not any('/analytics/sprint' in path for path in app.openapi()['paths'])


@pytest.mark.asyncio
async def test_active_analytics_reader_and_baseline_store_are_retired():
    assert find_spec('okto_pulse.community.adapters.sqlalchemy_sprint_activation_baseline') is None
    with pytest.raises(ValueError, match='unsupported_analytics_entity:sprint'):
        await CommunitySqlAlchemyAnalyticsReader().list(object(), AnalyticsQuery(entity='sprint'))
