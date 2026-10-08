"""HTTP contract, using the real use case and reducer over bounded facts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from okto_pulse.community.api import analytics
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption, GraphQueryTimeout
from okto_pulse.core.ports.bug_clusters import BugClustersSnapshot, ClusterBugFact, BugClusterAssociation
from okto_pulse.core.services.bug_clusters import project_bug_clusters


@pytest_asyncio.fixture
async def api(monkeypatch):
    actor = ActorContext('agent', 'mcp', actor_kind='agent', board_id='board',
        realm_id=LOCAL_REALM_ID, permissions=['*'])
    monkeypatch.setattr(RESTAdapterContract, 'actor', lambda *args, **kwargs: actor)
    board = SimpleNamespace(id='board', owner_id='owner', realm_id=LOCAL_REALM_ID,
        settings={'kg_query_timeout_ms': 700})
    calls = []

    async def aggregate(query, **options):
        calls.append((query, options))
        facts = tuple(ClusterBugFact(str(i), 'Bug', query.window.from_inclusive,
            'done', severity, None, 'revision') for i, severity in enumerate(('major', 'minor')))
        return project_bug_clusters(query, BugClustersSnapshot(query.board_id, query.actor_scope_ref,
            datetime.now(timezone.utc), facts, 2, True))

    operation = AsyncMock(side_effect=aggregate)
    uow = SimpleNamespace(boards=SimpleNamespace(get=AsyncMock(return_value=board)),
        services=SimpleNamespace(analytics=SimpleNamespace(bug_clusters=operation)))
    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[require_user] = lambda: 'user'
    app.dependency_overrides[get_unit_of_work] = lambda: uow
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield SimpleNamespace(client=client, actor=actor, board=board, calls=calls, operation=operation)


URL = '/boards/board/analytics/bug-clusters'


@pytest.mark.asyncio
async def test_default_window_typed_envelope_and_policy_cap(api):
    response = await api.client.get(URL, params={'group_by': 'severity', 'timeout_ms': 30000})
    assert response.status_code == 200, response.text
    data = response.json()
    query, options = api.calls[0]
    assert query.window.to_exclusive - query.window.from_inclusive == timedelta(days=15)
    assert options == {'timeout_ms': 700}
    assert data['authority'] == 'informational'
    assert data['distinct_bug_count'] == 2
    assert data['projection_freshness']['state'] == 'unknown'
    assert data['items'][0]['observed_median_resolution_hours'] is None
    assert set(data['window']) == {'from', 'to'}


@pytest.mark.asyncio
async def test_date_only_bounds_and_pagination_preserve_global_denominator(api):
    first = await api.client.get(URL, params={'group_by': 'severity', 'from': '2026-09-01',
        'to': '2026-09-15', 'limit': 1})
    assert first.status_code == 200, first.text
    data = first.json()
    assert data['window'] == {'from': '2026-09-01T00:00:00.000000Z', 'to': '2026-09-16T00:00:00.000000Z'}
    second = await api.client.get(URL, params={**data['window'], 'group_by': 'severity',
        'limit': 1, 'cursor': data['next_cursor']})
    assert second.status_code == 200, second.text
    assert second.json()['distinct_bug_count'] == data['distinct_bug_count'] == 2
    assert second.json()['items'][0]['target_ref'] != data['items'][0]['target_ref']


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [
    {'from': 'nonsense'}, {'to': 'nonsense'}, {'from': '2026-09-20', 'to': '2026-09-01'},
    {'cursor': 'opaque'}, {'severity': 'high'}, {'status': 'invalid'},
])
async def test_invalid_queries_are_not_empty_success(api, params):
    response = await api.client.get(URL, params={'group_by': 'severity', **params})
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_revoked_permission_denies_even_with_cursor(api):
    first = (await api.client.get(URL, params={'group_by': 'severity', 'limit': 1})).json()
    api.actor.permissions = ['board.read']
    response = await api.client.get(URL, params={**first['window'], 'group_by': 'severity',
        'limit': 1, 'cursor': first['next_cursor']})
    assert response.status_code == 403
    assert api.operation.await_count == 1


@pytest.mark.asyncio
async def test_foreign_board_is_not_enumerable(api):
    api.board.realm_id = 'foreign'
    response = await api.client.get(URL)
    assert response.status_code == 404
    api.operation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(('error', 'status', 'code'), [
    (ValueError('bug_clusters_cursor_stale'), 409, 'bug_clusters_cursor_stale'),
    (ValueError('bug_clusters_source_changed'), 409, 'bug_clusters_source_changed'),
    (ValueError('bug_clusters_target_outside_scope'), 503, 'bug_clusters_unavailable'),
    (GraphCorruption('secret native path and title'), 503, 'bug_clusters_unavailable'),
    (GraphQueryTimeout('secret native query'), 504, 'graph_query_timeout'),
])
async def test_failure_contract_is_explicit_and_does_not_expose_native_details(api, error, status, code):
    api.operation.side_effect = error
    response = await api.client.get(URL)
    assert response.status_code == status
    assert response.json()['detail']['code'] == code
    assert 'secret' not in response.text


@pytest.mark.asyncio
async def test_filter_change_rejects_cursor_without_mixing_pages(api):
    first = (await api.client.get(URL, params={'group_by': 'severity', 'limit': 1})).json()
    # A different filter is a different cursor scope even if its rows coincide.
    response = await api.client.get(URL, params={**first['window'], 'group_by': 'spec',
        'limit': 1, 'cursor': first['next_cursor']})
    assert response.status_code == 409

@pytest.mark.asyncio
async def test_generation_change_alone_rejects_cursor_without_mixing_pages(api):
    generation = ["generation-a"]

    async def aggregate(query, **options):
        facts = tuple(ClusterBugFact(str(i), "Bug", query.window.from_inclusive,
            "done", "major", None, "revision") for i in range(2))
        associations = tuple(BugClusterAssociation(str(i), "proxy", f"spec:one:tr:{i}",
            f"Target {i}", f"rule:{i}", "unknown") for i in range(2))
        return project_bug_clusters(query, BugClustersSnapshot(
            query.board_id, query.actor_scope_ref, datetime.now(timezone.utc),
            facts, 2, True, associations, graph_generation=generation[0],
            projected_bug_ids=("0", "1")))

    api.operation.side_effect = aggregate
    response = await api.client.get(URL, params={"group_by": "proxy", "limit": 1})
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["next_cursor"]
    params = {**first["window"], "group_by": "proxy", "limit": 1}
    same = await api.client.get(URL, params={**params, "cursor": first["next_cursor"]})
    assert same.status_code == 200, same.text
    generation[0] = "generation-b"
    stale = await api.client.get(URL, params={**params, "cursor": first["next_cursor"]})
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "bug_clusters_cursor_stale"
    assert "items" not in stale.json()
    restarted = await api.client.get(URL, params=params)
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["projection_freshness"]["graph_generation"] == "generation-b"
