"""KG4.4/KG7.5: a present projection does not prove the source is still current."""
import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from test_learning_materialization_worker import (
    runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, work_store as _work_store, deliver_capture_events,
)
from test_learning_capture_writer import BOARD
from test_learning_materialization_writer import graph_rows

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['action_plan', 'reopen'])
async def test_present_graph_cannot_hide_a_changed_authored_source_basis(graph_runtime, work_store, change):
    runtime, capture, _, _ = graph_runtime
    factory, assembler, sources, _ = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    history = await sources.enumerate(BOARD)
    async with factory() as session:
        old = await assembler.assemble_semantic(session, board_id=BOARD, bug_id='bug-context')
        bug = await session.get(Card, 'bug-context')
        if change == 'action_plan':
            bug.action_plan = 'A different correction with a different applicability basis.'
        else:
            bug.status = 'in_progress'
        await session.commit()
    async with factory() as session:
        current = await assembler.assemble_semantic(session, board_id=BOARD, bug_id='bug-context')
    assert current.source_digest != old.source_digest
    # The graph still has the old projection; neither canonical presence nor
    # a stable source reference establishes current applicability.
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [
        [capture.node_id, 'canonical-bug']]
    assert await worker.drain_once() == 1
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == ('failed' if change == 'action_plan' else 'pending')
    assert item.reason == ('learning_materialization_current_binding_required'
        if change == 'action_plan' else 'learning_capture_awaiting_done')
    assert await sources.enumerate(BOARD) == history


@pytest.mark.asyncio
async def test_source_inspection_cannot_overwrite_a_concurrent_substantive_hold(graph_runtime, work_store, monkeypatch):
    from okto_pulse.core.application import learning_materialization_worker
    runtime, _, _, _ = graph_runtime
    factory, _, sources, _ = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    history = await sources.enumerate(BOARD)
    async with factory() as session:
        bug = await session.get(Card, 'bug-context')
        bug.action_plan = 'Different correction'
        await session.commit()
    inspect = learning_materialization_worker.inspect_capture_work_basis
    protected = []
    async def concurrently_review(*args, **kwargs):
        result = await inspect(*args, **kwargs)
        assert result is not None
        generation = store.latest_generation(BOARD)
        item, = store.list_items(BOARD, generation)
        protected.append(store.update_item(board_id=BOARD, kg_generation_id=generation, item_id=item.item_id,
            new_status='pending', updated_by_agent_id='human:reviewer', reason_code='authority_denied',
            reason='Concurrent substantive review', actor='human'))
        return result
    monkeypatch.setattr(learning_materialization_worker, 'inspect_capture_work_basis', concurrently_review)
    assert await worker.drain_once() == 0
    assert store.list_items(BOARD, store.latest_generation(BOARD)) == protected
    assert await sources.enumerate(BOARD) == history
