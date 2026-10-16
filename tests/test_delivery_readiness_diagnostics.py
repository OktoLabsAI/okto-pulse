"""Diagnostic details from actual native SQLite rows and signed reports."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_verification import prepare_reports
from okto_pulse.community.adapters.sqlalchemy_models import Card, CardDeliveryEvidenceRecordRow
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, assess_test_delivery, aggregate_delivery_metrics
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceInput
from okto_pulse.core.services.delivery_readiness_diagnostics import (
    compose_delivery_diagnostics, render_diagnostic_action, render_diagnostic_issue,
)


ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


@pytest.mark.parametrize('method', ['inspection', 'static_analysis', 'demonstration'])
async def test_native_association_diagnostics_are_read_only_and_use_existing_receipt(ledger, tmp_path, method):
    session, store, refs, implementation, scenarios = await prepare_reports(ledger, tmp_path, method, ['passed'])
    snapshot, _ = await store.load_rollup_snapshot(delivery.BOARD_ID, delivery.SPEC_ID)
    card = await session.get(Card, 'test', populate_existing=True)
    assessment = assess_test_delivery(snapshot, card_id=card.id, scenario_ids=tuple(card.test_scenario_ids))
    before = deepcopy([(r.id, r.payload) for r in await session.scalars(select(CardDeliveryEvidenceRecordRow))])
    metrics = aggregate_delivery_metrics(assessment)
    projection = compose_delivery_diagnostics(assessment, snapshot,
        spec_assessment=assess_obligation_delivery(snapshot),
        permissions={'spec.tests.execute': True}, card_versions={card.id: card.policy_version})
    assert projection.graph.causes and all(c.code == 'verification_association_missing' for c in projection.graph.causes)
    for action in projection.graph.actions:
        dto = render_diagnostic_action(action)
        assert dto.kind == 'associate_verification' and dto.state == 'requires_input'
        command = CardDeliveryEvidenceInput.model_validate(dto.arguments['evidence'] | {
            'justification': 'Associate the existing signed result', 'idempotency_key': 'diagnostic-contract-only'})
        assert command.scenario_id == scenarios[0]['id']
        assert command.implementation_ids == [implementation['id']]
        assert set(command.obligation_refs) <= set(refs)
    details = {d.issue_id: d for d in projection.details}
    native_receipts = {run.receipt_id for run in snapshot.effective_context.native_verification.runs}
    for cause in projection.graph.causes:
        issue = render_diagnostic_issue(cause, details[cause.id], scope=snapshot.scope,
            card_id=card.id, snapshot_id='c' * 64)
        assert issue.affected_unit_count == 1
        assert any(e.kind == 'receipt' and e.id in native_receipts for e in issue.evidence_refs)
        assert all(f.verification_method in {None, method} for f in issue.observed)
    assert metrics == aggregate_delivery_metrics(assessment)
    assert [(r.id, r.payload) for r in await session.scalars(select(CardDeliveryEvidenceRecordRow))] == before
    assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize('mismatch', ['source', 'revision'])
async def test_signed_wrong_base_details_show_actual_expected_and_observed_sources(ledger, tmp_path, mismatch):
    session, store, _, _, _ = await prepare_reports(ledger, tmp_path, 'inspection', ['failed'], mismatch=mismatch)
    snapshot, _ = await store.load_rollup_snapshot(delivery.BOARD_ID, delivery.SPEC_ID)
    card = await session.get(Card, 'test', populate_existing=True)
    assessment = assess_test_delivery(snapshot, card_id=card.id, scenario_ids=tuple(card.test_scenario_ids))
    projection = compose_delivery_diagnostics(assessment, snapshot)
    assert projection.graph.causes and all(c.code == 'verification_base_mismatch' for c in projection.graph.causes)
    for cause in projection.graph.causes:
        assert cause.origin.expected_bases and cause.origin.observed_bases
        assert cause.origin.expected_bases != cause.origin.observed_bases
    assert all(a.kind == 'execute_verification' for a in projection.graph.actions)
    assert aggregate_delivery_metrics(assessment).verification.percent == 0


@pytest.mark.parametrize('method', ['inspection', 'static_analysis', 'demonstration'])
@pytest.mark.parametrize('outcome', [None, 'passed'])
async def test_batch_procedure_becomes_external_guidance_without_executing_or_writing(ledger, tmp_path, method, outcome):
    session, _, _, _, scenarios = await prepare_reports(ledger, tmp_path, method, [outcome], mismatch='revision')
    before = deepcopy([(r.id, r.payload) for r in await session.scalars(select(CardDeliveryEvidenceRecordRow))])
    await session.rollback()
    class Clock:
        def now(self):
            return datetime.now(timezone.utc)
    batch = await CommunityDeliveryReadinessReader(session.bind, clock=Clock()).load(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, card_id='test')
    assessment = assess_test_delivery(batch.facts, card_id='test', scenario_ids=(scenarios[0]['id'],))
    projection = compose_delivery_diagnostics(assessment, batch.facts, procedures=batch.scenarios,
        spec_assessment=assess_obligation_delivery(batch.facts), permissions={'spec.tests.execute': True})
    assert projection.graph.actions
    for action in projection.graph.actions:
        dto = render_diagnostic_action(action)
        assert dto.kind == 'execute_verification' and dto.state == 'ready'
        assert dto.execution_surface == 'external_agent' and dto.pulse_tool is None
        procedure, = dto.arguments['scenarios']
        assert procedure == {key: scenarios[0][key] for key in ('title', 'given', 'when', 'then', 'verification_method')} | {
            'scenario_id': scenarios[0]['id'], 'criterion_ids': scenarios[0]['linked_criteria']}
        bases = dto.arguments['implementation_bases']
        assert bases and all(b['revision'] == 'a' * 40 for b in bases)
        assert all(b['execution_id'] == 'execution' for b in bases)
    assert [(r.id, r.payload) for r in await session.scalars(select(CardDeliveryEvidenceRecordRow))] == before
    assert not session.new and not session.dirty and not session.deleted
