"""Human handoff reuses qualified agent proof and the real lifecycle writer."""
from unittest.mock import AsyncMock
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_delivery_inline_execution import composed as composed, db as db, command as inline_command
from test_delivery_report import setup
from test_delivery_execution_sets import native_verifier as native_verifier
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec, CardDeliveryEvidenceRecordRow as Record
from test_evidence_v2_adapter import SCENARIO
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.human_task_submission import SubmitHumanTaskUseCase
from okto_pulse.core.models.human_task_submission import HumanTaskSubmissionCommand
from okto_pulse.core.models.schemas import CardMove

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures('native_verifier')]


@pytest.mark.parametrize('requested_status', ['validation', 'done'])
async def test_human_report_reuses_existing_assessment_without_new_technical_claim(composed, db, requested_status, tmp_path):
    seed, _, origin, agent = composed
    await seed.execute(update(Spec).where(Spec.id == 's').values(require_task_validation=False, test_scenarios=[{
        **SCENARIO, 'id': 'scenario', 'linked_criteria': ['ac'], 'linked_task_ids': ['test'], 'status': 'draft',
    }]))
    seed.add(Card(id='test', board_id='b', spec_id='s', card_type='test', status='not_started',
        title='Verify parser', position=1, created_by='owner', test_scenario_ids=['scenario']))
    from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import CommunitySqlAlchemySemanticGuidelineAssessment
    from okto_pulse.core.domain.guideline_policy import PolicyEntityType
    from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
    await seed.flush()
    await CommunitySqlAlchemySemanticGuidelineAssessment(seed).record_semantic_subject_mutation(
        board_id='b', entity_type=PolicyEntityType.CARD, subject_id='c', actor_id='owner',
        idempotency_key='human-report-fixture', request_digest=canonical_sha256({'fixture': 'human-report'}),
        changed_at=datetime.now(timezone.utc))
    await seed.commit()
    proof = inline_command().model_dump()
    proof['entries'][0]['bindings'] = [dict(obligation_ref=ref, contribution='complete') for ref in ('fr:fr', 'ac:ac')]
    from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceBatchCommand
    technical = await origin.execute(CardDeliveryEvidenceBatchCommand.model_validate(proof), actor=agent, uow=composed[1])
    proof_id = technical['entries'][0]['id']
    session, uow, _ = await setup(db)
    try:
        if requested_status == 'done':
            # Compose the native cognitive provider even for this isolated
            # delivery fixture. No live Board setting is changed.
            from okto_pulse.community.adapters.composition import configure_community_kg_registry
            from okto_pulse.community.config import CommunitySettings
            from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
            from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
            from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
            from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port
            register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
            register_knowledge_propagation_port(CommunitySqlAlchemyKnowledgePropagationStore(async_sessionmaker(session.bind)))
            configure_community_kg_registry(async_sessionmaker(session.bind), settings=CommunitySettings(
                data_dir=str(tmp_path / 'runtime'), kg_base_dir=str(tmp_path / 'kg'),
                kg_embedding_mode='stub', kg_embedding_dim=8))
            board = await session.get(Board, 'b')
            board.settings = {**board.settings, 'skip_cognitive_consolidation': True}
            resources = CommunitySqlAlchemyResourceGateAdapter(session)
            for resource in ('architecture', 'mockup'):
                await resources.save_not_applicable('b', 'card', 'c', resource, 'owner',
                    justification='Isolated delivery-command integration fixture', source_channel='test')
            await session.commit()
        # A previously accepted technical report is the authoritative assessment.
        await uow.services.cards.move_card('c', 'agent-1', CardMove(status='validation',
            conclusion='Verified parser implementation', completeness=100,
            completeness_justification='Both assigned criteria implemented', drift=0, drift_justification='Within scope',
            delivery_selection=dict(expected_card_version=1, expected_spec_edition=1,
                expected_delivery_revision=1, record_ids=[proof_id])))
        await session.commit()
        await uow.services.cards.move_card('c', 'owner', CardMove(status='on_hold'))
        await session.commit()
        await uow.services.cards.move_card('c', 'owner', CardMove(status='in_progress'))
        await session.commit()
        card = await session.get(Card, 'c')
        before = card.policy_version
        from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope
        from okto_pulse.core.domain.effective_delivery_coverage import EvaluatedDeliverySnapshot
        from okto_pulse.core.domain.delivery_assessment import aggregate_delivery_metrics
        snapshot = await uow.services.delivery_evidence.load_card_snapshot(CardDeliveryScope('b', 'c', 's', 1))
        assessed = EvaluatedDeliverySnapshot(snapshot).obligations(card_id='c', card_type='normal')
        assert aggregate_delivery_metrics(assessed).implementation.unresolved == 0, (before, assessed, snapshot.rejected_records)
        uow.begin_write = AsyncMock()
        actor = ActorContext(actor_id='owner', actor_kind='user', source='rest', board_id='b',
            permissions=['card.entity.read', 'spec.entity.read', 'card.conclusion.write', f'card.move.in_progress_to_{requested_status}'])
        command = HumanTaskSubmissionCommand(board_id='b', card_id='c', spec_id='s',
            contract_version='human-task-submission/v1', action='delivery_report', requested_status=requested_status,
            content='Entrega revisada pelo usuário', expected_card_version=before, expected_spec_edition=1,
            idempotency_key='human-real-report-1')
        result = await SubmitHumanTaskUseCase().execute(command, actor=actor, uow=uow)
        assert result['status'] == requested_status
        row = await session.get(Record, result['submission_id'])
        receipt = row.payload['human_submission']['receipt']
        link = row.payload['human_submission']['report_link']
        card = await session.get(Card, 'c')
        assert receipt['authority'] == 'human_declaration' and receipt['before_card_version'] == before
        assert card.conclusions[link['index']]['text'] == 'Entrega revisada pelo usuário'
        assert card.conclusions[-1]['completeness'] == 100
        assert card.conclusions[-1]['delivery_manifest']['records'][0]['id'] == proof_id
        assert (await SubmitHumanTaskUseCase().execute(command, actor=actor, uow=uow)) == {**result, 'replayed': True}
        assert len(list((await session.scalars(select(Record).where(Record.payload['kind'].as_string() == 'human_submission'))).all())) == 1
    finally:
        await session.close()
