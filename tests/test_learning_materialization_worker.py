"""Capture outbox → durable work → governed SQL/Grafx projection.

Completion gates and health are explicit fixtures inherited from the writer
suite. No LLM, real application data or running runtime is used.
"""
from dataclasses import replace

import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore, CommunityFileSystemCognitivePendingWorkProvider,
)
from okto_pulse.core.events.types import LearningCaptureAdmitted
from okto_pulse.core.events.handlers.learning_capture import LearningCaptureMaterializationEnqueuer
from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItemStore
from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from test_learning_materialization_writer import graph_runtime as _graph_runtime, graph_rows
from test_learning_capture_writer import BOARD, actor, uow, runtime as _runtime
from test_learning_submission_writer import independent_gates as _independent_gates

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
pytestmark = pytest.mark.asyncio


@pytest.fixture
def work_store(monkeypatch, tmp_path):
    from okto_pulse.core.kg import cognitive_closeout_production
    store = CognitiveConsolidationItemStore(artifact_store=CommunityFileSystemRebuildAuditArtifactStore(tmp_path))
    monkeypatch.setattr(cognitive_closeout_production, '_default_store', lambda: store)
    return store, CommunityFileSystemCognitivePendingWorkProvider(tmp_path)


async def deliver_capture_events(factory):
    async with factory() as session:
        rows = (await session.scalars(select(DomainEventRow).where(
            DomainEventRow.event_type == LearningCaptureAdmitted.event_type))).all()
        assert rows
        for row in rows:
            event = LearningCaptureAdmitted(board_id=row.board_id, event_id=row.id,
                actor_id=row.actor_id, actor_type=row.actor_type, occurred_at=row.occurred_at,
                **row.payload_json)
            await LearningCaptureMaterializationEnqueuer().handle(event, session)
        await session.commit()
        return len(rows)


async def test_capture_before_done_stays_pending_without_invoking_graph(runtime, work_store, monkeypatch):
    from unittest.mock import AsyncMock
    from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
    from okto_pulse.core.kg.cognitive_closeout_production import ConsolidationPipelinePersister
    factory, _, source_store, request = runtime
    store, discovery = work_store
    async with factory() as session:
        capture = await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))
        await session.commit()
    original_history = await source_store.enumerate(BOARD)
    assert len(original_history) == 1
    assert original_history[0].record_fingerprint == capture.record_fingerprint
    assert await deliver_capture_events(factory) == 1
    persist = AsyncMock(side_effect=AssertionError('No graph materialization before Done'))
    monkeypatch.setattr(ConsolidationPipelinePersister, 'persist_authored_learning', persist)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'pending' and item.reason == 'learning_capture_awaiting_done'
    assert await source_store.enumerate(BOARD) == original_history
    persist.assert_not_called()


@pytest.mark.parametrize('late_target', [False, True])
async def test_event_and_worker_materialize_without_agent_polling_or_llm(graph_runtime, work_store, late_target):
    runtime, capture, _, _ = graph_runtime
    factory, _, source_store, _ = runtime
    store, discovery = work_store
    assert await deliver_capture_events(factory) == 1
    assert graph_rows('MATCH (n:Learning) RETURN n.id') == []
    if late_target:
        graph_rows("MATCH (b:Bug) WHERE b.id = 'canonical-bug' DETACH DELETE b")
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    if late_target:
        assert await worker.drain_once() == 1
        item, = store.list_items(BOARD, store.latest_generation(BOARD))
        assert item.status == 'pending' and item.reason == 'learning_capture_awaiting_canonical_bug'
        assert len(await source_store.enumerate(BOARD)) == 1
        graph_rows("CREATE (b:Bug {id: 'canonical-bug', title: 'Bug', "
            "source_artifact_ref: 'bug:bug-context', graph_layer: 'canonical', maturity_status: 'canonical_eligible'})")
    assert await worker.drain_once() == 1
    item, = store.list_items(BOARD, store.latest_generation(BOARD))
    assert item.status == 'consolidated' and item.artifact_id == 'bug:bug-context'
    assert item.evidence_refs == (f'kg:{capture.node_id}',)
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [[capture.node_id, 'canonical-bug']]
    history = await source_store.enumerate(BOARD)
    assert len(history) == 2 and history[0] == capture
    assert await deliver_capture_events(factory) == 1  # at-least-once event replay
    assert await worker.drain_once() == 0
    assert await source_store.enumerate(BOARD) == history


async def test_independent_post_done_capture_gets_distinct_work_and_preserves_legacy_debt(graph_runtime, work_store):
    from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
    from okto_pulse.core.kg.cognitive_closeout_production import open_cognitive_closeout_pending
    runtime, first, _, _ = graph_runtime
    factory, assembler, source_store, request = runtime
    store, discovery = work_store
    async with factory() as session:
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id='bug-context')
        second = await StageLearningCaptureUseCase().execute(replace(request, capture_id='post-done-worker',
            content='A distinct authored lesson.', expected_source_digest=source.source_digest,
            expected_source_version=source.source_policy_version), actor=actor(), uow=uow(session))
        await session.commit()
    open_cognitive_closeout_pending(board_id=BOARD, source_ref='bug:bug-context', artifact_type='bug', store=store)
    generation = store.latest_generation(BOARD)
    legacy, = store.list_items(BOARD, generation)
    legacy = store.update_item(board_id=BOARD, kg_generation_id=generation, item_id=legacy.item_id,
        new_status='pending', updated_by_agent_id='legacy', reason='Existing mixed hold; preserve history')
    assert await deliver_capture_events(factory) == 2
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 2
    items = store.list_items(BOARD, generation)
    assert len(items) == 3
    assert next(row for row in items if row.item_id == legacy.item_id) == legacy
    assert sum(row.status == 'consolidated' for row in items) == 2
    assert {row[0] for row in graph_rows('MATCH (n:Learning) RETURN n.id')} == {first.node_id, second.node_id}
    assert len(await source_store.enumerate(BOARD)) == 4


async def test_same_bug_reuse_outbox_work_materializes_after_first_capture_finished(graph_runtime, work_store):
    from test_learning_reuse_materialization import stage_reuse, associations
    runtime, original, _, _ = graph_runtime
    factory, _, source_store, _ = runtime
    store, discovery = work_store
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    await deliver_capture_events(factory)
    assert await worker.drain_once() == 1
    generation = store.latest_generation(BOARD)
    first, = store.list_items(BOARD, generation)
    assert first.status == 'consolidated'
    capture, _ = await stage_reuse(graph_runtime)
    assert await deliver_capture_events(factory) == 2
    items = store.list_items(BOARD, generation)
    assert first in items and len(items) == 2
    new, = [item for item in items if item.content_hash == capture.record_fingerprint]
    assert new.status == 'pending' and new.item_id != first.item_id
    assert await worker.drain_once() == 1
    history = await source_store.enumerate(BOARD)
    assert len(history) == 4
    assert max(history, key=lambda row: row.source_revision).payload['source_content_hash'] == capture.record_fingerprint
    assert associations() == {(original.node_id, 'canonical-bug')}
    assert all(item.status == 'consolidated' for item in store.list_items(BOARD, generation))
    await deliver_capture_events(factory)
    assert await worker.drain_once() == 0
    assert await source_store.enumerate(BOARD) == history
