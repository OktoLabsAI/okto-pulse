from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from okto_pulse.community.api import analytics
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import ActorContext, EntityNotFoundError
from okto_pulse.core.domain.delivery_evidence import DeliveryScope
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption, GraphQueryTimeout
from okto_pulse.core.ports.spec_coverage_query import SpecCoverageSnapshot, SpecCoverageGraphFacts
from okto_pulse.core.services.decision_impact import build_decision_impact_scope, project_decision_impact

URL = '/boards/board/specs/spec/decisions/decision/impact'
FLAGS = ['board.read', 'spec.entity.read', 'card.entity.read', 'spec.tests.read',
    'spec.integration_requirements.read', 'spec.observability_requirements.read', 'kg.query.related_context']


@pytest_asyncio.fixture
async def api(monkeypatch):
    actor = ActorContext('agent', 'mcp', actor_kind='agent', realm_id='local', board_id='board', permissions=list(FLAGS))
    monkeypatch.setattr(RESTAdapterContract, 'actor', lambda *args, **kwargs: actor)
    board = SimpleNamespace(id='board', owner_id='owner', realm_id='local', settings={'kg_query_timeout_ms': 700})
    calls = []
    async def aggregate(query, **options):
        calls.append((query, options))
        fields = {field: [] for _, field in COLLECTIONS}
        fields.update(decisions=[{'id': 'decision', 'title': 'Choice', 'status': 'active', 'linked_requirements': ['fr1', 'fr2']}],
            functional_requirements=[{'id': 'fr1', 'text': 'First'}, {'id': 'fr2', 'text': 'Second'}])
        spec = SimpleNamespace(id=query.spec_id, board_id=query.board_id, edition=1, test_scenarios=[], **fields)
        facts = SpecCoverageSnapshot(DeliveryScope(query.board_id, query.spec_id, 1), query.actor_scope_ref,
            'revision', datetime.now(timezone.utc), spec, (), True, None, 'restricted')
        scope = build_decision_impact_scope(facts)
        return project_decision_impact(query, facts, scope, SpecCoverageGraphFacts(state='unavailable'))
    operation = AsyncMock(side_effect=aggregate)
    uow = SimpleNamespace(boards=SimpleNamespace(get=AsyncMock(return_value=board)),
        services=SimpleNamespace(analytics=SimpleNamespace(decision_impact=operation)))
    app = FastAPI(); app.include_router(analytics.router)
    app.dependency_overrides[require_user] = lambda: 'user'
    app.dependency_overrides[get_unit_of_work] = lambda: uow
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield SimpleNamespace(client=client, actor=actor, operation=operation, calls=calls)


@pytest.mark.asyncio
async def test_pagination_keeps_global_counts_and_clamps_timeout_without_proof_grant(api):
    response = await api.client.get(URL, params={'limit': 1, 'timeout_ms': 30000})
    assert response.status_code == 200, response.text
    first = response.json()
    assert api.calls[0][1] == {'timeout_ms': 700}
    assert not api.calls[0][0].source_query().read_delivery
    assert first['counts']['confirmed_link_targets'] == 2
    second = await api.client.get(URL, params={'limit': 1, 'cursor': first['next_cursor']})
    assert second.status_code == 200, second.text
    assert second.json()['counts'] == first['counts']
    assert second.json()['items'][0]['target_ref'] != first['items'][0]['target_ref']
    assert second.json()['projection_freshness']['state'] == 'unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', FLAGS)
async def test_grants_precede_read_and_counts(api, flag):
    api.actor.permissions.remove(flag)
    assert (await api.client.get(URL)).status_code == 403
    api.operation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(('error', 'status', 'code'), [
    (ValueError('decision_impact_cursor_invalid'), 400, 'decision_impact_cursor_invalid'),
    (ValueError('decision_impact_cursor_stale'), 409, 'decision_impact_cursor_stale'),
    (ValueError('spec_coverage_source_changed'), 409, 'spec_coverage_source_changed'),
    (ValueError('secret invalid source'), 503, 'decision_impact_unavailable'),
    (GraphCorruption('secret path'), 503, 'decision_impact_unavailable'),
    (GraphQueryTimeout('secret query'), 504, 'graph_query_timeout'),
    (EntityNotFoundError('decision', 'secret'), 404, 'decision_not_found'),
])
async def test_failures_are_sanitized_not_empty_success(api, error, status, code):
    api.operation.side_effect = error
    response = await api.client.get(URL)
    assert response.status_code == status
    assert response.json()['detail']['code'] == code
    assert 'secret' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [{'limit': 1001}, {'max_depth': 9}, {'max_depth': 0}, {'timeout_ms': 30001}])
async def test_transport_bounds_precede_aggregation(api, params):
    assert (await api.client.get(URL, params=params)).status_code == 422
    api.operation.assert_not_called()
