"""Projection-loss recovery from an admitted capture, without resubmission."""
import pytest

from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from test_learning_materialization_worker import (
    runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, work_store as _work_store, deliver_capture_events,
)
from test_learning_materialization_writer import graph_rows
from test_learning_capture_writer import BOARD

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store


@pytest.mark.asyncio
@pytest.mark.parametrize('loss', ['node', 'edge'])
async def test_worker_restores_lost_projection_without_new_authorship(graph_runtime, work_store, loss):
    runtime, capture, _, _ = graph_runtime
    factory, _, source_store, _ = runtime
    store, discovery = work_store
    assert await deliver_capture_events(factory) == 1
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    history = await source_store.enumerate(BOARD)
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'consolidated'
    if loss == 'node':
        graph_rows('MATCH (n:Learning) WHERE n.id = $id DETACH DELETE n', {'id': capture.node_id})
    else:
        graph_rows('MATCH (n:Learning)-[r:validates]->(b:Bug) WHERE n.id = $id DELETE r', {'id': capture.node_id})
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == []
    assert await worker.drain_once() == 1
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [[capture.node_id, 'canonical-bug']]
    assert await source_store.enumerate(BOARD) == history
    assert await worker.drain_once() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['consolidated', 'pending', 'skipped'])
async def test_projection_recovery_preserves_human_work_decisions(graph_runtime, work_store, status):
    runtime, capture, _, _ = graph_runtime
    factory, _, source_store, _ = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    history = await source_store.enumerate(BOARD)
    generation = store.latest_generation(BOARD)
    item, = store.list_items(BOARD, generation)
    protected = store.update_item(board_id=BOARD, kg_generation_id=generation, item_id=item.item_id,
        new_status=status, updated_by_agent_id='human:reviewer', reason='Preserve reviewed restriction',
        reason_code='canonical_learning_mixed_deferred', actor='human')
    graph_rows('MATCH (n:Learning) WHERE n.id = $id DETACH DELETE n', {'id': capture.node_id})
    assert await worker.drain_once() == 0
    assert store.list_items(BOARD, generation) == [protected]
    assert await source_store.enumerate(BOARD) == history
    assert graph_rows('MATCH (n:Learning) RETURN n.id') == []
