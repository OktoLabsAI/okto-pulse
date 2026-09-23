"""Real signed external reports share the scoped scenario and delivery trust path."""
from copy import deepcopy
import json

import httpx
import pytest
from sqlalchemy import update

from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.community.adapters.test_evidence import CommunityEvidenceLedger, CommunityTestVerificationReportIssuer, CommunityTestEvidenceWriteVerifier
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.mcp import server
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import SpecCreate, SpecUpdate
from okto_pulse.core.services.main import SpecService
from okto_pulse.core.ports.permission_policy import set_permission_flag
from okto_pulse.core.ports.test_evidence import register_test_verification_report_issuer, register_test_evidence_write_verifier, reset_test_verification_report_issuer_for_tests, reset_test_evidence_write_verifier_for_tests
from okto_pulse.core.services.test_scenario_lifecycle import scenario_has_authenticated_required_evidence

import test_scenario_verification_method as methods
import test_delivery_evidence_integration as delivery

adopted_context = methods.adopted_context
classified_context = methods.classified_context
ledger = delivery.ledger
SOURCE = {'reference': 'repo:component', 'revision': 'commit-1', 'sha256': 'a' * 64}
CRITERIA = [{'id': 'ac-1', 'text': 'Public ports only'}]


def report(method='inspection'):
    result = {'schema_version': 'verification-report/v1', 'method': method, 'report_id': 'report-1',
        'observed_at': '2026-09-23T13:00:00Z', 'sources': [SOURCE], 'conclusion': 'Public ports only', 'result': 'passed',
        'observations': [{'observation_id': 'o1', 'criterion_id': 'ac-1', 'observation_ref': 'repo:component#imports',
            'expected': 'Public ports', 'observed': 'Imports use ports', 'outcome': 'passed'}]}
    if method == 'inspection':
        result['inspection_procedure'] = SOURCE
    elif method == 'demonstration':
        result.update(procedure=SOURCE, environment=SOURCE)
    else:
        result.update(tool_name='checker', tool_version='1', rules=[SOURCE], configuration=SOURCE,
                      analyzed_scope=['component'], findings=[])
    return result


@pytest.fixture(autouse=True)
def reset():
    reset_test_verification_report_issuer_for_tests()
    yield
    reset_test_verification_report_issuer_for_tests()
    reset_test_evidence_write_verifier_for_tests()


async def setup(db, monkeypatch, tmp_path, method):
    app, scenario = await methods.setup(db, monkeypatch, method=method)
    scenario['linked_criteria'] = ['ac-1']
    await db.execute(update(Spec).where(Spec.id == 'spec').values(test_scenarios=[scenario], acceptance_criteria=CRITERIA))
    await db.commit()
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence')
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=ledger))
    register_test_verification_report_issuer(CommunityTestVerificationReportIssuer(ledger=ledger))
    return app, scenario, ledger


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['inspection', 'static_analysis', 'demonstration'])
async def test_rest_mcp_admission_and_real_status_write_with_freshness(classified_context, monkeypatch, tmp_path, method):
    db = classified_context
    app, scenario, ledger = await setup(db, monkeypatch, tmp_path, method)
    value = report(method)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/specs/spec/scenarios/ts/evidence/reports', json={'report': value})
        assert response.status_code == 200, response.text
        evidence = response.json()['evidence']
        assert evidence['report_author_id'] == 'author'
        assert 'execution_attestation' not in evidence and 'manifest_ref' not in evidence
        persisted = await db.get(Spec, 'spec', populate_existing=True)
        assert persisted.test_scenarios[0]['status'] == 'ready'
        native = json.loads(await server.okto_pulse_admit_test_verification_report.fn(
            board_id='board', spec_id='spec', scenario_id='ts', report=value))
        assert native.get('error') is None, native
        assert native['scenario_persisted'] is False
        assert native['evidence']['execution_receipt'] != evidence['execution_receipt']
        assert len(list(ledger.receipt_root.glob('*.json'))) == 2
        for field, replacement in [('report_author_id', 'another'), ('scenario_sha256', 'sha256:' + 'b' * 64)]:
            bad = {**evidence, field: replacement}
            denied = await client.patch('/api/v1/specs/spec/scenarios/ts/status', json={'status': 'passed', 'evidence': bad})
            assert denied.status_code == 422, denied.text
        changed = deepcopy(evidence)
        changed['verification_report']['conclusion'] = 'fabricated replacement'
        denied = await client.patch('/api/v1/specs/spec/scenarios/ts/status', json={'status': 'passed', 'evidence': changed})
        assert denied.status_code == 422, denied.text
        passed = await client.patch('/api/v1/specs/spec/scenarios/ts/status', json={'status': 'passed', 'evidence': evidence})
        assert passed.status_code == 200, passed.text
    observed = {**scenario, 'status': 'passed', 'evidence': evidence}
    common = dict(board_id='board', spec_id='spec', acceptance_criteria=CRITERIA)
    assert scenario_has_authenticated_required_evidence(scenario=observed, **common)
    for changed in ({**observed, 'then': 'Changed condition'}, {**observed, 'verification_method': 'automated_test'},
                    {**observed, 'verification_method': None}, {**observed, 'linked_criteria': []}):
        assert not scenario_has_authenticated_required_evidence(scenario=changed, **common)
    assert not scenario_has_authenticated_required_evidence(scenario=observed, board_id='board', spec_id='spec',
        acceptance_criteria=[{**CRITERIA[0], 'text': 'Changed criterion'}])
    # A fresh ledger object validates all existing report receipts before signing.
    restarted = CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence')
    verifier = CommunityTestEvidenceWriteVerifier(ledger=restarted)
    assert verifier.verify(board_id='board', spec_id='spec', status='passed', scenario_id='ts',
        scenario_sha256=evidence['scenario_sha256'], actor_id=None, evidence=evidence).verified


@pytest.mark.asyncio
async def test_report_denials_happen_before_receipt_and_preserve_scope(classified_context, monkeypatch, tmp_path):
    app, _, ledger = await setup(classified_context, monkeypatch, tmp_path, 'inspection')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        path = '/api/v1/specs/spec/scenarios/ts/evidence/reports'
        for bad in ({**report(), 'approved': True}, report('static_analysis'),
                    {**report(), 'observations': [{**report()['observations'][0], 'criterion_id': 'foreign'}]}):
            response = await client.post(path, json={'report': bad})
            assert response.status_code == 422, response.text
        denied_actor = methods.actor()
        set_permission_flag(denied_actor.permissions.flags, 'spec.tests.execute', False)
        monkeypatch.setattr(RESTAdapterContract, 'actor', staticmethod(lambda *args, **kwargs: denied_actor))
        denied = await client.post(path, json={'report': report()})
        assert denied.status_code == 403, denied.text
        monkeypatch.setattr(RESTAdapterContract, 'actor', staticmethod(lambda *args, **kwargs: methods.actor(board_id='other-board')))
        foreign = await client.post(path, json={'report': report()})
        missing = await client.post(path.replace('/spec/', '/missing/'), json={'report': report()})
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
    assert not ledger.receipt_root.exists() or not list(ledger.receipt_root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['inconclusive', 'aborted', 'unavailable'])
async def test_nonconclusive_report_is_authenticated_pending_work_not_test_credit(classified_context, monkeypatch, tmp_path, outcome):
    db = classified_context
    app, _, _ = await setup(db, monkeypatch, tmp_path, 'inspection')
    value = report()
    value['result'] = value['observations'][0]['outcome'] = outcome
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        admitted = await client.post('/api/v1/specs/spec/scenarios/ts/evidence/reports', json={'report': value})
        assert admitted.status_code == 200, admitted.text
        evidence = admitted.json()['evidence']
        unsigned = {key: item for key, item in evidence.items() if key != 'execution_receipt'}
        rejected = await client.patch('/api/v1/specs/spec/scenarios/ts/status', json={'status': 'ready', 'evidence': unsigned})
        assert rejected.status_code == 422, rejected.text
        rejected = await client.patch('/api/v1/specs/spec/scenarios/ts/status', json={'status': 'passed', 'evidence': evidence})
        assert rejected.status_code == 422, rejected.text
        saved = await client.patch('/api/v1/specs/spec/scenarios/ts/status', json={'status': 'ready', 'evidence': evidence})
        assert saved.status_code == 200, saved.text
    persisted = await db.get(Spec, 'spec', populate_existing=True)
    observed = persisted.test_scenarios[0]
    assert observed['status'] == 'ready'
    assert observed['evidence']['verification_report']['result'] == outcome
    # Authenticity is independent of passing credit: consumers also check status.
    assert scenario_has_authenticated_required_evidence(board_id='board', spec_id='spec',
        scenario=observed, acceptance_criteria=CRITERIA)
    cold = CommunityTestEvidenceWriteVerifier(ledger=CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence'))
    assert cold.verify(board_id='board', spec_id='spec', status='ready', scenario_id='ts',
        scenario_sha256=evidence['scenario_sha256'], actor_id='author', evidence=evidence).verified


@pytest.mark.asyncio
async def test_bulk_ready_report_writes_cannot_bypass_authentication(classified_context, monkeypatch, tmp_path):
    db = classified_context
    app, scenario, _ = await setup(db, monkeypatch, tmp_path, 'inspection')
    value = report()
    value['result'] = value['observations'][0]['outcome'] = 'inconclusive'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        admitted = await client.post('/api/v1/specs/spec/scenarios/ts/evidence/reports', json={'report': value})
        assert admitted.status_code == 200, admitted.text
    evidence = admitted.json()['evidence']
    db.info['realm_scope'] = RealmScope.local()
    service = SpecService(db)
    unsigned = {key: item for key, item in evidence.items() if key != 'execution_receipt'}
    with pytest.raises(ValueError, match='verification_evidence_authenticated_result_required'):
        await service.update_spec('spec', 'author', SpecUpdate(test_scenarios=[{**scenario, 'evidence': unsigned}]))
    forged = deepcopy(evidence)
    forged['verification_report']['conclusion'] = 'Unobserved conclusion'
    with pytest.raises(ValueError, match='evidence_unverified: verification_report_invalid'):
        await service.update_spec('spec', 'author', SpecUpdate(test_scenarios=[{**scenario, 'evidence': forged}]))
    with pytest.raises(ValueError, match='test_scenario_status_requires_scoped_update'):
        await service.create_spec('board', 'author', SpecCreate(title='Cannot copy signed identity',
            delivery_context='brownfield', test_scenarios=[{**scenario, 'evidence': evidence}]))
    observed = await db.get(Spec, 'spec', populate_existing=True)
    assert observed.test_scenarios == [scenario]


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['inspection', 'static_analysis', 'demonstration'])
async def test_report_reuses_test_card_delivery_chain_and_revokes_stale_credit(ledger, tmp_path, method):
    from okto_pulse.core.ports.test_evidence import TestVerificationReportRequest as ReportRequest
    from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
    session, store, _ = ledger
    spec = await session.get(Spec, delivery.SPEC_ID)
    scenario = {**delivery.SCENARIO, 'verification_method': method}
    digest = compute_test_scenario_semantic_sha256(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        scenario=scenario, acceptance_criteria=spec.acceptance_criteria)
    value = report(method)
    value['observations'][0]['criterion_id'] = 'ac-about'
    mixed_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence')
    issuer = CommunityTestVerificationReportIssuer(ledger=mixed_ledger)
    # Validates retained automated-test receipts before adding another schema.
    issued = await issuer.admit(ReportRequest(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        scenario_id=scenario['id'], scenario_sha256=digest, actor_id='agent-1', report=value))
    evidence = dict(issued.evidence)
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        test_scenarios=[{**scenario, 'status': 'passed', 'evidence': evidence}]))
    await session.commit()
    implementation = await delivery.record(store, delivery.command())
    result = await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert projected['allowed'] and projected['rows'][0]['test_ids'] == (result['id'],)
    assert (await session.get(Spec, delivery.SPEC_ID)).status.value == 'in_progress'
    # A newly signed old observation cannot qualify a newer implementation.
    stale_value = {**value, 'observed_at': '2026-07-13T14:00:00Z'}
    stale = await issuer.admit(ReportRequest(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        scenario_id=scenario['id'], scenario_sha256=digest, actor_id='agent-1', report=stale_value))
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        test_scenarios=[{**scenario, 'status': 'passed', 'evidence': dict(stale.evidence)}]))
    await session.commit()
    with pytest.raises(ValueError, match='current_verified_test'):
        await delivery.record(store, delivery.command('test', idempotency_key='stale-report', implementation_ids=[implementation['id']]))
    await session.rollback()
    assert not (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))['allowed']
    for outcome in ('inconclusive', 'aborted', 'unavailable'):
        pending_value = deepcopy(value)
        pending_value['result'] = pending_value['observations'][0]['outcome'] = outcome
        pending = await issuer.admit(ReportRequest(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
            scenario_id=scenario['id'], scenario_sha256=digest, actor_id='agent-1', report=pending_value))
        await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
            test_scenarios=[{**scenario, 'status': 'ready', 'evidence': dict(pending.evidence)}]))
        await session.commit()
        assert not (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))['allowed']
        with pytest.raises(ValueError, match='current_verified_test'):
            await delivery.record(store, delivery.command('test', idempotency_key=f'pending-{outcome}',
                implementation_ids=[implementation['id']]))
        await session.rollback()
    # An external report never closes a Spec, and a failed fresh report removes coverage.
    value['result'] = 'failed'
    value['observations'][0]['outcome'] = 'failed'
    failed = await issuer.admit(ReportRequest(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        scenario_id=scenario['id'], scenario_sha256=digest, actor_id='agent-1', report=value))
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        test_scenarios=[{**scenario, 'status': 'failed', 'evidence': dict(failed.evidence)}]))
    await session.commit()
    assert not (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))['allowed']
