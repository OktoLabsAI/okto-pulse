"""Human handoff reuses qualified agent proof and the real lifecycle writer."""
from dataclasses import replace
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_code_traceability_persistence import _attestation_bundle
from test_delivery_inline_execution import command as inline_command
from test_delivery_report import setup
from test_delivery_execution_sets import native_verifier as native_verifier
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec, CardDeliveryEvidenceRecordRow as Record
from test_evidence_v2_adapter import SCENARIO
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.human_task_submission import SubmitHumanTaskUseCase
from okto_pulse.core.models.human_task_submission import HumanTaskSubmissionCommand
from okto_pulse.core.models.schemas import CardMove

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures('native_verifier')]


@pytest_asyncio.fixture
async def db(tmp_path):
    """Exercise human delivery with the installed format's native state writers."""
    from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
    from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
    from okto_pulse.core.domain.execution_contract import new_execution_contract
    from okto_pulse.core.ports.verification_currentness import (
        register_verification_currentness_store_factory,
        reset_verification_currentness_store_factory_for_tests,
    )

    from okto_pulse.community.adapters.current_relational_schema import (
        current_schema_contract,
        initialize_current_schema,
    )
    from okto_pulse.community.adapters.sqlalchemy_database import (
        build_community_engine,
        build_community_session_factory,
        install_community_sqlite_pragmas,
    )
    from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import (
        CommunityDeliveryEvidenceStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_verification_currentness import (
        CommunityVerificationCurrentnessStore,
    )

    engine = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'human-delivery.sqlite'}")
    install_community_sqlite_pragmas(engine)
    register_verification_currentness_store_factory(CommunityVerificationCurrentnessStore)
    try:
        await initialize_current_schema(engine, current_schema_contract())
        async with build_community_session_factory(engine)() as session:
            session.add(Board(id='b', name='Board', owner_id='owner', realm_id='local'))
            await session.flush()
            spec = Spec(id='s', board_id='b', title='Spec', status='in_progress', version=1,
                created_by='owner', architecture_adoption=ArchitectureAdoptionScope(
                    board_id='b', spec_id='s', adopted_in_edition=1, actor_id='owner',
                    inherited_resource_ids=()).model_dump(mode='json'),
                execution_contract=new_execution_contract(board_id='b', spec_id='s', edition=1,
                    actor_id='owner', origin='new_spec'), test_scenarios=[])
            for _, field in COLLECTIONS:
                setattr(spec, field, [])
            spec.functional_requirements = [{
                'id': 'fr', 'text': 'Parse the current input', 'linked_task_ids': ['c'],
                'verification': {'mode': 'explicit', 'required_profiles': ['functional']},
                'implementation_plan': {'contributions': [{
                    'card_id': 'c', 'scope': 'selected_criteria', 'criterion_ids': ['ac'],
                    'summary': 'Parser implementation',
                }]},
            }]
            spec.acceptance_criteria = [{
                'id': 'ac', 'text': 'Valid input is parsed', 'verification_profile': 'functional',
                'linked_task_ids': ['c'],
                'requirement_links': [{'requirement_type': 'functional_requirement', 'requirement_id': 'fr'}],
            }]
            session.add(spec)
            await session.flush()
            session.add(Card(id='c', board_id='b', spec_id='s', title='Card', status='in_progress',
                position=0, created_by='owner', card_type='normal'))
            await session.commit()
            yield engine, session, CommunityDeliveryEvidenceStore(session)
    finally:
        reset_verification_currentness_store_factory_for_tests()
        await engine.dispose()


@pytest_asyncio.fixture
async def composed(db):
    """Admit source proof through the native writer, including its SQL guards."""
    from okto_pulse.core.application.use_cases.code_traceability import (
        SubmitImplementationTargetExecutionUseCase,
    )
    from okto_pulse.core.application.use_cases.delivery_evidence import (
        RecordCardDeliveryEvidenceUseCase,
    )
    from okto_pulse.core.domain.code_traceability import (
        code_investigation_observation_sha256_v2,
    )
    from okto_pulse.core.services.code_investigation import (
        CodeInvestigationService,
        selector_scope_digest_for_card_targets,
    )
    from okto_pulse.core.services.implementation_targets import (
        ImplementationTargetService,
    )

    from okto_pulse.community.adapters.sqlalchemy_code_traceability import (
        CommunitySqlAlchemyCodeInvestigationStore,
        CommunitySqlAlchemyCodeTraceabilityStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
        CommunitySqlAlchemyDomainEventPublisher,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import ImplementationTargetRow

    _, session, store = db
    now = datetime.now(UTC)
    opened, consumed, receipt, head, workspace = _attestation_bundle(now, subject_id='c')
    workspace = replace(workspace, declared_revision='a' * 40)
    digest = selector_scope_digest_for_card_targets(
        board_id='b', card_id='c', card_version=1, targets=(('target', 1),))
    opened = replace(opened, board_id='b', selector_scope_digest=digest)
    consumed = replace(consumed, board_id='b', selector_scope_digest=digest)
    receipt = replace(receipt, board_id='b', selector_scope_digest=digest,
        workspace_state=workspace, declared_revision=workspace.declared_revision,
        observation_sha256=code_investigation_observation_sha256_v2(
            source_ref=receipt.source_ref, selector_scope_digest=digest,
            delivery_context=receipt.delivery_context, outcome=receipt.contextual_outcome,
            capabilities=receipt.capabilities, source_identity_digest=receipt.source_identity_digest,
            declared_revision=workspace.declared_revision, workspace_state=workspace,
            omission_manifest=()))
    investigations = CommunitySqlAlchemyCodeInvestigationStore(session)
    await investigations.create_request(opened)
    await investigations.consume_request_append_receipt_and_advance_head(
        request=consumed, receipt=receipt, head=replace(head, board_id='b'),
        expected_head_revision=None)
    session.add(ImplementationTargetRow(id='target', board_id='b', card_id='c',
        source_ref=receipt.source_ref, selector_kind='file', relative_path_hint='src/file.py',
        role='modify', intent='Deliver', required=True, source_spec_version=1,
        lifecycle_status='active', revision=1, created_by='agent-1', created_at=now, updated_at=now))
    await session.commit()

    async def publish(event):
        await CommunitySqlAlchemyDomainEventPublisher().publish(
            session, event=event, handler_names=('test_observer',))

    async def board(identity):
        return await session.get(Board, identity)

    async def card(identity):
        return await session.get(Card, identity)

    services = SimpleNamespace(delivery_evidence=store, code_investigations=investigations,
        code_traceability=CommunitySqlAlchemyCodeTraceabilityStore(session),
        boards=SimpleNamespace(get_board=board), cards=SimpleNamespace(get_card=card),
        publish_domain_event=publish,
        agents=SimpleNamespace(agent_has_board_access=AsyncMock(return_value=True)))
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
    transaction = CommunityUnitOfWork(session)
    uow = SimpleNamespace(services=services, commit=AsyncMock(side_effect=transaction.commit),
        begin_write=transaction.begin_write, rollback=AsyncMock(side_effect=transaction.rollback))
    origin = SubmitImplementationTargetExecutionUseCase(
        CodeInvestigationService(), ImplementationTargetService())
    actor = ActorContext(actor_id='agent-1', actor_kind='agent', source='mcp', board_id='b',
        permissions=['code_traceability.target.execution_submit', 'card.conclusion.write'])
    yield session, uow, RecordCardDeliveryEvidenceUseCase(origin), actor


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
        assert snapshot.effective_context.native_verification.complete
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
