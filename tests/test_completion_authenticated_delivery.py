"""BASE T39/T40: signed product proof and real closeout with projection debt.

SQLite, the cognitive file store, issuer, verifier, delivery admission and Card
lifecycle are real. Membership is supplied by the shared report fixture; this
is not an authentication-transport test. The product probe uses its ASGI route
without starting background workers or touching an existing installation.
"""
import copy
import json

import httpx
import pytest
from sqlalchemy import func, select, update

import test_adopted_delivery_report as adopted
from okto_pulse.community.app import create_app
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters.sqlalchemy_database import configure_community_database
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Card, Spec, CanonicalDebt, CardDeliveryEvidenceRecordRow as Record,
    DomainEventRow,
)
from okto_pulse.community.adapters.test_evidence import (
    CommunityHttpManifestExecutor, run_manifest_and_build_evidence_v2,
)
from okto_pulse.core.application.use_cases.delivery_evidence import RecordCardDeliveryEvidenceUseCase
from okto_pulse.core.kg.cognitive_readiness import CognitiveReadinessService
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
from okto_pulse.core.kg.rebuild_audit import (
    CognitiveConsolidationItem, CognitiveConsolidationItemStore,
    require_rebuild_audit_artifact_store,
)
from okto_pulse.core.models.delivery_evidence import card_delivery_command

ledger = adopted.ledger
signed = adopted.contract.signed
BOARD, SPEC = adopted.BOARD, adopted.SPEC


@pytest.mark.asyncio
@pytest.mark.parametrize('substantive', [False, True])
async def test_signed_test_closeout_separates_product_proof_from_projection(
    ledger, tmp_path, monkeypatch, substantive,
):
    settings = CommunitySettings(data_dir=str(tmp_path / 'app'),
        kg_base_dir=str(tmp_path / 'kg'), kg_embedding_mode='stub',
        cognitive_readiness_blocking_enabled=True)
    runtime = configure_community_database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    app = create_app(settings, auth_provider=object(), storage_provider=object())
    # Keep the semantic digest tied to what the actual product probe observes.
    monkeypatch.setattr(signed, 'SCENARIO', {**signed.SCENARIO,
        'then': f'the installed version is {settings.app_version}'})
    monkeypatch.setattr(signed, 'ACCEPTANCE_CRITERIA', [{**signed.ACCEPTANCE_CRITERIA[0],
        'text': f'Health reports {settings.app_version}'}])
    issued = []

    async def produce(path):
        evidence_ledger = signed._ledger(path)
        manifest = signed._manifest(expected=settings.app_version)
        (evidence_ledger.manifest_root / 'product.json').write_text(json.dumps(manifest), encoding='utf-8')
        evidence = await run_manifest_and_build_evidence_v2(
            manifest_ref='product.json', board_id=BOARD, spec_id=SPEC,
            scenario_id=signed.SCENARIO_ID, scenario_sha256=signed.SCENARIO_SHA256,
            status='passed', actor_id=signed.ACTOR_ID, environment='pytest-asgi',
            ledger=evidence_ledger, executor=CommunityHttpManifestExecutor(
                base_url='http://127.0.0.1', transport=httpx.ASGITransport(app=app)))
        issued.append(copy.deepcopy(evidence))
        return evidence_ledger, [], evidence

    monkeypatch.setattr(signed, '_produce', produce)
    session = None
    try:
        session, uow, actor = await adopted.setup(ledger, tmp_path, monkeypatch)
        actor.permissions.append('spec.tests.execute')
        resources = CommunitySqlAlchemyResourceGateAdapter(session)
        for resource in ('architecture', 'mockup'):
            await resources.save_not_applicable(BOARD, 'card', 'test', resource, 'owner',
                justification='Version endpoint verification needs no new design', source_channel='test')
        board = await session.get(Board, BOARD)
        board.settings = {**board.settings, 'skip_cognitive_consolidation': False,
                          'cognitive_readiness_policy': 'blocking'}
        await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress'))
        # The source fixture already contains an accepted implementation receipt;
        # this scenario closes its Test Card, not an unfinished implementation.
        await session.execute(update(Card).where(Card.id == 'task').values(status='done'))
        implementation = await adopted.contract.delivery.record(
            uow.services.delivery_evidence, adopted.contract.implementation())
        session.add(CanonicalDebt(id='projection-debt', board_id=BOARD,
            artifact_type='test', artifact_id='test', source_ref='test:test',
            content_hash='d' * 64, target_status='done', canonical_state='pending',
            failure_reason='projection_pending'))
        await session.commit()
        artifacts = require_rebuild_audit_artifact_store()
        if substantive:
            item = CognitiveConsolidationItem(item_id='contradictory-evidence',
                board_id=BOARD, kg_generation_id='generation', source_ref='test:test',
                artifact_type='test', status='failed', recorded_at='2026-09-30T00:00:00Z',
                reason_code='evidence_insufficient')
            artifacts.write_json_atomic(RebuildAuditKey(namespace='cognitive_pending',
                board_id=BOARD, kg_generation_id='generation'), dict(board_id=BOARD,
                kg_generation_id='generation', recorded_at=item.recorded_at,
                items=[item.to_dict()]))
        cognitive = CognitiveConsolidationItemStore(artifact_store=artifacts)
        before = cognitive.read_completion_snapshot(BOARD)
        diagnostics = CognitiveReadinessService(cognitive)
        diagnostic = await diagnostics.evaluate_artifact(session, board_id=BOARD, source_ref='test:test')
        assert diagnostic.tier == 'canonical_debt_open'
        assert len(issued) == 1
        spec = await session.get(Spec, SPEC)
        assert spec.test_scenarios[0]['evidence'] == issued[0]
        command = card_delivery_command(board_id=BOARD, card_id='test', spec_id=SPEC, evidence=dict(
            contract_version='card-delivery-report/v1', expected_card_status='in_progress',
            batch=dict(contract_version='card-delivery-batch/v1', expected_card_version=1,
                expected_spec_edition=1, expected_delivery_revision=0, idempotency_key='close-test',
                entries=[dict(client_ref='proof', kind='test', scenario_id=signed.SCENARIO_ID,
                    obligation_refs=['fr:fr', 'ac:ac-about'], implementation_ids=[implementation['id']],
                    justification='Authenticated observation of the product health route')]),
            report=dict(status='done', conclusion='Installed product version verified', completeness=100,
                completeness_justification='Assigned observable assertions passed', drift=0,
                drift_justification='Within assigned scope')))
        count_before = await session.scalar(select(func.count()).select_from(DomainEventRow))
        if substantive:
            with pytest.raises(ValueError, match='cognitive_consolidation_pending'):
                await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
            await session.commit()  # A caller catching the refusal cannot persist half a report.
            assert (await session.get(Card, 'test')).status == 'in_progress'
            assert await session.scalar(select(func.count()).select_from(Record)) == 1
            assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == count_before
        else:
            result = await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
            assert (await session.get(Card, 'test')).status == 'done'
            projection = await uow.services.delivery_evidence.projection(BOARD, SPEC)
            assert projection['allowed'], json.dumps(projection, default=str)
            replay = await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
            assert replay == {**result, 'replayed': True}
        assert cognitive.read_completion_snapshot(BOARD) == before
        debt = await session.get(CanonicalDebt, 'projection-debt')
        assert debt.canonical_state == 'pending' and debt.failure_reason == 'projection_pending'
        after_diagnostic = await diagnostics.evaluate_artifact(session, board_id=BOARD, source_ref='test:test')
        assert after_diagnostic.tier == 'canonical_debt_open'
        await session.refresh(spec)
        assert spec.test_scenarios[0]['evidence'] == issued[0]
    finally:
        if session is not None:
            await session.close()
        await runtime.engine.dispose()
