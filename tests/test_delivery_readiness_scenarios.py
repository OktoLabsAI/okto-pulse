"""Scenario metrics from actual SQL snapshots and native signed reports."""

from copy import deepcopy

import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_verification import prepare_reports
from okto_pulse.community.adapters.sqlalchemy_models import Card, CardDeliveryEvidenceRecordRow, Spec
from okto_pulse.core.domain.delivery_assessment import (
    assess_test_delivery, assess_obligation_delivery, aggregate_delivery_metrics,
)


ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


async def read_metrics(store, session):
    snapshot, _ = await store.load_rollup_snapshot(delivery.BOARD_ID, delivery.SPEC_ID)
    card = await session.get(Card, 'test', populate_existing=True)
    assessment = assess_test_delivery(snapshot, card_id=card.id, scenario_ids=tuple(card.test_scenario_ids))
    return snapshot, assessment, aggregate_delivery_metrics(assessment)


@pytest.mark.parametrize('last,reason', [('failed', 'verification_failed'),
    ('inconclusive', 'verification_inconclusive'), (None, 'verification_not_run'),
    ('passed', 'verification_association_missing')])
async def test_four_scenarios_count_once_each_while_shared_obligations_remain_pending(ledger, tmp_path, last, reason):
    session, store, refs, impl, scenarios = await prepare_reports(
        ledger, tmp_path, 'inspection', ['passed', 'passed', 'passed', last])
    for index, scenario in enumerate(scenarios[:3]):
        await delivery.record(store, delivery.command('test', idempotency_key=f'run-{index}',
            scenario_id=scenario['id'], obligation_refs=refs, implementation_ids=[impl['id']]))
    await session.commit()
    before = deepcopy([(row.id, row.payload) for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))])
    snapshot, result, metrics = await read_metrics(store, session)
    assert metrics.implementation.status == 'not_applicable' and metrics.implementation.percent is None
    assert metrics.verification.total == 4 and metrics.verification.percent == 75
    last_row = next(row for row in result.scenarios if row.scenario_id == scenarios[-1]['id'])
    assert reason in {item.code for item in last_row.verification.reasons}
    assert bool(last_row.semantic_sha256)
    assert aggregate_delivery_metrics(assess_obligation_delivery(snapshot)).verification.percent == 0
    assert [(row.id, row.payload) for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))] == before


async def test_existing_signed_result_binding_advances_card_without_rerun_or_done(ledger, tmp_path):
    session, store, refs, impl, scenarios = await prepare_reports(ledger, tmp_path, 'inspection', ['passed', 'passed'])
    await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress'))
    await session.commit()
    await delivery.record(store, delivery.command('test', idempotency_key='first', scenario_id=scenarios[0]['id'],
        obligation_refs=refs, implementation_ids=[impl['id']]))
    await session.commit()
    _, _, metrics = await read_metrics(store, session)
    assert metrics.verification.percent == 50
    spec = await session.get(Spec, delivery.SPEC_ID, populate_existing=True)
    original = deepcopy(spec.test_scenarios)
    await delivery.record(store, delivery.command('test', idempotency_key='second', scenario_id=scenarios[1]['id'],
        obligation_refs=refs, implementation_ids=[impl['id']]))
    await session.commit()
    _, result, metrics = await read_metrics(store, session)
    assert metrics.verification.percent == 100
    assert all(not row.verification.lifecycle_satisfied for row in result.scenarios)
    assert (await session.get(Spec, delivery.SPEC_ID, populate_existing=True)).test_scenarios == original
    assert (await session.get(Card, 'test')).status == 'in_progress'


@pytest.mark.parametrize('mismatch', ['source', 'revision'])
async def test_authenticated_old_base_is_stale_not_current_failure(ledger, tmp_path, mismatch):
    session, store, _, _, _ = await prepare_reports(ledger, tmp_path, 'inspection', ['failed'], mismatch=mismatch)
    _, result, metrics = await read_metrics(store, session)
    assert metrics.verification.percent == 0
    assert result.scenarios[0].verification.state == 'stale'
    assert {r.code for r in result.scenarios[0].verification.reasons} == {'verification_base_mismatch'}


async def test_second_real_association_of_same_execution_preserves_existing_test_credit(ledger, tmp_path):
    session, store, refs, impl, scenarios = await prepare_reports(ledger, tmp_path, 'inspection', ['passed'])
    await delivery.record(store, delivery.command('test', scenario_id=scenarios[0]['id'],
        obligation_refs=refs, implementation_ids=[impl['id']]))
    alias = await delivery.record(store, delivery.command(idempotency_key='same-execution-new-association', obligation_refs=refs))
    await session.commit()
    assert alias['id'] != impl['id']
    snapshot, _, metrics = await read_metrics(store, session)
    assert metrics.verification.percent == 100
    assert aggregate_delivery_metrics(assess_obligation_delivery(snapshot)).verification.percent == 100
    projection = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert projection['allowed']
    assert all(set(row['implementation_ids']) == {alias['id'], impl['id']} for row in projection['rows'])


async def test_cancelled_only_contributor_remains_pending_without_shrinking_spec_denominator(ledger, tmp_path):
    session, store, refs, impl, scenarios = await prepare_reports(ledger, tmp_path, 'inspection', ['passed'])
    await delivery.record(store, delivery.command('test', scenario_id=scenarios[0]['id'],
        obligation_refs=refs, implementation_ids=[impl['id']]))
    await session.commit()
    snapshot, _, _ = await read_metrics(store, session)
    previous = aggregate_delivery_metrics(assess_obligation_delivery(snapshot))
    assert previous.implementation.percent == previous.verification.percent == 100
    await session.execute(update(Card).where(Card.id == 'task').values(status='cancelled'))
    await session.commit()
    snapshot, _, _ = await read_metrics(store, session)
    assessment = assess_obligation_delivery(snapshot)
    current = aggregate_delivery_metrics(assessment)
    assert current.implementation.total == previous.implementation.total == 2
    assert current.implementation.percent == current.verification.percent == 0
    assert not assessment.completeness.scope_resolved
    assert all(any(reason.code == 'scope_allocation_unresolved' for reason in row.implementation.reasons)
        for row in assessment.obligations)
