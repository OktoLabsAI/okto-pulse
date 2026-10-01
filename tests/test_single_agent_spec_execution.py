"""BASE T01 composition: one authenticated agent through actual Spec admission."""
import copy
import json

import pytest
from fastmcp import Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.mcp_auth import make_community_mcp_authenticator
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent, AgentBoard, Board, Card, PermissionPreset, QualityAssessmentReceiptRow, Spec,
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


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments, raise_on_error=False)
    assert not result.is_error, result.content
    body = json.loads(result.content[0].text)
    assert not body.get('error') and body.get('outcome') != 'error', body
    return body['data'] if 'outcome' in body else body


@pytest.mark.asyncio
async def test_one_agent_records_lint_validates_evaluates_and_starts_spec(adopted_context, tmp_path, monkeypatch):
    db = adopted_context
    await complete_start_fixture(db, tmp_path)
    register_report_adapters()
    register_structured_spec_store(CommunitySqlAlchemyStructuredSpecStore())
    factory = async_sessionmaker(db.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={'realm_scope': RealmScope.local()})
    board = await db.get(Board, 'board')
    board.settings = {**(board.settings or {}), 'reviewer_separation_mode': 'off',
                      'require_spec_validation': True, 'require_task_validation': True}
    spec = await db.get(Spec, 'spec', populate_existing=True)
    spec.evaluations = []
    spec.created_by = 'solo'
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
    scope = {'board_id': 'board', 'spec_id': 'spec'}
    async with Client(host) as client:
        resolved = await server._get_agent_ctx('board')
        assert resolved.agent_id == 'solo'
        assert not resolved.permissions.owner_review_required
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
    async with factory() as reader:
        current = await reader.get(Spec, 'spec')
        assert current.status == 'in_progress'
        assert (current.validations, current.evaluations, current.current_validation_id) == preserved
        assert current.validations[-1]['reviewer_id'] == 'solo'
        assert current.evaluations[-1]['evaluator_id'] == 'solo'
        assert (await reader.get(Card, 'task')).status == 'not_started'
        assert list(await reader.scalars(select(Agent.id))) == ['solo']
        receipts = list(await reader.scalars(select(QualityAssessmentReceiptRow)))
        # Canonical five-metric validation is append-only Spec JSON; external
        # Requirement Lint has its own quality receipt table.
        assert {receipt.assessment_kind for receipt in receipts} == {'requirement_lint'}
        assert current.validations[-1]['receipt_id'] == current.current_validation_id
        assert all(receipt.created_by == 'solo' and receipt.subject_edition == current.edition for receipt in receipts)
        assert any(receipt.id == lint['result_id'] for receipt in receipts)
