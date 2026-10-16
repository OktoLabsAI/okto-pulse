"""M04/G12: native Done is preserved while current proof is re-evaluated."""
import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import ACTOR, read, setup
from test_delivery_readiness_batch import statements
from okto_pulse.community.adapters.sqlalchemy_models import AgentBoard, Card, Spec
from okto_pulse.core.ports.delivery_readiness import ReadinessActor, ReadinessReadError
from okto_pulse.core.services.delivery_readiness import EvaluatedDeliveryReadiness

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


@pytest.mark.parametrize('actor', [ACTOR, ReadinessActor('shared', 'human', 'local')])
@pytest.mark.parametrize('card_id', [None, 'task', 'test'])
async def test_G12_completed_native_snapshot_loses_proof_without_reopening_or_requesting_new_report(
        ledger, actor, card_id):
    session, store, _ = ledger
    await setup(session)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    # Seed the persisted result of an earlier native closeout. This test reads
    # historical completion; positive mutation acceptance is tested separately.
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(status='done'))
    await session.commit()
    with statements(session.bind) as sql:
        before = await read(session, actor=actor, card_id=card_id)
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert before.native_lifecycle.completion_recorded
    assert before.native_lifecycle.check_status == 'not_evaluated'
    assert not before.native_lifecycle.required_inputs
    assert ('spec', 'done') not in before.access.completion_states
    if actor.actor_kind == 'human':
        assert not before.access.spec_write_access
    reader = EvaluatedDeliveryReadiness(before, actor_id=actor.actor_id)
    context = reader.context(card_id)
    assert context.delivery_state == 'delivered' and context.lifecycle.qualification_is_current is True
    assert context.verification.percent == 100
    assert context.implementation.percent == (None if card_id == 'test' else 100)
    assert not reader.diagnostics(card_id).graph.actions
    assert (await read(session, actor=actor, card_id=card_id)).snapshot_id == before.snapshot_id

    await delivery.record(store, delivery.command('revoke', card_id='task',
        record_id=implementation['id']), human=True)
    await session.commit()
    with statements(session.bind) as sql:
        after = await read(session, actor=actor, card_id=card_id)
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert after.snapshot_id != before.snapshot_id
    reader = EvaluatedDeliveryReadiness(after, actor_id=actor.actor_id)
    current = reader.context(card_id)
    assert current.lifecycle.stored_status == 'done'
    assert current.lifecycle.qualification_is_current is False and not current.proof_complete
    assert current.summary.message_code == 'historical_completion_diverged'
    assert current.delivery_state != 'delivered' and current.verification.percent == 0
    assert all(action.kind != 'complete_lifecycle' for action in reader.diagnostics(card_id).graph.actions)
    assert (await session.scalar(select(Spec.status).where(Spec.id == delivery.SPEC_ID))).value == 'done'
    assert all(status.value == 'done' for status in (await session.scalars(select(Card.status))).all())
    assert not session.new and not session.dirty and not session.deleted


async def test_historical_done_never_bypasses_revoked_board_access(ledger):
    session, _, _ = ledger
    await setup(session)
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(status='done'))
    await session.execute(AgentBoard.__table__.delete().where(AgentBoard.agent_id == ACTOR.actor_id))
    await session.commit()
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await read(session)
