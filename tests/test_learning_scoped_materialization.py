"""Scoped supersedence through real signed admission, SQL/Grafx and recovery.

The inherited fixture isolates unrelated completion gates and health. No
successor literal or target claim is seeded by these integration tests.
"""
from dataclasses import replace

import pytest

from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
from okto_pulse.core.domain.learning_closeout import LearningCaptureSelection
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


async def prepare_replacement(graph_runtime):
    runtime, original, selection, persister = graph_runtime
    factory, assembler, store, request = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection, raise_failures=True)
    await add_second_bug(factory)
    _, reused = await stage_reuse(graph_runtime, bug_id='second-bug')
    assert await persister.persist_authored_learning(BOARD, 'second-bug', reused, raise_failures=True)
    async with factory() as session:
        target = await store.read_latest_in_context(session, board_id=BOARD,
            node_id=original.node_id, generation=original.generation)
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id='second-bug')
        draft = replace(request, bug_id='second-bug', capture_id='scoped-materialization',
            content='A correction applicable to the second deployment only.',
            expected_source_digest=source.source_digest, expected_source_version=source.source_policy_version,
            intent=LearningCaptureIntent('supersede', target.node_id, target.generation,
                target.record_fingerprint, 'Replace only this explicitly selected origin', 'source_bug'))
        capture = await StageLearningCaptureUseCase().execute(draft, actor=actor(), uow=uow(session))
        await session.commit()
    return capture, LearningCaptureSelection(learning_id=capture.node_id, generation=capture.generation,
        fingerprint=capture.record_fingerprint), target


async def test_scoped_commit_preserves_other_origin_and_replays_joint_history(graph_runtime):
    runtime, original, _, persister = graph_runtime
    factory, _, store, _ = runtime
    capture, selection, previous = await prepare_replacement(graph_runtime)
    before = await store.enumerate(BOARD)
    assert await persister.persist_authored_learning(BOARD, 'second-bug', selection, raise_failures=True)
    after = await store.enumerate(BOARD)
    assert len(after) == len(before) + 2 and all(row in after for row in before)
    assert associations() == {(original.node_id, 'canonical-bug'), (capture.node_id, 'second-canonical-bug')}
    assert graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN n.superseded_by', {'id': original.node_id}) == [[None]]
    from okto_pulse.core.application.learning_supersedence import read_learning_scope_replacements
    async with factory() as session:
        head = await store.read_latest_in_context(session, board_id=BOARD, node_id=previous.node_id,
            generation=previous.generation)
        claim, = await read_learning_scope_replacements(session, store, head=head)
        assert claim.previous == previous
        # SQL's operational committed_at codec may omit the UTC suffix; the
        # fingerprint and authored payload (including captured_at) remain exact.
        assert claim.capture.record_fingerprint == capture.record_fingerprint
        assert claim.capture.payload == capture.payload
        assert claim.claimed.payload == previous.payload
        assert claim.successor.payload['content'] == capture.payload['content']
    assert await persister.persist_authored_learning(BOARD, 'second-bug', selection, raise_failures=True)
    assert await store.enumerate(BOARD) == after


async def test_worker_restores_missing_nodes_without_recreating_replaced_origin(graph_runtime, work_store):
    from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
    runtime, original, _, persister = graph_runtime
    factory, _, source_store, _ = runtime
    capture, selection, _ = await prepare_replacement(graph_runtime)
    assert await persister.persist_authored_learning(BOARD, 'second-bug', selection, raise_failures=True)
    history = await source_store.enumerate(BOARD)
    graph_rows('MATCH (n:Learning) DETACH DELETE n')
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 3
    assert associations() == {(original.node_id, 'canonical-bug'), (capture.node_id, 'second-canonical-bug')}
    items = store.list_items(BOARD, store.latest_generation(BOARD))
    assert all(item.status == 'consolidated' for item in items)
    assert sum(item.reason == 'learning_materialization_scope_replaced' for item in items) == 1
    assert await source_store.enumerate(BOARD) == history
    assert await worker.drain_once() == 0


async def test_joint_append_failure_compensates_successor_and_restores_old_association(graph_runtime, monkeypatch):
    runtime, original, _, persister = graph_runtime
    _, _, store, _ = runtime
    capture, selection, _ = await prepare_replacement(graph_runtime)
    history, edges = await store.enumerate(BOARD), associations()
    calls = []
    async def fail(context, records, *, expected_fingerprints):
        calls.append((records, expected_fingerprints))
        raise RuntimeError('injected_scoped_joint_append_failure')
    with monkeypatch.context() as patch:
        patch.setattr(store, 'append_many_if_current_in_context', fail)
        assert not await persister.persist_authored_learning(BOARD, 'second-bug', selection)
    assert len(calls) == 1 and len(calls[0][0]) == 2
    assert {record.node_id for record in calls[0][0]} == {original.node_id, capture.node_id}
    assert associations() == edges and await store.enumerate(BOARD) == history
    assert graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN n.id', {'id': capture.node_id}) == []
    assert await persister.persist_authored_learning(BOARD, 'second-bug', selection, raise_failures=True)


@pytest.mark.parametrize('change', ['source_revision', 'graph_curation'])
async def test_target_change_after_capture_cannot_be_overwritten(graph_runtime, change):
    runtime, _, _, persister = graph_runtime
    factory, _, store, _ = runtime
    capture, selection, target = await prepare_replacement(graph_runtime)
    if change == 'source_revision':
        async with factory() as session:
            newer = replace(target, payload={**target.payload, 'context': 'Revised by another authorized writer'},
                source_revision=target.source_revision + 1, record_fingerprint='')
            await store.append_many_if_current_in_context(session, (newer,),
                expected_fingerprints=(target.record_fingerprint,))
            await session.commit()
    else:
        graph_rows('MATCH (n:Learning) WHERE n.id = $id SET n.content = $content, n.human_curated = true',
            {'id': target.node_id, 'content': 'Concurrent graph curation'})
    history, edges = await store.enumerate(BOARD), associations()
    if change == 'source_revision':
        with pytest.raises(ValueError, match='learning_capture_target_changed'):
            await persister.persist_authored_learning(BOARD, 'second-bug', selection)
    else:
        assert not await persister.persist_authored_learning(BOARD, 'second-bug', selection)
    assert await store.enumerate(BOARD) == history and associations() == edges
    assert graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN n.id', {'id': capture.node_id}) == []


async def test_late_relational_commit_failure_rolls_back_both_heads_and_graph(graph_runtime, monkeypatch):
    from okto_pulse.core.ports.consolidation import get_consolidation_persistence_port
    runtime, _, _, persister = graph_runtime
    _, _, store, _ = runtime
    capture, selection, target = await prepare_replacement(graph_runtime)
    history, edges = await store.enumerate(BOARD), associations()
    port = get_consolidation_persistence_port()
    commit = port.commit
    failures = []
    async def fail_after_staging(context):
        successor = await store.read_latest_in_context(context, board_id=BOARD,
            node_id=capture.node_id, generation=capture.generation)
        if successor is not None and successor.payload.get('source_content_hash') == capture.record_fingerprint:
            claimed = await store.read_latest_in_context(context, board_id=BOARD,
                node_id=target.node_id, generation=target.generation)
            assert claimed.record_fingerprint != target.record_fingerprint
            assert (target.node_id, 'second-canonical-bug') not in associations()
            assert (capture.node_id, 'second-canonical-bug') in associations()
            failures.append(True)
            raise RuntimeError('injected_scoped_outer_commit_failure')
        await commit(context)
    with monkeypatch.context() as patch:
        patch.setattr(port, 'commit', fail_after_staging)
        assert not await persister.persist_authored_learning(BOARD, 'second-bug', selection)
    assert failures == [True]
    assert await store.enumerate(BOARD) == history and associations() == edges
    assert graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN n.id', {'id': capture.node_id}) == []
