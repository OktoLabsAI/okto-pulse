"""Real signed external reports share the scoped scenario and delivery trust path."""
from copy import deepcopy
import json
from types import SimpleNamespace

from verification_report_fixtures import bind_report_sources, report

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.community.adapters.sqlalchemy_verification_currentness import (
    CommunityVerificationCurrentnessStore,
    CommunityVerificationReservationPort,
)
from okto_pulse.community.adapters.test_evidence import CommunityEvidenceLedger, CommunityTestVerificationReportIssuer, CommunityTestEvidenceWriteVerifier
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.mcp import server
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import SpecCreate, SpecUpdate
from okto_pulse.core.services.main import SpecService
from okto_pulse.core.ports.permission_policy import set_permission_flag
from okto_pulse.core.ports.test_evidence import register_test_verification_report_issuer, register_test_evidence_write_verifier, reset_test_verification_report_issuer_for_tests, reset_test_evidence_write_verifier_for_tests
from okto_pulse.core.ports.verification_currentness import (
    register_verification_currentness_store_factory,
    register_verification_reservation_port,
    reset_verification_currentness_store_factory_for_tests,
    reset_verification_reservation_port_for_tests,
)
from okto_pulse.core.services.test_scenario_lifecycle import scenario_has_authenticated_required_evidence

import test_scenario_verification_method as methods
import test_delivery_evidence_integration as delivery

classified_context = methods.classified_context
CRITERIA = [{'id': 'ac-1', 'text': 'Public ports only'}]


@pytest_asyncio.fixture(params=['native_schema'])
async def adopted_context(tmp_path, request):
    # Generation/state creation must run through the actual format-407 hooks.
    async for db in methods.adopted_context.__wrapped__(tmp_path, request):
        yield db


@pytest_asyncio.fixture(params=['native_schema'])
async def ledger(tmp_path, request):
    async for value in delivery.ledger.__wrapped__(tmp_path, request):
        yield value


def compose_verification(db):
    factory = build_community_session_factory(db.bind)
    register_verification_reservation_port(CommunityVerificationReservationPort(factory))
    register_verification_currentness_store_factory(CommunityVerificationCurrentnessStore)
    return factory




@pytest.fixture(autouse=True)
def reset():
    reset_test_verification_report_issuer_for_tests()
    reset_verification_currentness_store_factory_for_tests()
    reset_verification_reservation_port_for_tests()
    yield
    reset_test_verification_report_issuer_for_tests()
    reset_test_evidence_write_verifier_for_tests()
    reset_verification_currentness_store_factory_for_tests()
    reset_verification_reservation_port_for_tests()


async def setup(db, monkeypatch, tmp_path, method):
    app, scenario = await methods.setup(db, monkeypatch, method=method)
    factory = compose_verification(db)
    app.state.runtime_composition = SimpleNamespace(uow_factory=CommunityUnitOfWorkFactory(factory))
    scenario['linked_criteria'] = ['ac-1']
    await db.execute(update(Spec).where(Spec.id == 'spec').values(test_scenarios=[scenario], acceptance_criteria=CRITERIA))
    # Criteria and links were changed after the base fixture sealed its heads.
    # Publish this fixture's final native semantic state through the real adapter.
    from datetime import datetime, timezone
    from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import CommunitySqlAlchemySemanticGuidelineAssessment
    from okto_pulse.core.domain.guideline_policy import PolicyEntityType
    from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
    for kind, identity in [(PolicyEntityType.SPEC, 'spec'), (PolicyEntityType.TEST_SCENARIO, 'ts')]:
        await CommunitySqlAlchemySemanticGuidelineAssessment(db).record_semantic_subject_mutation(
            board_id='board', entity_type=kind, subject_id=identity, actor_id='author',
            idempotency_key='native-report-fixture-' + identity,
            request_digest=canonical_sha256({'scenario': scenario, 'criteria': CRITERIA}),
            changed_at=datetime.now(timezone.utc))
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
@pytest.mark.parametrize('missing', ['sources', 'rules', 'configuration'])
async def test_incomplete_static_report_cannot_mint_receipt_or_change_history(classified_context, monkeypatch, tmp_path, missing):
    db = classified_context
    app, _, ledger = await setup(db, monkeypatch, tmp_path, 'static_analysis')
    before = await methods.writes.snapshot(db)
    value = report('static_analysis')
    del value[missing]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/specs/spec/scenarios/ts/evidence/reports', json={'report': value})
        assert response.status_code == 422, response.text
    assert await methods.writes.snapshot(db) == before
    assert not ledger.receipt_root.exists() or not list(ledger.receipt_root.iterdir())


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
    with pytest.raises(ValueError, match=r'evidence_unverified: verification_report\.receipt_report_binding_mismatch, evidence_v2\.receipt_evidence_tampered'):
        await service.update_spec('spec', 'author', SpecUpdate(test_scenarios=[{**scenario, 'evidence': forged}]))
    with pytest.raises(ValueError, match='test_scenario_status_requires_scoped_update'):
        await service.create_spec('board', 'author', SpecCreate(title='Cannot copy signed identity',
            delivery_context='brownfield', test_scenarios=[{**scenario, 'evidence': evidence}]))
    observed = await db.get(Spec, 'spec', populate_existing=True)
    assert observed.test_scenarios == [scenario]


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['inspection', 'static_analysis', 'demonstration'])
async def test_report_reuses_test_card_delivery_chain_and_revokes_stale_credit(ledger, tmp_path, method):
    from datetime import datetime, timezone
    from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
    from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import CommunitySqlAlchemySemanticGuidelineAssessment
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
    from okto_pulse.core.application.use_cases.base import ActorContext
    from okto_pulse.core.application.use_cases.spec_crud import (
        AdmitTestVerificationReportCommand, AdmitTestVerificationReportUseCase,
        SetTestScenarioStatusCommand, SetTestScenarioStatusUseCase,
    )
    from okto_pulse.core.domain.permissions import PermissionSet
    from okto_pulse.core.domain.guideline_policy import PolicyEntityType
    from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
    from okto_pulse.core.domain.verification_report import parse_verification_report, verification_report_scenario_status
    from okto_pulse.core.ports.relational_application import register_relational_application_adapter

    session, store, _ = ledger
    factory = compose_verification(session)
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    scenario = {**delivery.SCENARIO, 'verification_method': method, 'status': 'ready'}
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(test_scenarios=[scenario]))
    # The shared fixture began with an automated-test scenario. Seal the actual
    # replacement semantics through the native policy owner before publication.
    for kind, identity in [(PolicyEntityType.SPEC, delivery.SPEC_ID),
                           (PolicyEntityType.TEST_SCENARIO, scenario['id'])]:
        await CommunitySqlAlchemySemanticGuidelineAssessment(session).record_semantic_subject_mutation(
            board_id=delivery.BOARD_ID, entity_type=kind, subject_id=identity, actor_id='agent-1',
            idempotency_key='report-chain-method-' + identity,
            request_digest=canonical_sha256({'scenario': scenario}), changed_at=datetime.now(timezone.utc))
    await session.commit()
    value = report(method)
    value['observations'][0]['criterion_id'] = 'ac-about'
    value['observed_at'] = datetime.now(timezone.utc).isoformat()
    await bind_report_sources(session, value)
    await session.commit()
    mixed_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence')
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=mixed_ledger))
    register_test_verification_report_issuer(CommunityTestVerificationReportIssuer(ledger=mixed_ledger))
    actor = ActorContext('agent-1', 'mcp', actor_kind='agent', board_id=delivery.BOARD_ID,
        realm_id='local', permissions=PermissionSet({
            'spec': {'tests': {'execute': True, 'update_status': True}, 'interact_in': {'in_progress': True}},
            'test_scenario': {'interact_in': {'ready': True, 'passed': True, 'failed': True}},
        }))

    async def admit_and_publish(observation):
        # Both allocation and publication use the production use cases and
        # isolated native transactions; a signed report alone is not accepted.
        async with CommunityUnitOfWork(factory(), actor=actor) as uow:
            issued = await AdmitTestVerificationReportUseCase().execute(
                AdmitTestVerificationReportCommand(delivery.SPEC_ID, scenario['id'], observation),
                actor=actor, uow=uow)
        status = verification_report_scenario_status(parse_verification_report(observation))
        async with CommunityUnitOfWork(factory(), actor=actor) as uow:
            accepted = await SetTestScenarioStatusUseCase().execute(
                SetTestScenarioStatusCommand(delivery.SPEC_ID, scenario['id'], status, issued.evidence),
                actor=actor, uow=uow)
        assert accepted.result['verification_current'] and accepted.result['accepted_result_id']
        session.expire_all()
        return accepted.result

    # Admission also validates retained automated receipts before adding reports.
    await admit_and_publish(value)
    implementation = await delivery.record(store, delivery.command())
    result = await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert projected['allowed'] and projected['rows'][0]['test_ids'] == (result['id'],)
    assert (await session.get(Spec, delivery.SPEC_ID)).status.value == 'in_progress'
    # A newly signed old observation cannot qualify a newer implementation.
    stale_value = {**value, 'observed_at': '2026-07-13T14:00:00Z'}
    await session.rollback()
    await admit_and_publish(stale_value)
    with pytest.raises(ValueError, match='current_verified_test'):
        await delivery.record(store, delivery.command('test', idempotency_key='stale-report', implementation_ids=[implementation['id']]))
    await session.rollback()
    assert not (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))['allowed']
    for outcome in ('inconclusive', 'aborted', 'unavailable'):
        pending_value = deepcopy(value)
        pending_value['result'] = pending_value['observations'][0]['outcome'] = outcome
        await session.rollback()
        await admit_and_publish(pending_value)
        assert not (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))['allowed']
        with pytest.raises(ValueError, match='current_verified_test'):
            await delivery.record(store, delivery.command('test', idempotency_key=f'pending-{outcome}',
                implementation_ids=[implementation['id']]))
        await session.rollback()
    # An external report never closes a Spec, and a failed fresh report removes coverage.
    value['result'] = 'failed'
    value['observations'][0]['outcome'] = 'failed'
    await session.rollback()
    await admit_and_publish(value)
    assert not (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))['allowed']
