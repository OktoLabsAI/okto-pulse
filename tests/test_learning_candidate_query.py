"""Existing context transports and real SQL/Grafx candidate qualification.

The graph fixture uses a controlled embedding and mocks independent completion
gates/health. It proves retrieval identity and provenance, not model calibration.
"""
import json
from types import SimpleNamespace

import pytest

from okto_pulse.core.application import learning_candidates
from okto_pulse.core.application.learning_capture import get_learning_capture_source
from okto_pulse.core.kg.interfaces.registry import get_kg_registry
from okto_pulse.core.ports.permission_policy import set_permission_flag
from test_learning_capture_rest import api as _api
from test_learning_capture_mcp import mcp_capture as _mcp_capture
from test_learning_capture_writer import runtime as _runtime, BOARD
from test_learning_materialization_writer import graph_runtime as _graph_runtime
from test_learning_submission_writer import independent_gates as _independent_gates

api = _api
mcp_capture = _mcp_capture
runtime = _runtime
graph_runtime = _graph_runtime
independent_gates = _independent_gates
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('transport', ['rest', 'mcp'])
async def test_query_is_optional_and_read_denial_precedes_search(api, mcp_capture, monkeypatch, transport):
    client, _, principal, _ = api
    read, _, body, _, flags = mcp_capture
    calls = []
    def registry(): calls.append(True); return SimpleNamespace(ranked_graph_search=None, embedding_provider=None)
    monkeypatch.setattr(learning_candidates, 'get_kg_registry', registry)
    if transport == 'rest':
        ordinary = await client.get('/api/v1/bugs/bug-context/learning-capture-context', params={'board_id': BOARD})
        assert ordinary.status_code == 200 and 'candidates' not in ordinary.json()
        set_permission_flag(principal.permissions, 'kg.query.learning_from_bugs', False)
        denied = await client.get('/api/v1/bugs/bug-context/learning-capture-context', params={'board_id': BOARD, 'candidate_query': 'lesson'})
        assert denied.status_code == 403 and not calls
        set_permission_flag(principal.permissions, 'kg.query.learning_from_bugs', True)
        result = (await client.get('/api/v1/bugs/bug-context/learning-capture-context', params={'board_id': BOARD, 'candidate_query': 'lesson'})).json()
    else:
        ordinary = json.loads(await read(board_id=BOARD, bug_id=body['bug_id']))
        assert 'candidates' not in ordinary
        set_permission_flag(flags, 'kg.query.learning_from_bugs', False)
        denied = json.loads(await read(board_id=BOARD, bug_id=body['bug_id'], candidate_query='lesson'))
        assert denied['code'] == 'permission_denied' and not calls
        set_permission_flag(flags, 'kg.query.learning_from_bugs', True)
        result = json.loads(await read(board_id=BOARD, bug_id=body['bug_id'], candidate_query='lesson'))
    assert result['candidates']['status'] == 'unavailable'
    assert result['candidates']['limitation'] == 'search_capability_unavailable'
    assert calls == [True]


async def test_routed_native_candidates_match_durable_heads_without_writes(graph_runtime, monkeypatch):
    runtime, capture, selection, persister = graph_runtime
    factory, _, store, _ = runtime
    registry = get_kg_registry()
    monkeypatch.setattr(registry.require_embedding_provider(), 'encode', lambda query: [1.0] + [0.0] * 383)
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    before = await store.enumerate(BOARD)
    literal = max(before, key=lambda row: row.source_revision)
    async with factory() as session:
        page = await get_learning_capture_source(session, board_id=BOARD, bug_id='bug-context', candidate_query='lesson')
    candidates = page['candidates']
    assert candidates['status'] == 'available', candidates
    candidate, = candidates['items']
    assert candidate['learning_id'] == capture.node_id and candidate['fingerprint'] == literal.record_fingerprint
    assert candidate['content'] == capture.payload['content']
    assert candidate['similarity'] == pytest.approx(1.0) and candidate['suggestion'] == 'reuse'
    assert candidates['applicability'] == 'not_assessed' and not candidates['exhaustive']
    assert await store.enumerate(BOARD) == before
