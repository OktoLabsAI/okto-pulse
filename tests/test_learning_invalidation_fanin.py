"""Current scope support, other origins and compensation on real SQL/Grafx."""
import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from test_learning_materialization_worker import deliver_capture_events
from test_learning_source_invalidation import (
    runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, work_store as _work_store,
)
from test_learning_capture_writer import BOARD
from test_learning_reuse_materialization import add_second_bug, stage_reuse, associations

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store
pytestmark = pytest.mark.asyncio


async def prepare_worker(graph_runtime, work_store):
    runtime, _, _, _ = graph_runtime
    factory, _, _, _ = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    return worker


async def change_source(factory):
    async with factory() as session:
        bug = await session.get(Card, 'bug-context')
        bug.action_plan = 'An updated correction requiring its own applicability assessment.'
        await session.commit()


async def test_latest_valid_capture_for_same_bug_preserves_pair_against_old_receipt(graph_runtime, work_store):
    runtime, capture, _, persister = graph_runtime
    factory, _, sources, _ = runtime
    worker = await prepare_worker(graph_runtime, work_store)
    await change_source(factory)
    _, reused = await stage_reuse(graph_runtime, capture_id='new-current-basis')
    assert await persister.persist_authored_learning(BOARD, 'bug-context', reused, raise_failures=True)
    history = await sources.enumerate(BOARD)
    # Only the original receipt is queued: the newer current authored source
    # must protect the pair independently of a worker tick/ACK for that source.
    assert await worker.drain_once() == 0
    assert associations() == {(capture.node_id, 'canonical-bug')}
    assert await sources.enumerate(BOARD) == history


async def test_invalidating_old_origin_preserves_newest_literal_and_other_origin(graph_runtime, work_store):
    runtime, capture, _, persister = graph_runtime
    factory, _, sources, _ = runtime
    worker = await prepare_worker(graph_runtime, work_store)
    await add_second_bug(factory)
    _, reused = await stage_reuse(graph_runtime, bug_id='second-bug')
    assert await persister.persist_authored_learning(BOARD, 'second-bug', reused, raise_failures=True)
    history = await sources.enumerate(BOARD)
    await change_source(factory)
    assert await worker.drain_once() == 1
    assert associations() == {(capture.node_id, 'second-canonical-bug')}
    assert await sources.enumerate(BOARD) == history


async def test_concurrent_hold_after_graph_commit_restores_pair(graph_runtime, work_store, monkeypatch):
    from okto_pulse.core.kg.guarded_write import GuardedWriteLease
    runtime, capture, _, _ = graph_runtime
    factory, _, sources, _ = runtime
    worker = await prepare_worker(graph_runtime, work_store)
    await change_source(factory)
    history = await sources.enumerate(BOARD)
    store, _ = work_store
    durable = GuardedWriteLease.ensure_durable
    protected = []
    def hold_after_commit(lease, *args, **kwargs):
        result = durable(lease, *args, **kwargs)
        if not protected:
            generation = store.latest_generation(BOARD)
            item, = store.list_items(BOARD, generation)
            protected.append(store.update_item(board_id=BOARD, kg_generation_id=generation,
                item_id=item.item_id, new_status='pending', updated_by_agent_id='human:reviewer',
                reason_code='authority_denied', reason='Concurrent substantive review', actor='human'))
        return result
    monkeypatch.setattr(GuardedWriteLease, 'ensure_durable', hold_after_commit)
    assert await worker.drain_once() == 0
    assert len(protected) == 1
    assert store.list_items(BOARD, store.latest_generation(BOARD)) == protected
    assert associations() == {(capture.node_id, 'canonical-bug')}
    assert await sources.enumerate(BOARD) == history


@pytest.mark.parametrize('failure', ['commit', 'durability'])
async def test_late_failure_restores_pair_and_retries_without_fabricated_ack(graph_runtime, work_store,
        monkeypatch, failure):
    from okto_pulse.community.adapters.grafx_graph_transaction import _GrafxTransactionScope
    from okto_pulse.core.kg.guarded_write import GuardedWriteLease
    runtime, capture, _, _ = graph_runtime
    factory, _, sources, _ = runtime
    worker = await prepare_worker(graph_runtime, work_store)
    await change_source(factory)
    history = await sources.enumerate(BOARD)
    injected = []
    with monkeypatch.context() as patch:
        if failure == 'commit':
            commit = _GrafxTransactionScope.commit
            async def fail_after_commit(scope):
                result = await commit(scope)
                if not injected:
                    injected.append(True)
                    raise RuntimeError('injected_after_invalidation_commit')
                return result
            patch.setattr(_GrafxTransactionScope, 'commit', fail_after_commit)
        else:
            durable = GuardedWriteLease.ensure_durable
            def fail_after_durability(lease, *args, **kwargs):
                result = durable(lease, *args, **kwargs)
                if not injected:
                    injected.append(True)
                    raise RuntimeError('injected_after_invalidation_durability')
                return result
            patch.setattr(GuardedWriteLease, 'ensure_durable', fail_after_durability)
        assert await worker.drain_once() == 1
    assert injected == [True]
    store, _ = work_store
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'pending' and item.reason == 'learning_invalidation_pending'
    assert associations() == {(capture.node_id, 'canonical-bug')}
    assert await sources.enumerate(BOARD) == history
    assert await worker.drain_once() == 1
    assert associations() == set()
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'failed' and item.reason == 'learning_materialization_current_binding_required'
    assert await sources.enumerate(BOARD) == history
