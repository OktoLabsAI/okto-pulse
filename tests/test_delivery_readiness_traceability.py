"""Readiness composes native source gates without altering delivery proof."""
from dataclasses import replace

import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import setup, read, ACTOR
from test_delivery_readiness_batch import statements
from okto_pulse.community.adapters.sqlalchemy_code_traceability import CommunitySqlAlchemyCodeTraceabilityStore
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card
from okto_pulse.core.domain.code_traceability import CodeTraceabilitySubjectType
from okto_pulse.core.services.main import evaluate_code_traceability_transition
from okto_pulse.core.services.delivery_readiness import EvaluatedDeliveryReadiness

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


@pytest.mark.parametrize('mode', ['advisory', 'blocking'])
async def test_native_unexecuted_target_has_same_readiness_blocker_and_no_proof_penalty(ledger, mode):
    session, store, _ = ledger
    await setup(session)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.execute(update(Board).where(Board.id == delivery.BOARD_ID).values(
        settings={'require_task_validation': False, 'code_traceability': {'mode': mode}}))
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    await session.commit()
    before = await read(session, card_id='task')
    targets = CommunitySqlAlchemyCodeTraceabilityStore(session)
    target = await targets.get_target(board_id=delivery.BOARD_ID, target_id='target')
    await targets.create_target(target=replace(target, id='private-unexecuted-target',
        relative_path_hint='src/private-pending.py'), expected_head_revision=1, expected_spec_version=1)
    await session.commit()
    card = await session.get(Card, 'task')
    board = await session.get(Board, delivery.BOARD_ID)
    with statements(session.bind) as sql:
        native = await evaluate_code_traceability_transition(session, board=board, subject=card,
            subject_type=CodeTraceabilitySubjectType.CARD, from_status='in_progress', to_status='done')
        after = await read(session, card_id='task')
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert after.snapshot_id != before.snapshot_id
    expected = tuple(sorted({item.code for item in native.blockers if item.blocking}))
    assert after.native_lifecycle.blocking_codes == expected
    if mode == 'blocking':
        assert 'target_execution_disposition_required' in expected
    else:
        assert not expected
    context = EvaluatedDeliveryReadiness(after, actor_id=ACTOR.actor_id).context('task')
    assert context.implementation.percent == context.verification.percent == 100
    assert 'private-unexecuted-target' not in context.model_dump_json()
    assert 'private-pending.py' not in context.model_dump_json()
    assert card.status.value == 'in_progress'
    assert not session.new and not session.dirty and not session.deleted
