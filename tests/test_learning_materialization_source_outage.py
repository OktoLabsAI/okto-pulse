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


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['capture', 'bug'])
async def test_missing_native_source_records_never_generate_retrospective_learning(
        runtime, work_store, monkeypatch, tmp_path, missing):
    from unittest.mock import AsyncMock
    from okto_pulse.community.adapters.sqlalchemy_kg_cognitive_source import CommunitySqlAlchemyCognitiveSourceStore
    from okto_pulse.core.kg.cognitive_closeout_production import ConsolidationPipelinePersister
    from test_bug_cognitive_context_adapter import _runtime

    factory, assembler, source_store, request = runtime
    work, discovery = work_store
    async with factory() as session:
        await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))
        await session.commit()
    history = await source_store.enumerate(BOARD)
    await deliver_capture_events(factory)
    generation = work.latest_generation(BOARD)
    before, = work.list_items(BOARD, generation)
    # Fault injection routes one source read to a genuinely empty current-schema
    # database. It does not delete immutable native history or install old data.
    empty_engine, empty_factory = await _runtime(tmp_path / 'missing-native-source.sqlite')
    missing_store = CommunitySqlAlchemyCognitiveSourceStore(empty_factory)
    persist = AsyncMock(side_effect=AssertionError('No authored material without its source'))
    try:
        with monkeypatch.context() as fault:
            fault.setattr(ConsolidationPipelinePersister, 'persist_authored_learning', persist)
            if missing == 'capture':
                async def read_empty_capture(context, **identity):
                    async with empty_factory() as empty_context:
                        return await missing_store.read_fingerprint_in_context(empty_context, **identity)
                fault.setattr(source_store, 'read_fingerprint_in_context', read_empty_capture)
            else:
                assemble = assembler.assemble_semantic
                async def read_empty_source(context, *, board_id, bug_id):
                    async with empty_factory() as empty_context:
                        return await assemble(empty_context, board_id=board_id, bug_id=bug_id)
                fault.setattr(assembler, 'assemble_semantic', read_empty_source)
            worker = CognitiveCloseoutWorker(factory, store=work, pending_work_provider=discovery)
            assert await worker.drain_once() == 1
            item, = work.list_items(BOARD, generation)
            assert item.item_id == before.item_id and item.content_hash == before.content_hash
            assert item.status == ('failed' if missing == 'capture' else 'pending')
            assert item.reason == ('learning_capture_work_source_mismatch'
                if missing == 'capture' else 'learning_capture_source_unavailable')
            assert not item.evidence_refs
            persist.assert_not_called()
            assert await missing_store.enumerate(BOARD) == ()
            assert await source_store.enumerate(BOARD) == history
            if missing == 'capture':
                assert await worker.drain_once() == 0
    finally:
        await empty_engine.dispose()
