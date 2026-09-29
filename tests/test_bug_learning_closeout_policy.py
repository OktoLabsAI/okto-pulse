"""Real SQL closeout policy. Independent existing gate aggregate is a fixture."""
# ruff: noqa: F811 -- imported fixtures are injected by pytest.
import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.application.use_cases.card_crud import (
    MoveCardCommand, MoveCardUseCase, SubmitTaskValidationCommand, SubmitTaskValidationUseCase,
)
from test_learning_submission_writer import runtime, independent_gates, prepare, unit, author  # noqa: F401
from test_learning_capture_validation_binding import prepare as validation_prepare, reviewer, validation_uow
from test_learning_capture_writer import BOARD


@pytest.mark.parametrize('policy', [None, 'advisory', 'blocking'])
async def test_missing_learning_direct_done_obeys_effective_policy(runtime, independent_gates, policy):
    factory, _, store, _ = runtime
    bug_id, data = await prepare(runtime, 'done', learning_policy=policy)
    data = data.model_copy(update={'learning_submission': None})
    async with factory() as session:
        uow = unit(session)
        if policy == 'blocking':
            with pytest.raises(ValueError, match='valid Learning') as error:
                await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(), uow=uow)
            assert error.value.code == 'bug_learning_capture_required'
            await session.commit()
            bug = await session.get(Card, bug_id)
            assert bug.status.value == 'validation' and not bug.conclusions
        else:
            await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(), uow=uow)
            assert (await session.get(Card, bug_id)).status.value == 'done'
    assert await store.enumerate(BOARD) == ()


@pytest.mark.parametrize('policy', [None, 'advisory', 'blocking'])
async def test_unselected_capture_is_not_silent_proof_for_validation(runtime, policy):
    factory, _, _, _ = runtime
    request, data = await validation_prepare(runtime, learning_policy=policy)
    # A capture exists, but the caller supplied no authenticated selection.
    # A queue count or any convenient historical row cannot satisfy blocking.
    data.pop('learning_capture')
    async with factory() as session:
        result = await SubmitTaskValidationUseCase().execute(
            SubmitTaskValidationCommand(request.bug_id, data), actor=reviewer(), uow=validation_uow(session))
        expected = 'rejected' if policy == 'blocking' else 'completed'
        assert result.validation['completion_outcome'] == expected
        assert result.validation['validation_outcome'] == 'success'
        if policy == 'blocking':
            assert result.validation['completion_gate_failures'][0]['code'] == 'bug_learning_capture_required'
        bug = await session.get(Card, request.bug_id)
        assert bug.status.value == ('rejected' if policy == 'blocking' else 'done')
        assert not bug.learning_closeout_bindings
