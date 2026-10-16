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
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout
from okto_pulse.core.ports.lineage_query import LineageSnapshot, LineageNode, LineageRelation
from okto_pulse.core.ports.traceability import TraceabilityReadError
from okto_pulse.core.services.lineage_query import project_lineage

URL = '/boards/board/lineage'
FLAGS = ['board.read','spec.entity.read','card.entity.read','ideation.entity.read',
    'refinement.entity.read','story.entity.read','amendment.revision.read']


@pytest_asyncio.fixture
async def api(monkeypatch):
    actor = ActorContext('agent','mcp',actor_kind='agent',realm_id='local',board_id='board',permissions=list(FLAGS))
    monkeypatch.setattr(RESTAdapterContract,'actor',lambda *args,**kwargs:actor)
    board = SimpleNamespace(id='board',owner_id='owner',realm_id='local',settings={'kg_query_timeout_ms':700})
    calls = []
    async def aggregate(query,**options):
        calls.append((query,options))
        snapshot = LineageSnapshot('board',query.subject_ref,query.actor_scope_ref,'revision',datetime.now(timezone.utc),
            tuple(LineageNode(f'spec:{i}','spec',f'Spec {i}','validated') for i in range(6)),
            tuple(LineageRelation(f'spec:{i}',f'spec:{i+1}','precedes',f'dependency:{i}') for i in range(5)),True)
        return project_lineage(query,snapshot)
    operation = AsyncMock(side_effect=aggregate)
    uow = SimpleNamespace(boards=SimpleNamespace(get=AsyncMock(return_value=board)),
        services=SimpleNamespace(analytics=SimpleNamespace(lineage=operation)))
    app = FastAPI(); app.include_router(analytics.router)
    app.dependency_overrides[require_user] = lambda:'user'
    app.dependency_overrides[get_unit_of_work] = lambda:uow
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        yield SimpleNamespace(client=client,actor=actor,operation=operation,calls=calls)


@pytest.mark.asyncio
async def test_partial_expansion_and_pages_keep_global_counts_and_policy(api):
    response = await api.client.get(URL,params={'subject_ref':'spec:0','limit':1,'timeout_ms':30000})
    assert response.status_code == 200,response.text
    first = response.json()
    assert api.calls[0][1] == {'timeout_ms':700}
    assert first['frontier_refs'] == ['spec:4']
    second = await api.client.get(URL,params={'subject_ref':'spec:0','limit':1,'cursor':first['next_cursor']})
    assert second.status_code == 200,second.text
    assert second.json()['counts'] == first['counts']
    expanded = await api.client.get(URL,params={'subject_ref':'spec:0','max_depth':8})
    assert expanded.json()['counts']['reached_targets'] == 5
    assert expanded.json()['projection_freshness']['state'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('flag',FLAGS)
async def test_grants_precede_source_aggregation(api,flag):
    api.actor.permissions.remove(flag)
    assert (await api.client.get(URL,params={'subject_ref':'spec:0'})).status_code == 403
    api.operation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(('error','status','code'),[
    (ValueError('lineage_cursor_invalid'),400,'lineage_cursor_invalid'),
    (ValueError('lineage_cursor_stale'),409,'lineage_cursor_stale'),
    (ValueError('lineage_source_changed'),409,'lineage_source_changed'),
    (ValueError('lineage_source_node_bound'),409,'lineage_source_node_bound'),
    (ValueError('secret invalid source'),503,'lineage_unavailable'),
    (GraphQueryTimeout('secret query'),504,'graph_query_timeout'),
    (TraceabilityReadError('secret','secret',status_code=404),404,'lineage_subject_not_found'),
])
async def test_failures_are_sanitized_not_empty_success(api,error,status,code):
    api.operation.side_effect = error
    response = await api.client.get(URL,params={'subject_ref':'spec:0'})
    assert response.status_code == status
    assert response.json()['detail']['code'] == code
    assert 'secret' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('params',[{'limit':1001},{'max_depth':33},{'subject_ref':'spec:s:fr:x'}])
async def test_closed_bounds_precede_aggregation(api,params):
    assert (await api.client.get(URL,params={'subject_ref':'spec:0',**params})).status_code == 422
    api.operation.assert_not_called()
