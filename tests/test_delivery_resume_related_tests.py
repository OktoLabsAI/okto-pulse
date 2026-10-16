import pytest
from sqlalchemy import update

import test_multicard_delivery_integration as multi
from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery

base_ledger = multi.base_ledger
ledger = multi.ledger


def query(card_id):
    return DeliveryEvidenceReadQuery(board_id=multi.BOARD, spec_id=multi.SPEC, card_id=card_id, view='resume')


@pytest.mark.asyncio
async def test_related_signed_tests_respect_split_contributions_and_inherited_rule(ledger, tmp_path, monkeypatch):
    session, store = await multi.setup(ledger)
    ui = await multi.delivery.record(store, multi.implementation())
    auth = await multi.delivery.record(store, multi.implementation('authorization'))
    await multi.bind_test(session, store, tmp_path, 'ui', [ui['id']])
    await multi.bind_test(session, store, tmp_path, 'auth', [auth['id']], passed=False)
    await session.commit()
    async def forbidden(*args, **kwargs):
        raise AssertionError('Resume must not load the whole Spec rollup')
    monkeypatch.setattr(store, 'load_rollup_snapshot', forbidden)
    ui_context = await store.card_resume(query('task'), actor_id='successor')
    auth_context = await store.card_resume(query('authorization'), actor_id='successor')
    assert [row['scenario_id'] for row in ui_context['tests']['items']] == ['ts-ui']
    assert ui_context['tests']['items'][0]['current_verified_run']
    assert ui_context['tests']['items'][0]['observes_this_card']
    assert {row['ref'] for row in ui_context['obligations']['items']} == set(multi.UI_REFS)
    assert [row['scenario_id'] for row in auth_context['tests']['items']] == ['ts-auth']
    assert auth_context['tests']['items'][0]['result'] == 'failed'
    assert auth_context['tests']['items'][0]['current_verified_run']
    assert not next(row for row in auth_context['obligations']['items'] if row['ref'] == 'br:br')['test_satisfied']
    assert auth_context['follow_up']['test_card_ledgers'][0]['card_id'] == 'test'
    assert auth_context['follow_up']['targets']['card_id'] == 'authorization'


@pytest.mark.asyncio
async def test_missing_test_owner_is_unknown_work_not_zero_verification(ledger):
    session, store = await multi.setup(ledger)
    await session.execute(update(Card).where(Card.id == 'test').values(status='cancelled'))
    await session.commit()
    context = await store.card_resume(query('task'), actor_id='successor')
    assert not context['tests']['total_exact']
    assert not context['obligations']['complete']
    assert context['verification_plan']['items'][0]['test_card_ids'] == []
    assert 'verification_test_card_required' in context['verification_plan']['items'][0]['blockers']
