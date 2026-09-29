"""Reuse captured before Done binds the exact admitted lifecycle delta."""
from dataclasses import replace

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.application.use_cases.card_crud import SubmitTaskValidationCommand, SubmitTaskValidationUseCase
from okto_pulse.core.domain.learning_closeout import LearningCaptureSelection, closeout_binding_is_current
from okto_pulse.core.ports.learning_capture import LearningCaptureIntent
from test_learning_capture_validation_binding import prepare, reviewer, validation_uow
from test_learning_reuse_materialization import (
    BOARD, add_second_bug, associations, graph_rows,
    graph_runtime as _graph_runtime, runtime as _runtime, independent_gates as _independent_gates,
)

graph_runtime = _graph_runtime
runtime = _runtime
independent_gates = _independent_gates
pytestmark = pytest.mark.asyncio


async def prepare_reuse(graph_runtime, policy=None):
    runtime, original, original_selection, persister = graph_runtime
    factory, assembler, store, request = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', original_selection)
    await add_second_bug(factory)
    async with factory() as session:
        target = await store.read_latest_in_context(session, board_id=BOARD,
            node_id=original.node_id, generation=original.generation)
    draft = replace(request, bug_id='second-bug', capture_id='reuse-before-done',
        content=target.payload['content'], intent=LearningCaptureIntent('reuse', target.node_id,
            target.generation, target.record_fingerprint, 'Applies to the second corrected Bug'))
    _, data = await prepare((factory, assembler, store, draft), learning_policy=policy)
    return data, LearningCaptureSelection(**data['learning_capture'])


@pytest.mark.parametrize('policy', [None, 'advisory', 'blocking'])
async def test_reuse_waits_for_done_then_materializes_the_bound_revision(graph_runtime, policy):
    runtime, original, _, persister = graph_runtime
    factory, assembler, store, _ = runtime
    data, selection = await prepare_reuse(graph_runtime, policy)
    history = await store.enumerate(BOARD)
    assert len(history) == 3 and associations() == {(original.node_id, 'canonical-bug')}
    with pytest.raises(ValueError, match='source_not_eligible'):
        await persister.persist_authored_learning(BOARD, 'second-bug', selection)
    async with factory() as session:
        unit = validation_uow(session)
        command = SubmitTaskValidationCommand('second-bug', data)
        result = await SubmitTaskValidationUseCase().execute(command, actor=reviewer(), uow=unit)
        assert result.validation['completion_outcome'] == 'completed'
        bug = await session.get(Card, 'second-bug')
        assert bug.status.value == 'done' and len(bug.learning_closeout_bindings) == 1
        binding = bug.learning_closeout_bindings[0]
        assert binding['capture'] == selection.model_dump() and binding['capture_revision'] == 2
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug.id)
        assert closeout_binding_is_current(binding, source)
        replay = await SubmitTaskValidationUseCase().execute(command, actor=reviewer(), uow=unit)
        assert replay.validation['replayed'] is True
    assert await store.enumerate(BOARD) == history
    assert await persister.persist_authored_learning(BOARD, 'second-bug', selection, raise_failures=True)
    assert associations() == {(original.node_id, 'canonical-bug'), (original.node_id, 'second-canonical-bug')}
    after = await store.enumerate(BOARD)
    assert len(after) == 4 and all(row in after for row in history)
    async with factory() as session:
        bug = await session.get(Card, 'second-bug')
        bug.status = 'in_progress'  # An already authorized reopen, not its gate proof.
        await session.commit()
    with pytest.raises(ValueError, match='source_not_eligible'):
        await persister.persist_authored_learning(BOARD, 'second-bug', selection)
    assert await store.enumerate(BOARD) == after


@pytest.mark.parametrize('damage', ['basis', 'head', 'late_binding'])
async def test_reuse_closeout_failure_keeps_capture_history_and_existing_projection(graph_runtime, damage, monkeypatch):
    runtime, original, _, _ = graph_runtime
    factory, _, store, _ = runtime
    data, selection = await prepare_reuse(graph_runtime)
    if damage == 'head':
        async with factory() as session:
            head = await store.read_latest_in_context(session, board_id=BOARD,
                node_id=selection.learning_id, generation=selection.generation)
            changed = replace(head, record_fingerprint='', payload={**head.payload, 'applicability': 'Changed scope'})
            await store.append_many_if_current_in_context(session, (changed,),
                expected_fingerprints=(head.record_fingerprint,))
            await session.commit()
    elif damage == 'basis':
        async with factory() as session:
            bug = await session.get(Card, 'second-bug')
            bug.action_plan = 'A different correction after capture'
            await session.commit()
    else:
        from okto_pulse.core.domain import learning_closeout
        def fail(**kwargs):
            raise RuntimeError('injected_reuse_binding_failure')
        monkeypatch.setattr(learning_closeout, 'bind_learning_capture_to_closed_source', fail)
    history = await store.enumerate(BOARD)
    expected = {'basis': 'task_validation_subject_version_conflict', 'head': 'learning_capture_selection_changed',
        'late_binding': 'injected_reuse_binding_failure'}[damage]
    async with factory() as session:
        with pytest.raises((ValueError, RuntimeError)) as failure:
            await SubmitTaskValidationUseCase().execute(SubmitTaskValidationCommand('second-bug', data),
                actor=reviewer(), uow=validation_uow(session))
        assert getattr(failure.value, 'code', str(failure.value)) == expected
    async with factory() as session:
        bug = await session.get(Card, 'second-bug')
        assert bug.status.value == 'validation' and not bug.learning_closeout_bindings and not bug.validations
    assert await store.enumerate(BOARD) == history
    assert associations() == {(original.node_id, 'canonical-bug')}
    assert len(graph_rows('MATCH (n:Learning) RETURN n.id')) == 1
