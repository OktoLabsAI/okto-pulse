"""Recovery honors committed scope history; successor materialization is a fixture.

Original/reused Learning materialization, signed captures, history writer and
worker are real. The new successor literal is explicitly seeded, because its
governed materializer remains a separate integration dependency.
"""
from dataclasses import replace

import pytest

from okto_pulse.core.application.learning_supersedence import stage_learning_scope_replacement
from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
from okto_pulse.core.domain.learning_materialization import CapturedLearningProjection
from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from okto_pulse.core.ports.learning_capture import LearningCaptureIntent
from test_learning_capture_writer import BOARD, actor, uow
from test_learning_reuse_materialization import add_second_bug, stage_reuse, associations, graph_rows
from test_learning_materialization_worker import (
    runtime as _runtime, independent_gates as _independent_gates, graph_runtime as _graph_runtime,
    work_store as _work_store, deliver_capture_events,
)

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store
pytestmark = pytest.mark.asyncio


async def test_worker_does_not_recreate_replaced_scope_but_restores_uncovered_origin(graph_runtime, work_store):
    runtime, original, _, _ = graph_runtime
    factory, assembler, source_store, request = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    await add_second_bug(factory)
    await stage_reuse(graph_runtime, bug_id='second-bug')
    await deliver_capture_events(factory)
    assert await worker.drain_once() == 1
    assert associations() == {(original.node_id, 'canonical-bug'), (original.node_id, 'second-canonical-bug')}
    async with factory() as session:
        target = await source_store.read_latest_in_context(session, board_id=BOARD,
            node_id=original.node_id, generation=original.generation)
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id='second-bug')
        draft = replace(request, bug_id='second-bug', capture_id='second-bug-replacement',
            content='Use the corrected behavior in this deployment only.',
            expected_source_digest=source.source_digest, expected_source_version=source.source_policy_version,
            intent=LearningCaptureIntent('supersede', target.node_id, target.generation,
                target.record_fingerprint, 'Narrow replacement for this correction', 'source_bug'))
        capture = await StageLearningCaptureUseCase().execute(draft, actor=actor(), uow=uow(session))
        plan = CapturedLearningProjection(capture, capture, 'second-bug')
        successor = replace(capture, source_revision=capture.source_revision + 1, record_fingerprint='',
            evidence_refs=plan.evidence_refs, payload={**plan.fields, 'generation': capture.generation,
                'created_at': capture.payload['captured_at'], 'created_by_agent': capture.payload['author_id']})
        await stage_learning_scope_replacement(session, source_store,
            previous=target, capture=capture, successor=successor)
        await session.commit()
    history = await source_store.enumerate(BOARD)
    # Do not deliver the successor's event: that materializer is not claimed by
    # this test. Exercise only recovery of the two already materialized origins.
    graph_rows('MATCH (n:Learning) WHERE n.id = $id DETACH DELETE n', {'id': original.node_id})
    assert await worker.drain_once() == 2
    assert associations() == {(original.node_id, 'canonical-bug')}
    items = store.list_items(BOARD, store.latest_generation(BOARD))
    replaced, = [item for item in items if item.reason == 'learning_materialization_scope_replaced']
    assert replaced.status == 'consolidated' and replaced.outcome_type == 'no_action_required'
    assert replaced.evidence_refs == () and replaced.reason_code is None and replaced.actor is None
    from okto_pulse.core.kg.cognitive_readiness import compose_readiness
    def readiness(**changes):
        return compose_readiness(**(dict(artifact_id=replaced.artifact_id, technical_dlq=False,
            canonical_debt_open=False, cognitive_items=[replaced]) | changes))
    assert readiness().tier == 'terminal_history'
    assert readiness(technical_dlq=True).blocking
    assert readiness(canonical_debt_open=True).blocking
    assert readiness(cognitive_items=[replaced, replace(replaced, status='pending')]).blocking
    assert await source_store.enumerate(BOARD) == history
    assert await worker.drain_once() == 0
    assert associations() == {(original.node_id, 'canonical-bug')}
