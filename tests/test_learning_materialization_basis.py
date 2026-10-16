"""Relational worker admission, not canonical graph materialization."""
# ruff: noqa: F811 -- imported fixtures are injected by pytest.
from dataclasses import replace

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.application.learning_capture import revalidate_learning_capture_for_materialization
from okto_pulse.core.application.use_cases.card_crud import MoveCardCommand, MoveCardUseCase
from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
from test_learning_submission_writer import runtime, independent_gates, prepare, unit, author  # noqa: F401
from test_learning_capture_writer import BOARD, actor, uow


async def qualify(session, bug_id, record):
    return await revalidate_learning_capture_for_materialization(session, board_id=BOARD,
        bug_id=bug_id, learning_id=record.node_id, generation=record.generation,
        expected_fingerprint=record.record_fingerprint)


@pytest.mark.parametrize('damage', [None, 'reopen', 'changed_report', 'missing_binding', 'changed_version'])
async def test_closed_capture_requires_current_relational_basis(runtime, independent_gates, damage):
    factory, _, store, _ = runtime
    bug_id, data = await prepare(runtime, 'done')
    async with factory() as session:
        await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(), uow=unit(session))
        record, = await store.enumerate(BOARD)
        bug = await session.get(Card, bug_id)
        if damage == 'reopen':
            bug.status = 'in_progress'
        elif damage == 'changed_report':
            bug.conclusions = [{'text': 'Changed correction.'}]
        elif damage == 'missing_binding':
            bug.learning_closeout_bindings = None
        elif damage == 'changed_version':
            bug.policy_version += 1
        if damage:
            # Simulated changed authoritative state, not lifecycle authorization.
            await session.commit()
            with pytest.raises(ValueError, match='learning_materialization_(source_not_eligible|current_binding_required)'):
                await qualify(session, bug_id, record)
        else:
            result = await qualify(session, bug_id, record)
            assert result.source.status == 'done'
            assert result.capture == record
            assert result.closeout_transition_id == bug.learning_closeout_bindings[0]['transition_id']
        await session.rollback()
    assert await store.enumerate(BOARD) == (record,)


async def test_learning_can_be_authored_after_done_without_reopening_or_backfill(runtime, independent_gates):
    factory, assembler, store, draft = runtime
    bug_id, data = await prepare(runtime, 'done')
    data = data.model_copy(update={'learning_submission': None})
    async with factory() as session:
        await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(), uow=unit(session))
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug_id)
        request = replace(draft, expected_source_digest=source.source_digest,
            expected_source_version=source.source_policy_version)
        record = await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))
        await session.commit()
        stored_before = await store.enumerate(BOARD)
        assert len(stored_before) == 1
        assert stored_before[0].payload == record.payload
        assert stored_before[0].record_fingerprint == record.record_fingerprint
        result = await qualify(session, bug_id, record)
        assert result.source.status == 'done' and result.closeout_transition_id is None
        assert not (await session.get(Card, bug_id)).learning_closeout_bindings
        await session.rollback()
    assert await store.enumerate(BOARD) == stored_before
