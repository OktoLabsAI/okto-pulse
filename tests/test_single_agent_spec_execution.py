"""BASE T01: one authenticated agent from Draft through Spec Done."""
import copy
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastmcp import Client
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.mcp_auth import make_community_mcp_authenticator
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent, AgentBoard, Board, Card, CardDeliveryEvidenceRecordRow, PermissionPreset,
    QualityAssessmentReceiptRow, Spec,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_quality_assessment import CommunitySqlAlchemyQualityAssessmentPreflightReader
from okto_pulse.community.adapters.sqlalchemy_structured_spec import CommunitySqlAlchemyStructuredSpecStore
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.mcp import server
from okto_pulse.core.ports import McpCredential
from okto_pulse.core.ports.mcp_resources import freeze_mcp_resource_catalog
from okto_pulse.core.ports.permission_policy import builtin_permission_presets
from okto_pulse.core.ports.quality_assessment import register_quality_assessment_preflight_reader
from okto_pulse.core.ports.structured_spec import register_structured_spec_store
from okto_pulse.core.runtime_registry import register_unit_of_work_factory
from okto_pulse.core.services.main import AgentService

from test_architecture_start_transition import adopted_context as source_context, complete_start_fixture
from test_delivery_reused_impact import register_report_adapters

adopted_context = source_context


async def seed_external_implementation(db):
    """Fixture for an already-admitted external technical execution receipt."""
    from test_code_traceability_persistence import _attestation_bundle
    from okto_pulse.community.adapters.sqlalchemy_code_traceability import _receipt_row, _request_row
    from okto_pulse.community.adapters.sqlalchemy_models import (
        CodeInvestigationRequestRow, CodeInvestigationReceiptRow,
        ImplementationTargetRow, ImplementationTargetExecutionRecordRow,
    )
    from okto_pulse.core.domain.code_traceability import code_investigation_observation_sha256
    now = datetime.now(timezone.utc)
    _, consumed, receipt, _, workspace = _attestation_bundle(now, subject_id='task')
    workspace = replace(workspace, declared_revision='a' * 40)
    request = replace(consumed, board_id='board')
    observation = code_investigation_observation_sha256(source_ref=receipt.source_ref,
        selector_scope_digest=receipt.selector_scope_digest, outcome=receipt.outcome,
        capabilities=receipt.capabilities, source_identity_digest=receipt.source_identity_digest,
        declared_revision=workspace.declared_revision, workspace_state=workspace, omission_manifest=())
    receipt = replace(receipt, board_id='board', declared_revision='a' * 40,
        workspace_state=workspace, observation_sha256=observation)
    await db.execute(insert(CodeInvestigationRequestRow).values(**_request_row(request)))
    await db.execute(insert(CodeInvestigationReceiptRow).values(**_receipt_row(receipt)))
    spec = await db.get(Spec, 'spec')
    db.add(ImplementationTargetRow(id='target', board_id='board', card_id='task',
        source_ref=receipt.source_ref, selector_kind='file', relative_path_hint='src/file.py',
        role='modify', intent='Deliver the procedure view', required=True,
        source_spec_version=spec.version, lifecycle_status='active', revision=1,
        created_by='solo', created_at=now, updated_at=now))
    await db.flush()
    db.add(ImplementationTargetExecutionRecordRow(id='execution', board_id='board', card_id='task',
        target_id='target', target_revision=1, result_investigation_receipt_id=receipt.id,
        source_ref=receipt.source_ref, disposition='touched', result_declared_revision='a' * 40,
        result_workspace_state_id=workspace.workspace_state_id, actual_relative_path='src/file.py',
        justification='External implementation receipt fixture', submitted_by='solo', received_at=now,
        payload_sha256='b' * 64, idempotency_key='execution'))
    await db.commit()


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments, raise_on_error=False)
    assert not result.is_error, result.content
    body = json.loads(result.content[0].text)
    assert not body.get('error') and body.get('outcome') != 'error', body
    return body['data'] if 'outcome' in body else body


@pytest.mark.asyncio
@pytest.mark.parametrize('late_requirement_link', [False, True])
async def test_one_agent_preserves_assessments_and_evidence_through_spec_done(
    adopted_context, tmp_path, monkeypatch, late_requirement_link
):
    db = adopted_context
    await complete_start_fixture(db, tmp_path)
    register_report_adapters()
    register_structured_spec_store(CommunitySqlAlchemyStructuredSpecStore())
    factory = async_sessionmaker(db.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={'realm_scope': RealmScope.local()})
    from okto_pulse.community.adapters.composition import configure_community_kg_registry
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
    configure_community_kg_registry(factory, settings=CommunitySettings(
        data_dir=str(tmp_path / 'runtime'), kg_base_dir=str(tmp_path / 'kg'),
        kg_embedding_mode='stub', kg_embedding_dim=8))
    resources = CommunitySqlAlchemyResourceGateAdapter(db)
    for identity in ('task', 'test'):
        for resource in ('architecture', 'mockup'):
            await resources.save_not_applicable('board', 'card', identity, resource, 'author',
                justification='Procedure fixture has no additional design or mockup', source_channel='test')
    board = await db.get(Board, 'board')
    board.settings = {**(board.settings or {}), 'reviewer_separation_mode': 'off',
                      'require_spec_validation': True, 'require_task_validation': True,
                      'skip_cognitive_consolidation': True, 'impact_evidence_mode': 'off'}
    spec = await db.get(Spec, 'spec', populate_existing=True)
    spec.evaluations = []
    spec.created_by = 'solo'
    if late_requirement_link:
        # Draft input has a criterion linked to FR only. The BR link will be
        # authored through MCP after a real signed run, in the same edition.
        criteria = copy.deepcopy(spec.acceptance_criteria)
        criteria[0]['requirement_links'] = [criteria[0]['requirement_links'][0]]
        spec.acceptance_criteria = criteria
    for identity in ('task', 'test'):
        card = await db.get(Card, identity)
        card.created_by = card.assignee_id = 'solo'
    root = next(row for row in builtin_permission_presets() if row['name'] == 'Full Control')
    db.add(PermissionPreset(id='solo-preset', name=root['name'], flags=root['flags'], is_builtin=True))
    db.add(Agent(id='solo', name='Solo agent', created_by='author', api_key='fixture-solo',
        api_key_hash=AgentService.hash_api_key('fixture-solo'), is_active=True,
        preset_id='solo-preset', permissions=[], permission_flags={}))
    db.add(AgentBoard(id='solo-board', agent_id='solo', board_id='board', granted_by='author'))
    await db.commit()
    await db.close()
    register_unit_of_work_factory(CommunityUnitOfWorkFactory(factory))
    register_quality_assessment_preflight_reader(CommunitySqlAlchemyQualityAssessmentPreflightReader(factory))
    server.register_mcp_authenticator(make_community_mcp_authenticator(session_factory=factory))
    server._permission_cache.clear()
    monkeypatch.setattr(server, 'active_api_key_credential', lambda: McpCredential(
        source='x_api_key_header', value='fixture-solo'))
    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(server.mcp,
        resource_catalog=frozen, projection_identity=frozen.identity)
    # External project fixture: a real GET/assertion replay, signed by the
    # Community issuer and later authenticated by the write verifier.
    import httpx
    from fastapi import FastAPI
    from test_evidence_v2_adapter import _ledger
    from okto_pulse.community.adapters.test_evidence import (
        CommunityHttpManifestExecutor, CommunityTestEvidenceExecutionIssuer,
        CommunityTestEvidenceWriteVerifier,
    )
    from okto_pulse.core.ports.test_evidence import (
        register_test_evidence_execution_issuer, register_test_evidence_write_verifier,
    )
    project = FastAPI()
    steps = ['Open the procedure', 'Perform each required action', 'Record completion']

    @project.get('/procedure')
    async def procedure():
        return {'steps': steps}

    evidence_ledger = _ledger(tmp_path / 'external-evidence')
    register_test_evidence_execution_issuer(CommunityTestEvidenceExecutionIssuer(
        ledger=evidence_ledger, executor=CommunityHttpManifestExecutor(
            base_url='http://127.0.0.1', transport=httpx.ASGITransport(app=project)),
        environment='pytest-asgi'))
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger))
    scope = {'board_id': 'board', 'spec_id': 'spec'}
    async with Client(host) as client:
        resolved = await server._get_agent_ctx('board')
        assert resolved.agent_id == 'solo'
        assert not resolved.permissions.owner_review_required
        if late_requirement_link:
            from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
            async with factory() as reader:
                before = await reader.get(Spec, 'spec')
                before_edition, before_version = before.edition, before.version
                old_digest = compute_test_scenario_semantic_sha256(board_id='board', spec_id='spec',
                    scenario=before.test_scenarios[0], acceptance_criteria=before.acceptance_criteria)
            old_run = await call(client, 'okto_pulse_execute_test_scenario_evidence', **scope,
                scenario_id='scenario', status='passed', replay=json.dumps({
                    'description': 'Observe procedure before BR criterion association',
                    'steps': [{'name': 'procedure', 'path': '/procedure', 'expected_status': 200,
                        'assertions': [{'name': 'all-required-steps', 'kind': 'json_equals',
                            'path': 'steps', 'expected': steps}]}]}))
            old_evidence = copy.deepcopy(old_run['evidence'])
            await call(client, 'okto_pulse_update_spec_entity', **scope,
                entity_type='acceptance_criterion', entity_id='ac-procedure', operation='update',
                expected_spec_version=before_version, payload_json={'requirement_links': [
                    {'requirement_type': 'functional_requirement', 'requirement_id': 'fr'},
                    {'requirement_type': 'business_rule', 'requirement_id': 'br'}]})
            async with factory() as reader:
                linked = await reader.get(Spec, 'spec')
                assert linked.status == 'draft' and linked.edition == before_edition
                assert linked.version > before_version
                assert len(linked.acceptance_criteria[0]['requirement_links']) == 2
                current_digest = compute_test_scenario_semantic_sha256(board_id='board', spec_id='spec',
                    scenario=linked.test_scenarios[0], acceptance_criteria=linked.acceptance_criteria)
                assert current_digest != old_digest
                after_link = copy.deepcopy((linked.test_scenarios, linked.acceptance_criteria, linked.version))
            # The receipt remains authentic for its original scope. A link alone
            # cannot enlarge that scope, even before implementation/time gates.
            assert CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger).verify(
                board_id='board', spec_id='spec', scenario_id='scenario', status='passed',
                scenario_sha256=old_digest, actor_id='solo',
                evidence=old_evidence).verified
            refused = await client.call_tool('okto_pulse_update_test_scenario_status',
                {**scope, 'scenario_id': 'scenario', 'status': 'passed',
                 'evidence': json.dumps(old_evidence)}, raise_on_error=False)
            assert 'evidence_unverified' in str(refused), refused.content
            async with factory() as reader:
                linked = await reader.get(Spec, 'spec')
                assert (linked.test_scenarios, linked.acceptance_criteria, linked.version) == after_link
                assert not list(await reader.scalars(select(CardDeliveryEvidenceRecordRow)))
            assert old_run['evidence'] == old_evidence
        for state in ('review', 'approved'):
            await call(client, 'okto_pulse_move_spec', **scope, status=state)
        preflight = await call(client, 'okto_pulse_get_requirement_lint_preflight', **scope)
        lint = await call(client, 'okto_pulse_record_requirement_lint', **scope,
            **preflight['submission_fence'], ruleset_digest=preflight['ruleset_digest'],
            score=0, findings=[], summary='External evaluator inspected all requirement anchors against the pinned ruleset',
            idempotency_key='solo-lint')
        assert lint['status'] == 'accepted'
        async with factory() as reader:
            current = await reader.get(Spec, 'spec')
            fence = dict(expected_validation_edition=current.edition,
                expected_spec_version=current.version, expected_head_revision=0)
        metrics = {}
        for metric in ('confidence', 'clarity', 'assertiveness', 'decidability', 'ambiguity'):
            metrics[metric] = 0 if metric == 'ambiguity' else 95
            metrics[metric + '_justification'] = 'Inspected observable requirements, linked criteria and explicit task allocations'
        await call(client, 'okto_pulse_submit_spec_validation', **scope, **fence, **metrics, recommendation='approve')
        await call(client, 'okto_pulse_submit_spec_evaluation', **scope,
            breakdown_completeness=95, breakdown_justification='All requirements have explicit task contributions',
            granularity=95, granularity_justification='Implementation and verification are separately allocated',
            dependency_coherence=95, dependency_justification='No unresolved dependency is required for this procedure',
            test_coverage_quality=95, test_coverage_justification='The procedure scenario covers the observable criterion',
            overall_score=95, overall_justification='The complete plan is executable with explicit proof obligations',
            recommendation='approve')
        async with factory() as reader:
            current = await reader.get(Spec, 'spec')
            assert current.status == 'validated'
            preserved = copy.deepcopy((current.validations, current.evaluations, current.current_validation_id))
        await call(client, 'okto_pulse_move_spec', **scope, status='in_progress')
        for state in ('started', 'in_progress'):
            await call(client, 'okto_pulse_move_card', board_id='board', card_id='task', status=state)
        checkpoint_scope = await call(client, 'okto_pulse_get_delivery_evidence', **scope, card_id='task', view='resume')
        checkpoint = await call(client, 'okto_pulse_record_delivery_evidence', **scope, card_id='task', evidence=dict(
            kind='progress', expected_card_version=checkpoint_scope['card_version'],
            expected_spec_edition=checkpoint_scope['edition'], idempotency_key='solo-progress',
            justification='Inspected the assigned procedure scope before reporting implementation',
            progress=dict(contract_version='delivery-progress/v2', material_change='none',
                source_state=dict(workspace_state='unknown', recoverability='unknown'),
                remaining='Record accepted implementation and execute the procedure test')))
        async with factory() as reader:
            progress_row = await reader.get(CardDeliveryEvidenceRecordRow, checkpoint['id'])
            assert progress_row.kind == 'progress' and progress_row.actor_id == 'solo'
            progress_payload = copy.deepcopy(progress_row.payload)
            assert (await reader.get(Card, 'task')).status == 'in_progress'
            assert not list(await reader.scalars(select(CardDeliveryEvidenceRecordRow).where(
                CardDeliveryEvidenceRecordRow.kind.in_(['implementation', 'test']))))
        async with factory() as writer:
            await seed_external_implementation(writer)
        resume = await call(client, 'okto_pulse_get_delivery_evidence', **scope, card_id='task', view='resume')
        report = dict(contract_version='card-delivery-report/v1', expected_card_status='in_progress',
            batch=dict(contract_version='card-delivery-batch/v1', expected_card_version=resume['card_version'],
                expected_spec_edition=resume['edition'], expected_delivery_revision=resume['delivery_revision'],
                idempotency_key='solo-report', entries=[dict(client_ref='implementation', kind='implementation',
                    execution_id='execution', justification='Procedure implementation at the accepted revision',
                    bindings=[dict(obligation_ref=ref, contribution='complete') for ref in ('fr:fr', 'br:br', 'ac:ac-procedure', 'decision:decision')])]),
            report=dict(status='validation', conclusion='Procedure view displays every required step', completeness=100,
                completeness_justification='All allocated implementation is present', drift=0,
                drift_justification='Matches the approved procedure scope'))
        delivered = await call(client, 'okto_pulse_record_delivery_evidence', **scope, card_id='task', evidence=report)
        resume = await call(client, 'okto_pulse_get_delivery_evidence', **scope, card_id='task', view='resume')
        reviewed = await call(client, 'okto_pulse_submit_task_validation', board_id='board', card_id='task',
            expected_subject_version=resume['card_version'], idempotency_key='solo-review',
            confidence=95, confidence_justification='Inspected the accepted implementation record',
            estimated_completeness=100, completeness_justification='All allocated implementation is present',
            estimated_drift=0, drift_justification='No difference from the assigned scope',
            general_justification='Inspected the preserved implementation and report under permitted self-review',
            recommendation='approve')
        assert reviewed['card_status'] == 'done', reviewed
        # T07 in the same continuous flow: accepted implementation and task
        # review still cannot replace the independent test-phase evidence.
        refused = await client.call_tool('okto_pulse_move_spec', {**scope, 'status': 'done'}, raise_on_error=False)
        assert 'delivery_test_result_missing' in str(refused), refused.content
        async with factory() as reader:
            current = await reader.get(Spec, 'spec')
            assert current.status == 'in_progress'
            assert (current.validations, current.evaluations, current.current_validation_id) == preserved
            records = list(await reader.scalars(select(CardDeliveryEvidenceRecordRow)))
            assert {record.id for record in records} == {checkpoint['id'], delivered['entries'][0]['id']}
            implementation_payload = copy.deepcopy(next(
                record.payload for record in records if record.id == delivered['entries'][0]['id']))
        for state in ('started', 'in_progress'):
            await call(client, 'okto_pulse_move_card', board_id='board', card_id='test', status=state)
        executed = await call(client, 'okto_pulse_execute_test_scenario_evidence', **scope,
            scenario_id='scenario', status='passed', replay=json.dumps({'description': 'Verify every procedure step',
                'steps': [{'name': 'procedure', 'path': '/procedure', 'expected_status': 200,
                    'assertions': [{'name': 'all-required-steps', 'kind': 'json_equals', 'path': 'steps', 'expected': steps}]}]}))
        await call(client, 'okto_pulse_update_test_scenario_status', **scope,
            scenario_id='scenario', status='passed', evidence=json.dumps(executed['evidence']))
        resume_test = await call(client, 'okto_pulse_get_delivery_evidence', **scope, card_id='test', view='resume')
        test_report = dict(contract_version='card-delivery-report/v1', expected_card_status='in_progress',
            batch=dict(contract_version='card-delivery-batch/v1', expected_card_version=resume_test['card_version'],
                expected_spec_edition=resume_test['edition'], expected_delivery_revision=resume_test['delivery_revision'],
                idempotency_key='solo-test-report', entries=[dict(client_ref='test-proof', kind='test',
                    scenario_id='scenario', implementation_ids=[delivered['entries'][0]['id']],
                    obligation_refs=['fr:fr', 'br:br', 'ac:ac-procedure', 'decision:decision'],
                    justification='Authenticated GET replay verifies all required procedure steps')]),
            report=dict(status='done', conclusion='Procedure verification passed', completeness=100,
                completeness_justification='All assigned observable assertions passed', drift=0,
                drift_justification='Within the planned verification scope'))
        tested = await call(client, 'okto_pulse_record_delivery_evidence', **scope, card_id='test', evidence=test_report)
        await call(client, 'okto_pulse_move_spec', **scope, status='done')
    async with factory() as reader:
        current = await reader.get(Spec, 'spec')
        assert current.status == 'done'
        assert (current.validations, current.evaluations, current.current_validation_id) == preserved
        assert current.validations[-1]['reviewer_id'] == 'solo'
        assert current.evaluations[-1]['evaluator_id'] == 'solo'
        task = await reader.get(Card, 'task')
        assert task.status == 'done', json.dumps([reviewed, task.validations, task.rejection_records], default=str)
        assert task.validations[-1]['reviewer_id'] == 'solo'
        assert task.conclusions[0]['author_id'] == 'solo'
        assert task.conclusions[0]['delivery_manifest']['records'][0]['id'] == delivered['entries'][0]['id']
        test = await reader.get(Card, 'test')
        assert test.status == 'done' and not test.validations
        assert test.conclusions[0]['delivery_manifest']['records'][0]['id'] == tested['entries'][0]['id']
        assert current.test_scenarios[0]['evidence'] == executed['evidence']
        records = list(await reader.scalars(select(CardDeliveryEvidenceRecordRow)))
        assert {record.id for record in records} == {checkpoint['id'], delivered['entries'][0]['id'], tested['entries'][0]['id']}
        assert all(record.actor_id == 'solo' for record in records)
        assert (await reader.get(CardDeliveryEvidenceRecordRow, checkpoint['id'])).payload == progress_payload
        assert (await reader.get(CardDeliveryEvidenceRecordRow, delivered['entries'][0]['id'])).payload == implementation_payload
        assert list(await reader.scalars(select(Agent.id))) == ['solo']
        receipts = list(await reader.scalars(select(QualityAssessmentReceiptRow)))
        # Canonical five-metric validation is append-only Spec JSON; external
        # Requirement Lint has its own quality receipt table.
        assert {receipt.assessment_kind for receipt in receipts} == {'requirement_lint'}
        assert current.validations[-1]['receipt_id'] == current.current_validation_id
        assert all(receipt.created_by == 'solo' and receipt.subject_edition == current.edition for receipt in receipts)
        assert any(receipt.id == lint['result_id'] for receipt in receipts)
