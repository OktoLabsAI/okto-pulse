"""A technical source-read outage is retryable; integrity failure is not."""
import pytest
from sqlalchemy.exc import OperationalError

from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from okto_pulse.core.ports.kg_cognitive_source import CognitiveSourceConflict
from test_learning_capture_writer import BOARD, actor, uow, runtime as _runtime
from test_learning_materialization_worker import work_store as _work_store, deliver_capture_events
from test_learning_materialization_worker import graph_runtime as _graph_runtime, independent_gates as _independent_gates

runtime = _runtime
work_store = _work_store
graph_runtime = _graph_runtime
independent_gates = _independent_gates


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['operational', 'integrity'])
async def test_worker_distinguishes_source_outage_from_integrity(runtime, work_store, monkeypatch, failure):
    from okto_pulse.community.adapters import sqlalchemy_kg_cognitive_source as adapter
    factory, _, source_store, request = runtime
    store, discovery = work_store
    async with factory() as session:
        await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))
        await session.commit()
    history = await source_store.enumerate(BOARD)
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    async def unavailable(*_args, **_kwargs):
        if failure == 'operational':
            raise OperationalError('source read', {}, RuntimeError('temporary database outage'))
        raise CognitiveSourceConflict('cognitive_source_fingerprint_mismatch')
    with monkeypatch.context() as fault:
        fault.setattr(adapter, '_load_base_rows', unavailable)
        assert await worker.drain_once() == 1
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    if failure == 'operational':
        assert item.status == 'pending'
        assert item.reason == 'cognitive_source_read_unavailable'
        assert await worker.drain_once() == 1
        current, = store.list_items(BOARD, store.latest_generation(BOARD))
        assert current.status == 'pending' and current.reason == 'learning_capture_awaiting_done'
    else:
        assert item.status == 'failed' and item.reason == 'cognitive_source_fingerprint_mismatch'
        assert await worker.drain_once() == 0
        assert store.list_items(BOARD, store.latest_generation(BOARD)) == [item]
    assert await source_store.enumerate(BOARD) == history


@pytest.mark.asyncio
async def test_transient_append_failure_compensates_then_worker_retries(graph_runtime, work_store, monkeypatch):
    from test_learning_materialization_writer import graph_rows
    runtime, capture, _, _ = graph_runtime
    factory, _, source_store, _ = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    async def unavailable(*_args, **_kwargs):
        raise OperationalError('source append', {}, RuntimeError('temporary database outage'))
    with monkeypatch.context() as fault:
        fault.setattr(source_store, '_stage_append_many', unavailable)
        assert await worker.drain_once() == 1
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'pending' and item.reason == 'kg_cognitive_source_unavailable'
    assert graph_rows('MATCH (n:Learning) RETURN n.id') == []
    assert await source_store.enumerate(BOARD) == (capture,)
    assert await worker.drain_once() == 1
    current, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert current.status == 'consolidated'
    assert len(await source_store.enumerate(BOARD)) == 2
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [[capture.node_id, 'canonical-bug']]


@pytest.mark.asyncio
async def test_unavailable_projection_probe_does_not_attempt_graph_repair(graph_runtime, work_store, monkeypatch):
    from unittest.mock import AsyncMock
    from okto_pulse.core.kg.cognitive_closeout_production import ConsolidationPipelinePersister
    from okto_pulse.core.kg.interfaces import get_kg_registry
    runtime, _, _, _ = graph_runtime
    factory, _, source_store, _ = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    history = await source_store.enumerate(BOARD)
    def unavailable(*_args, **_kwargs):
        raise TimeoutError('graph read unavailable')
    persist = AsyncMock(side_effect=AssertionError('An unavailable probe is not verified absence'))
    with monkeypatch.context() as fault:
        fault.setattr(get_kg_registry().cypher_executor, 'execute_read_only', unavailable)
        fault.setattr(ConsolidationPipelinePersister, 'persist_authored_learning', persist)
        assert await worker.drain_once() == 1
    persist.assert_not_called()
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'pending' and item.reason == 'learning_capture_projection_unavailable'
    assert await source_store.enumerate(BOARD) == history
    assert await worker.drain_once() == 1
    current, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert current.status == 'consolidated'
    assert await source_store.enumerate(BOARD) == history
