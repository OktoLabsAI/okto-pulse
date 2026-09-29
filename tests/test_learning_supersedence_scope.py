"""Scoped admission prerequisite and the unchanged generic supersedence limit."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_learning_capture_intents import target, intent, submit
from test_learning_reuse_materialization import (
    BOARD, stage_reuse, add_second_bug, associations, graph_rows,
    graph_runtime as _graph_runtime, runtime as _runtime, independent_gates as _independent_gates,
)

graph_runtime = _graph_runtime
runtime = _runtime
independent_gates = _independent_gates
pytestmark = pytest.mark.asyncio


async def test_scoped_capture_preserves_v1_history_and_does_not_mutate_target(runtime):
    factory, _, store, request = runtime
    await target(runtime)
    previous, = await store.enumerate(BOARD)
    draft = replace(request, intent=replace(intent(previous, 'supersede'), scope='source_bug'))
    capture = await submit(factory, draft)
    assert capture.payload['capture_format'] == 'learning-capture/v2'
    assert capture.payload['intent']['scope'] == 'source_bug'
    assert capture.payload['source']['bug_id'] == request.bug_id
    assert (await submit(factory, draft)).record_fingerprint == capture.record_fingerprint
    history = await store.enumerate(BOARD)
    assert len(history) == 2 and previous in history
    with pytest.raises(ValueError, match='idempotency_conflict'):
        await submit(factory, replace(draft, intent=replace(draft.intent, scope=None)))
    assert await store.enumerate(BOARD) == history


async def test_generic_supersedence_hides_all_origins_and_is_not_a_scoped_replacement(graph_runtime):
    """Characterization only: direct generic graph operation, not the new writer.

    Its global mark must not be reused to implement KG7.6's partial scope.
    SQL history is deliberately not altered by this isolated graph experiment.
    """
    from kg_schema_testing import open_board_connection
    from okto_pulse.core.kg.transaction import TransactionOrchestrator
    runtime, original, selected, persister = graph_runtime
    factory, _, store, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selected)
    await add_second_bug(factory)
    _, reused = await stage_reuse(graph_runtime, bug_id='second-bug')
    assert await persister.persist_authored_learning(BOARD, 'second-bug', reused)
    before = await store.enumerate(BOARD)
    expected = {(original.node_id, 'canonical-bug'), (original.node_id, 'second-canonical-bug')}
    assert associations() == expected
    with open_board_connection(BOARD) as (_, connection):
        orchestrator = TransactionOrchestrator(connection, session_id='generic-scope-characterization', board_id=BOARD)
        orchestrator.supersede_node('Learning', 'generic-successor', original.node_id,
            {'title': 'Narrower replacement', 'content': 'Applies only to the second deployment',
                'created_at': '2026-09-29T00:00:00+00:00'}, revocation_reason='Scope experiment')
    assert graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN n.superseded_by',
        {'id': original.node_id}) == [['generic-successor']]
    assert associations() == expected  # Historical edges still exist physically.
    for bug_id in ('bug-context', 'second-bug'):
        work = SimpleNamespace(learning_id=original.node_id, bug_id=bug_id)
        assert await persister.inspect_authored_learning(BOARD, work) == 'missing'
    assert await store.enumerate(BOARD) == before
