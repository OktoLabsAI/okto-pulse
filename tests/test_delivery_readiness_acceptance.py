"""Named plan acceptance over canonical inventories and native proof readers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
import test_multicard_delivery_integration as multi
from test_delivery_readiness_batch import Clock, read, reader, statements
from test_native_delivery_currentness import advance_source
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, Spec, CardDeliveryEvidenceRecordRow as Record, CodeInvestigationReceiptRow as Receipt,
)
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
from okto_pulse.core.services.delivery_readiness_diagnostics import compose_delivery_diagnostics

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


async def test_M15_inherited_business_rule_preserves_path_and_reuses_actual_proof(ledger, tmp_path):
    session, store = await multi.setup(ledger)
    ui = await delivery.record(store, multi.implementation())
    auth = await delivery.record(store, multi.implementation('authorization'))
    await multi.bind_test(session, store, tmp_path, 'ui', [ui['id']])
    await session.commit()
    card_ids = set(await session.scalars(select(Card.id)))
    batch = await read(reader(session))
    assessment = assess_obligation_delivery(batch.facts)
    rule = next(row for row in assessment.obligations if row.binding.obligation_ref == 'br:br')
    assert rule.implementation.proof_satisfied and not rule.verification.proof_satisfied
    diagnostics = compose_delivery_diagnostics(assessment, batch.facts)
    rule_causes = {cause.id for cause in diagnostics.graph.causes
        if any(unit.ref == 'br:br' for unit in cause.units)}
    derived = [fact for detail in diagnostics.details if detail.issue_id in rule_causes
        for fact in detail.expected if getattr(fact, 'source_obligation_ref', None) == 'fr:fr']
    assert derived, 'Readiness must expose the actual FR -> BR responsibility path'
    contribution = next(row for row in batch.facts.effective_context.inventory.rows
        if row.binding.obligation_ref == 'br:br').contributions[0]
    assert contribution.origin == 'inherited'
    assert all(fact.obligation_ref == 'br:br' and fact.card_id == 'authorization'
        and fact.scope_sha256 == contribution.scope_sha256
        and fact.source_scope_sha256 == contribution.sources[0].scope_sha256 for fact in derived)
    await multi.bind_test(session, store, tmp_path, 'auth', [auth['id']])
    await session.commit()
    completed = assess_obligation_delivery((await read(reader(session))).facts)
    metrics = aggregate_delivery_metrics(completed)
    assert metrics.implementation.percent == metrics.verification.percent == 100
    assert next(row for row in completed.obligations if row.binding.obligation_ref == 'br:br').verification.proof_satisfied
    assert set(await session.scalars(select(Card.id))) == card_ids
    assert len(list(await session.scalars(select(Record).where(Record.kind == 'test')))) == 2


@pytest.mark.parametrize('change', ['independent_source', 'expired_investigation'], ids=['V22', 'V24'])
async def test_V22_V24_current_delivery_is_not_invalidated_by_unrelated_head_or_investigation_expiry(ledger, change):
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(Record))}
    before = aggregate_delivery_metrics(assess_obligation_delivery((await read(reader(session))).facts))
    clock = Clock()
    if change == 'independent_source':
        await advance_source(session, independent_target=True)
    else:
        expiration = max(await session.scalars(select(Receipt.expires_at)))
        clock.value = expiration + timedelta(days=1)
        assert clock.now() > expiration
    await session.rollback()
    with statements(session.bind) as sql:
        batch = await read(reader(session, clock=clock))
    assessed = assess_obligation_delivery(batch.facts)
    assert aggregate_delivery_metrics(assessed) == before
    assert before.implementation.percent == before.verification.percent == 100
    assert all(statement.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for statement in sql)
    assert {row.id: row.payload for row in await session.scalars(select(Record))} == history


async def test_V18_one_authenticated_result_verifies_two_compatible_implementations(ledger, tmp_path):
    from verification_report_fixtures import report, bind_report_sources
    from okto_pulse.community.adapters.test_evidence import (
        CommunityEvidenceLedger, CommunityTestVerificationReportIssuer,
        CommunityTestEvidenceWriteVerifier,
    )
    from okto_pulse.core.ports.test_evidence import (
        TestVerificationReportRequest as ReportRequest, register_test_evidence_write_verifier,
    )
    from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

    session, store = await multi.setup(ledger)
    spec = await session.get(Spec, delivery.SPEC_ID)
    scenario = {**spec.test_scenarios[0], 'verification_method': 'inspection',
                'linked_criteria': ['ac-ui', 'ac-auth']}
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(test_scenarios=[scenario]))
    await session.execute(update(Card).where(Card.id == 'test').values(test_scenario_ids=[scenario['id']]))
    await session.commit()
    card_ids = set(await session.scalars(select(Card.id)))
    ui = await delivery.record(store, multi.implementation())
    auth = await delivery.record(store, multi.implementation('authorization'))
    value = report()
    await bind_report_sources(session, value, ('execution', 'execution-auth'))
    value['observed_at'] = datetime.now(timezone.utc).isoformat()
    value['result'] = 'passed'
    value['observations'] = [
        {**value['observations'][0], 'observation_id': f'observation-{criterion}',
         'criterion_id': criterion, 'outcome': 'passed'}
        for criterion in ('ac-ui', 'ac-auth')
    ]
    native = CommunityEvidenceLedger(evidence_root=tmp_path / 'reused-report')
    digest = compute_test_scenario_semantic_sha256(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, scenario=scenario, acceptance_criteria=spec.acceptance_criteria)
    issued = await CommunityTestVerificationReportIssuer(ledger=native).admit(ReportRequest(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, scenario_id=scenario['id'],
        scenario_sha256=digest, actor_id='tester', report=value))
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=native))
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        test_scenarios=[{**scenario, 'status': 'passed', 'evidence': dict(issued.evidence)}]))
    await session.commit()
    proof = await delivery.record(store, delivery.command('test', scenario_id=scenario['id'],
        idempotency_key='one-result-two-implementations', implementation_ids=[ui['id'], auth['id']],
        obligation_refs=list(dict.fromkeys(multi.UI_REFS + multi.AUTH_REFS))))
    await session.commit()

    batch = await read(reader(session))
    for card_id in (None, 'task', 'authorization'):
        assessment = assess_obligation_delivery(batch.facts,
            **({'card_id': card_id, 'card_type': 'normal'} if card_id else {}))
        metrics = aggregate_delivery_metrics(assessment)
        assert metrics.implementation.percent == metrics.verification.percent == 100
    tests = list(await session.scalars(select(Record).where(Record.kind == 'test')))
    assert len(tests) == 1 and tests[0].id == proof['id']
    assert set(tests[0].payload['implementation_ids']) == {ui['id'], auth['id']}
    assert len(list(native.receipt_root.glob('*.json'))) == 1
    assert set(await session.scalars(select(Card.id))) == card_ids


async def test_V19_semantic_scenario_change_removes_credit_and_preserves_signed_history(ledger):
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    before = aggregate_delivery_metrics(assess_obligation_delivery((await read(reader(session))).facts))
    assert before.verification.percent == 100
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(Record))}
    spec = await session.get(Spec, delivery.SPEC_ID)
    scenarios = deepcopy(spec.test_scenarios)
    evidence = deepcopy(scenarios[0]['evidence'])
    scenarios[0]['then'] = 'A different business outcome must be observed'
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(test_scenarios=scenarios))
    await session.commit()

    batch = await read(reader(session))
    assessment = assess_obligation_delivery(batch.facts)
    metrics = aggregate_delivery_metrics(assessment)
    assert metrics.implementation.percent == 100 and metrics.verification.percent == 0
    reasons = {code for run in batch.facts.effective_context.native_verification.runs
               for code in run.native_reason_codes}
    assert reasons == {'evidence_v2.scenario_semantic_binding_mismatch'}
    diagnostics = compose_delivery_diagnostics(assessment, batch.facts)
    assert diagnostics.graph.causes
    assert any('evidence_v2.scenario_semantic_binding_mismatch' in cause.native_reason_codes
               for cause in diagnostics.graph.causes)
    assert {row.id: row.payload for row in await session.scalars(select(Record))} == history
    await session.refresh(spec)
    assert spec.test_scenarios[0]['evidence'] == evidence
