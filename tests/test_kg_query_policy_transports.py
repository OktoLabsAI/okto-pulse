"""Existing query transports cannot widen an authorized Board's deadline."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from okto_pulse.community.api import kg_routes as kg, kg_exploration as exploration
from okto_pulse.core.domain.permissions import ALL_FLAGS
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from test_kg_power_rest_authorization import _actor, _permission_set, BOARD_ID


@pytest.mark.asyncio
@pytest.mark.parametrize('door', ['cypher', 'search', 'analytics'])
@pytest.mark.parametrize('requested,expected', [(None, 800), (30000, 800), (12, 12)])
async def test_rest_reads_persisted_board_deadline(monkeypatch, door, requested, expected):
    actor = _actor(_permission_set({flag: True for flag in ALL_FLAGS}))
    board = SimpleNamespace(id=BOARD_ID, owner_id=actor.actor_id, realm_id=LOCAL_REALM_ID,
                            settings={'kg_query_timeout_ms': 800})
    uow = SimpleNamespace(boards=SimpleNamespace(get=AsyncMock(return_value=board)))
    observed = []
    if door == 'cypher':
        def read(*args, **kwargs):
            observed.append(kwargs['timeout_ms'])
            assert kwargs['max_rows'] == 200
            return {'rows': []}
        monkeypatch.setattr(kg, 'execute_cypher_read_only', read)
        result = await kg.cypher_query(BOARD_ID, cypher='MATCH (n) RETURN n.id',
                                       timeout_ms=requested, actor=actor, uow=uow)
    else:
        def read_search(board_id, request):
            observed.append(round(request.timeout_seconds * 1000))
            return {'hits': []}

        def read_analytics(board_id, **options):
            observed.append(round(options['timeout_seconds'] * 1000))
            return {'components': []}

        monkeypatch.setattr(exploration, 'get_kg_registry', lambda: SimpleNamespace(
            ranked_graph_search=SimpleNamespace(search=read_search),
            graph_analytics=SimpleNamespace(analyze=read_analytics)))
        timeout = {} if requested is None else {'timeout_seconds': requested / 1000}
        if door == 'search':
            body = exploration.SearchRequest(node_type='Decision', query='query', **timeout)
            result = await exploration.search(BOARD_ID, body, actor=actor, uow=uow)
        else:
            body = exploration.AnalyticsRequest(node_types=['Decision'], algorithm='cycles', **timeout)
            result = await exploration.analyze_graph(BOARD_ID, body, actor=actor, uow=uow)
    assert isinstance(result, dict), result
    assert observed == [expected]
    uow.boards.get.assert_awaited_once_with(BOARD_ID)
