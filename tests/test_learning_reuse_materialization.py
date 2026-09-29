"""Explicit reuse on real SQL/Grafx; completion gates retain fixture limits."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
from okto_pulse.core.domain.learning_closeout import LearningCaptureSelection
from okto_pulse.core.ports.learning_capture import LearningCaptureIntent
from test_learning_capture_writer import BOARD, actor, uow
from test_learning_materialization_writer import (
    graph_runtime as _graph_runtime, graph_rows, runtime as _runtime, independent_gates as _independent_gates,
)

graph_runtime = _graph_runtime
runtime = _runtime
independent_gates = _independent_gates
pytestmark = pytest.mark.asyncio


async def stage_reuse(graph_runtime, *, bug_id='bug-context', capture_id='reuse-1'):
    runtime, original, _, _ = graph_runtime
    factory, assembler, store, request = runtime
    async with factory() as session:
        target = await store.read_latest_in_context(session, board_id=BOARD,
            node_id=original.node_id, generation=original.generation)
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug_id)
        draft = replace(request, bug_id=bug_id, capture_id=capture_id,
            content=target.payload['content'], expected_source_digest=source.source_digest,
            expected_source_version=source.source_policy_version,
            context='Explicit reuse in another deployment', applicability='Verified corrected deployment',
            intent=LearningCaptureIntent('reuse', target.node_id, target.generation,
                target.record_fingerprint, 'The lesson applies to this correction'))
        record = await StageLearningCaptureUseCase().execute(draft, actor=actor(), uow=uow(session))
        await session.commit()
    return record, LearningCaptureSelection(learning_id=record.node_id,
        generation=record.generation, fingerprint=record.record_fingerprint)


async def add_second_bug(factory):
    from okto_pulse.community.adapters.sqlalchemy_models import Card
    async with factory() as session:
        session.add(Card(id='second-bug', board_id=BOARD, spec_id='spec-bug-context',
            title='Same metadata failure in another deployment', status='done', card_type='bug',
            origin_task_id='origin-task', expected_behavior='Correct release metadata',
            observed_behavior='Stale release metadata', steps_to_reproduce='Inspect About',
            action_plan='Correct the generated release metadata and inspect the rebuilt application.',
            linked_test_task_ids=['regression-test'], test_scenario_ids=['scenario-regression'],
            conclusions=[{'text': 'Corrected metadata verified.'}],
            validations=[{'outcome': 'success'}], created_by='author'))
        await session.commit()
    graph_rows("CREATE (b:Bug {id: 'second-canonical-bug', title: 'Second Bug', "
        "source_artifact_ref: 'bug:second-bug', graph_layer: 'canonical', maturity_status: 'canonical_eligible'})")


def associations():
    return {tuple(row) for row in graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id')}


async def test_reuse_adds_only_explicit_bug_and_old_recovery_preserves_latest_literal(graph_runtime):
    runtime, original, selection, persister = graph_runtime
    factory, _, store, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection, raise_failures=True)
    history = await store.enumerate(BOARD)
    previous = max(history, key=lambda row: row.source_revision)
    await add_second_bug(factory)
    capture, reused = await stage_reuse(graph_runtime, bug_id='second-bug')
    assert await persister.persist_authored_learning(BOARD, 'second-bug', reused, raise_failures=True)
    after = await store.enumerate(BOARD)
    assert len(after) == 4 and all(row in after for row in history)
    head = max(after, key=lambda row: row.source_revision)
    assert head.source_revision == 3 and head.payload['source_content_hash'] == capture.record_fingerprint
    for key in ('content', 'context', 'title', 'created_by_agent', 'created_at', 'source_artifact_ref', 'generation', 'embedding'):
        assert head.payload[key] == previous.payload[key]
    expected = {(original.node_id, 'canonical-bug'), (original.node_id, 'second-canonical-bug')}
    assert associations() == expected
    for bug_id in ('bug-context', 'second-bug'):
        work = SimpleNamespace(learning_id=original.node_id, bug_id=bug_id)
        assert await persister.inspect_authored_learning(BOARD, work) == 'present'
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection, raise_failures=True)
    assert await persister.persist_authored_learning(BOARD, 'second-bug', reused, raise_failures=True)
    assert await store.enumerate(BOARD) == after
    graph_rows('MATCH (n:Learning) WHERE n.id = $id DETACH DELETE n', {'id': original.node_id})
    # Old work restores the latest literal but only its currently proved edge;
    # new work independently revalidates its own Bug before restoring that edge.
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection, raise_failures=True)
    assert associations() == {(original.node_id, 'canonical-bug')}
    assert await persister.persist_authored_learning(BOARD, 'second-bug', reused, raise_failures=True)
    assert associations() == expected and await store.enumerate(BOARD) == after


async def test_same_bug_reuse_materializes_a_new_literal_and_replays_without_duplicate_history(graph_runtime):
    runtime, _, selection, persister = graph_runtime
    _, _, store, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    capture, reused = await stage_reuse(graph_runtime)
    assert await persister.persist_authored_learning(BOARD, 'bug-context', reused, raise_failures=True)
    history = await store.enumerate(BOARD)
    assert len(history) == 4
    assert max(history, key=lambda row: row.source_revision).payload['source_content_hash'] == capture.record_fingerprint
    assert len(associations()) == 1
    assert await persister.persist_authored_learning(BOARD, 'bug-context', reused, raise_failures=True)
    assert await store.enumerate(BOARD) == history


async def test_reuse_failure_compensates_provenance_and_new_edge_without_losing_old_association(graph_runtime, monkeypatch):
    runtime, original, selection, persister = graph_runtime
    factory, _, store, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    before_hash = graph_rows('MATCH (n:Learning) RETURN n.source_content_hash')
    await add_second_bug(factory)
    _, reused = await stage_reuse(graph_runtime, bug_id='second-bug')
    history = await store.enumerate(BOARD)
    calls = []
    async def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('injected_reuse_append_failure')
    monkeypatch.setattr(store, 'append_many_if_current_in_context', fail)
    assert not await persister.persist_authored_learning(BOARD, 'second-bug', reused)
    assert calls == [True]
    assert graph_rows('MATCH (n:Learning) RETURN n.source_content_hash') == before_hash
    assert associations() == {(original.node_id, 'canonical-bug')}
    assert await store.enumerate(BOARD) == history


async def test_reuse_refuses_graph_curated_after_target_admission(graph_runtime):
    runtime, original, selection, persister = graph_runtime
    _, _, store, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    _, reused = await stage_reuse(graph_runtime)
    history = await store.enumerate(BOARD)
    graph_rows('MATCH (n:Learning) WHERE n.id = $id SET n.content = $content, n.human_curated = true',
        {'id': original.node_id, 'content': 'Human correction after admission'})
    assert not await persister.persist_authored_learning(BOARD, 'bug-context', reused)
    assert await store.enumerate(BOARD) == history
    assert graph_rows('MATCH (n:Learning) RETURN n.content') == [['Human correction after admission']]


async def test_old_work_waits_for_admitted_reuse_then_recovers_current_revision(graph_runtime):
    from okto_pulse.core.application.learning_materialization_worker import materialize_capture_work
    from okto_pulse.core.domain.learning_materialization_work import LearningCaptureWorkRef
    runtime, original, selection, persister = graph_runtime
    factory, _, store, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    _, reused = await stage_reuse(graph_runtime)
    history = await store.enumerate(BOARD)
    work = LearningCaptureWorkRef('bug-context', original.node_id, original.generation)
    waiting = await materialize_capture_work(factory, board_id=BOARD, work=work,
        fingerprint=selection.fingerprint, persister=persister)
    assert (waiting.outcome, waiting.reason) == (
        'materialization_pending', 'learning_materialization_projection_pending')
    assert await store.enumerate(BOARD) == history
    assert await persister.persist_authored_learning(BOARD, 'bug-context', reused, raise_failures=True)
    completed = await materialize_capture_work(factory, board_id=BOARD, work=work,
        fingerprint=selection.fingerprint, persister=persister)
    assert completed.outcome == 'persisted'
