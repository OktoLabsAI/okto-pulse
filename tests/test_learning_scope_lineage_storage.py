"""Bounded SQL lineage history and the existing capture-list read surface."""
import pytest
from sqlalchemy import event

from okto_pulse.core.application.learning_supersedence import stage_learning_scope_replacement
from okto_pulse.core.ports.kg_cognitive_source import BoundedCognitiveHistoryReader, CognitiveSourceUnavailable
from test_learning_scope_history import store as _store, source_records as _source_records
from test_kg_cognitive_source_adapter import BOARD, _record

store = _store
source_records = _source_records
pytestmark = pytest.mark.asyncio


async def test_history_limit_is_applied_in_sql_and_never_returns_a_truncated_proof(store):
    adapter, factory = store
    assert isinstance(adapter, BoundedCognitiveHistoryReader)
    for title in ('first', 'second', 'third'):
        await adapter.append(_record('bounded-history', title=title))
    statements = []
    engine = factory.kw['bind'].sync_engine
    def observe(conn, cursor, statement, parameters, context, executemany):
        if 'kg_cognitive_source_revisions' in statement and statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(engine, 'before_cursor_execute', observe)
    try:
        async with factory() as session:
            with pytest.raises(CognitiveSourceUnavailable, match='cognitive_source_history_limit'):
                await adapter.read_bounded_history_in_context(session, board_id=BOARD,
                    node_id='bounded-history', generation=0, max_records=2)
            assert statements and all('LIMIT' in statement.upper() for statement in statements)
            complete = await adapter.read_bounded_history_in_context(session, board_id=BOARD,
                node_id='bounded-history', generation=0, max_records=3)
            assert [row.source_revision for row in complete] == [0, 1, 2]
    finally:
        event.remove(engine, 'before_cursor_execute', observe)


@pytest.mark.parametrize('limit', [0, -1, 201, True, '2'])
async def test_invalid_read_budget_is_rejected(store, limit):
    adapter, factory = store
    async with factory() as session:
        with pytest.raises(ValueError, match='history_selection_invalid'):
            await adapter.read_bounded_history_in_context(session, board_id=BOARD,
                node_id='absent', generation=0, max_records=limit)


async def test_capture_list_reports_only_committed_historical_linkage(store, source_records, monkeypatch):
    from okto_pulse.core.application import learning_capture
    adapter, factory = store
    previous, _, capture, successor = source_records(BOARD)
    monkeypatch.setattr(learning_capture, 'require_cognitive_source_store', lambda: adapter)
    await adapter.append_many((previous, capture))
    async with factory() as session:
        pending = await learning_capture.list_learning_captures(session, board_id=BOARD, bug_id='covered-bug')
        assert pending['items'][0]['lineage']['state'] == 'unverified'
        await stage_learning_scope_replacement(session, adapter, previous=previous, capture=capture, successor=successor)
        await session.commit()
    async with factory() as session:
        history = await learning_capture.list_learning_captures(session, board_id=BOARD, bug_id='covered-bug')
        item, = history['items']
        assert item['capture'] == capture.payload and item['fingerprint'] == capture.record_fingerprint
        assert item['lineage']['state'] == 'recorded'
        assert item['lineage']['current_applicability'] == item['lineage']['graph_projection'] == 'not_assessed'
