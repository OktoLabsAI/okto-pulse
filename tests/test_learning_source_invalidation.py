"""KG4.5: a known stale origin must stop supporting the current association."""
import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from test_learning_materialization_source_drift import (
    runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, work_store as _work_store,
)
from test_learning_materialization_worker import deliver_capture_events
from test_learning_capture_writer import BOARD
from test_learning_materialization_writer import graph_rows

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['action_plan', 'reopen'])
async def test_known_source_change_invalidates_only_current_association(graph_runtime, work_store, change):
    runtime, capture, _, _ = graph_runtime
    factory, _, sources, _ = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    history = await sources.enumerate(BOARD)
    async with factory() as session:
        bug = await session.get(Card, 'bug-context')
        if change == 'action_plan':
            bug.action_plan = 'The correction now depends on a different semantic basis.'
        else:
            bug.status = 'in_progress'
        await session.commit()
    assert await worker.drain_once() == 1
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == []
    assert graph_rows('MATCH (n:Learning) RETURN n.id') == [[capture.node_id]]
    assert await sources.enumerate(BOARD) == history
