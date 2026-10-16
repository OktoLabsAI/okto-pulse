"""A temporary projection outage must not hide later known source invalidation."""
import pytest

from okto_pulse.core.kg.cognitive_closeout_production import ConsolidationPipelinePersister
from test_learning_invalidation_fanin import prepare_worker, change_source
from test_learning_source_invalidation import (
    runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, work_store as _work_store,
)
from test_learning_capture_writer import BOARD
from test_learning_reuse_materialization import associations, stage_reuse

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store


@pytest.mark.asyncio
async def test_changed_source_after_projection_outage_still_invalidates_pair(graph_runtime, work_store, monkeypatch):
    runtime, _, _, _ = graph_runtime
    factory, _, sources, _ = runtime
    worker = await prepare_worker(graph_runtime, work_store)
    history = await sources.enumerate(BOARD)
    async def unavailable(*args, **kwargs):
        return 'unavailable'
    with monkeypatch.context() as patch:
        patch.setattr(ConsolidationPipelinePersister, 'inspect_authored_learning', unavailable)
        assert await worker.drain_once() == 1
    store, _ = work_store
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'pending' and item.reason == 'learning_capture_projection_unavailable'
    await change_source(factory)
    assert await worker.drain_once() == 1
    assert associations() == set()
    assert await sources.enumerate(BOARD) == history


@pytest.mark.asyncio
async def test_pending_old_receipt_recovers_when_new_capture_supports_pair(graph_runtime, work_store, monkeypatch):
    runtime, capture, _, persister = graph_runtime
    factory, _, sources, _ = runtime
    worker = await prepare_worker(graph_runtime, work_store)
    async def unavailable(*args, **kwargs):
        return 'unavailable'
    with monkeypatch.context() as patch:
        patch.setattr(ConsolidationPipelinePersister, 'inspect_authored_learning', unavailable)
        assert await worker.drain_once() == 1
    await change_source(factory)
    _, reused = await stage_reuse(graph_runtime, capture_id='valid-after-outage')
    assert await persister.persist_authored_learning(BOARD, 'bug-context', reused, raise_failures=True)
    history = await sources.enumerate(BOARD)
    assert await worker.drain_once() == 1
    store, _ = work_store
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'consolidated' and item.reason == 'authored_capture_materialized'
    assert associations() == {(capture.node_id, 'canonical-bug')}
    assert await sources.enumerate(BOARD) == history
