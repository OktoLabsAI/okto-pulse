"""The native critical-context preview has no mutation audit side effects."""
import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_batch import statements
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card
from okto_pulse.community.adapters.sqlalchemy_critical_context import CommunitySqlAlchemyCriticalContextReader
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import CommunitySqlAlchemyDomainEventFactReader
from okto_pulse.core.ports.critical_context import register_critical_context_read_port
from okto_pulse.core.ports.domain_event_delivery import register_domain_event_fact_reader
from okto_pulse.core.services.critical_context_guard import CriticalAction, FullContextUnavailableError
from okto_pulse.core.services.main import evaluate_critical_context

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


@pytest.mark.parametrize('entity_type,identity', [('card', 'task'), ('spec', delivery.SPEC_ID)])
async def test_native_context_resolves_stably_without_audit_writes(ledger, entity_type, identity):
    session, _, _ = ledger
    register_critical_context_read_port(CommunitySqlAlchemyCriticalContextReader())
    register_domain_event_fact_reader(CommunitySqlAlchemyDomainEventFactReader())
    kwargs = dict(board_id=delivery.BOARD_ID, actor_id='owner', entity_type=entity_type,
        entity_id=identity, critical_action=CriticalAction.CARD_CLOSEOUT if entity_type == 'card' else CriticalAction.SPEC_CLOSEOUT)
    with statements(session.bind) as sql:
        before = await evaluate_critical_context(session, **kwargs)
        repeated = await evaluate_critical_context(session, **kwargs)
    assert all(s.lstrip().upper().startswith('SELECT') for s in sql)
    assert before.outcome == 'allow' and before.reason == 'full_context_resolved'
    assert before.context_fingerprint == repeated.context_fingerprint
    await session.execute(update(Card).where(Card.id == 'task').values(title='Changed private Card title'))
    await session.commit()
    with statements(session.bind) as sql:
        changed = await evaluate_critical_context(session, **kwargs)
    assert all(s.lstrip().upper().startswith('SELECT') for s in sql)
    assert changed.context_fingerprint != before.context_fingerprint
    assert 'Changed private' not in repr(changed)
    assert not session.new and not session.dirty and not session.deleted


async def test_native_guard_disabled_and_unavailable_do_not_write_audit_events(ledger):
    session, _, _ = ledger
    register_critical_context_read_port(CommunitySqlAlchemyCriticalContextReader())
    register_domain_event_fact_reader(CommunitySqlAlchemyDomainEventFactReader())
    kwargs = dict(board_id=delivery.BOARD_ID, actor_id='owner', entity_type='card',
        entity_id='absent-card', critical_action=CriticalAction.CARD_CLOSEOUT)
    with statements(session.bind) as sql, pytest.raises(FullContextUnavailableError):
        await evaluate_critical_context(session, **kwargs)
    assert all(s.lstrip().upper().startswith('SELECT') for s in sql)
    await session.execute(update(Board).where(Board.id == delivery.BOARD_ID).values(
        settings={'require_full_context_for_critical_actions': False}))
    await session.commit()
    with statements(session.bind) as sql:
        disabled = await evaluate_critical_context(session, **kwargs)
    assert all(s.lstrip().upper().startswith('SELECT') for s in sql)
    assert disabled.outcome == 'allow' and disabled.reason == 'guard_disabled'
    assert disabled.context_fingerprint is None
    assert not session.new and not session.dirty and not session.deleted
