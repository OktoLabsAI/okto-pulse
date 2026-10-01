"""BASE T04: real grants, separate MCP sessions and durable review handoff."""
from copy import deepcopy
import json

import pytest
from fastmcp import Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import test_adopted_delivery_report as adopted
from okto_pulse.community.adapters.mcp_auth import make_community_mcp_authenticator
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent, AgentBoard, Board, Card, CardDeliveryEvidenceRecordRow, PermissionPreset, Spec,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.mcp import server
from okto_pulse.core.ports import McpCredential
from okto_pulse.core.ports.mcp_resources import freeze_mcp_resource_catalog
from okto_pulse.core.ports.permission_policy import (
    builtin_permission_presets, flatten_permission_flags, get_permission_flag,
    registered_permission_flags, set_permission_flag,
)
from okto_pulse.core.runtime_registry import register_unit_of_work_factory
from okto_pulse.core.services.main import AgentService

ledger = adopted.ledger


def grants(*, review):
    selected = {
        'board': {'read': True},
        'agent': {'entity': {'read': True}},
        'card': {
            'entity': {'read': True, 'edit_fields': not review},
            'conclusion': {'write': not review},
            'validation': {'read': True, 'submit': review},
            'interact_in': {state: True for state in ('in_progress', 'validation', 'done', 'rejected')},
            'move': {'in_progress_to_validation': not review},
        },
        'code_traceability': {'evidence': {'read': True}, 'target': {'execution_submit': not review}},
    }
    flags = {}
    for permission in flatten_permission_flags(registered_permission_flags()):
        set_permission_flag(flags, permission, False)
    for permission in flatten_permission_flags(selected):
        set_permission_flag(flags, permission, get_permission_flag(selected, permission))
    return flags


def payload(result):
    assert not result.is_error, result.content
    body = json.loads(result.content[0].text)
    return body['data'] if 'outcome' in body else body


async def snapshot(factory):
    async with factory() as db:
        card = await db.get(Card, 'task')
        records = list(await db.scalars(select(CardDeliveryEvidenceRecordRow).order_by(CardDeliveryEvidenceRecordRow.id)))
        return deepcopy({
            'status': card.status, 'version': card.policy_version,
            'conclusions': card.conclusions, 'validations': card.validations,
            'rejections': card.rejection_records,
            'records': {row.id: {'actor_id': row.actor_id, 'payload': row.payload} for row in records},
        })


@pytest.mark.asyncio
@pytest.mark.parametrize('recommendation', ['approve', 'reject'])
@pytest.mark.parametrize('executor_can_review', [False, True], ids=['grant-denied', 'separation-enforced'])
async def test_executor_and_reviewer_handoff_preserves_report_and_grant_boundaries(
    ledger, tmp_path, monkeypatch, recommendation, executor_can_review,
):
    seed, _, _ = await adopted.setup(ledger, tmp_path, monkeypatch)
    factory = async_sessionmaker(seed.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={'realm_scope': RealmScope.local()})
    try:
        board = await seed.get(Board, adopted.BOARD)
        board.settings = {**board.settings, 'require_task_validation': True,
                          'reviewer_separation_mode': 'enforce'}
        spec = await seed.get(Spec, adopted.SPEC)
        spec.require_task_validation = True
        card = await seed.get(Card, 'task')
        card.created_by = card.assignee_id = 'executor'
        root = next(row for row in builtin_permission_presets() if row['name'] == 'Full Control')
        seed.add(PermissionPreset(id='handoff-root', name=root['name'], flags=root['flags'], is_builtin=True))
        for identity, review in [('executor', False), ('reviewer', True)]:
            flags = grants(review=review)
            if identity == 'executor' and executor_can_review:
                set_permission_flag(flags, 'card.validation.submit', True)
            seed.add(Agent(id=identity, name=identity, created_by='owner',
                api_key='fixture-' + identity, api_key_hash=AgentService.hash_api_key('fixture-' + identity),
                is_active=True, permissions=[], preset_id='handoff-root', permission_flags=flags))
            seed.add(AgentBoard(id='grant-' + identity, agent_id=identity,
                board_id=adopted.BOARD, granted_by='owner'))
        await seed.commit()
        await seed.close()
        register_unit_of_work_factory(CommunityUnitOfWorkFactory(factory))
        server.register_mcp_authenticator(make_community_mcp_authenticator(session_factory=factory))
        server._permission_cache.clear()
        identity = 'reviewer'
        monkeypatch.setattr(server, 'active_api_key_credential', lambda: McpCredential(
            source='x_api_key_header', value='fixture-' + identity))
        frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
        host = CommunityMcpHostProvider().materialize_catalog(
            server.mcp, resource_catalog=frozen, projection_identity=frozen.identity)
        before = await snapshot(factory)
        command = adopted.request('complete')
        command = command.model_copy(update={
            'batch': command.batch.model_copy(update={'expected_card_version': before['version']}),
            'report': command.report.model_copy(update={'status': 'validation'}),
        })
        arguments = {'board_id': adopted.BOARD, 'card_id': 'task', 'spec_id': adopted.SPEC,
                     'evidence': command.model_dump(mode='json', exclude_none=True,
                         exclude={'board_id', 'card_id', 'spec_id'})}
        async with Client(host) as reviewer_client:
            resolved = await server._get_agent_ctx(adopted.BOARD)
            assert resolved.agent_id == 'reviewer'
            assert not resolved.permissions.owner_review_required, resolved.permissions.review_reason
            assert resolved.permissions.flags['code_traceability']['target']['execution_submit'] is False
            refused = await reviewer_client.call_tool('okto_pulse_record_delivery_evidence', arguments, raise_on_error=False)
            assert refused.is_error, refused.content
            assert json.loads(refused.content[0].text)['error_code'] == 'forbidden'
            assert await snapshot(factory) == before

        identity = 'executor'
        async with Client(host) as executor_client:
            resolved = await server._get_agent_ctx(adopted.BOARD)
            assert resolved.agent_id == 'executor'
            assert not resolved.permissions.owner_review_required, resolved.permissions.review_reason
            assert resolved.permissions.flags['code_traceability']['target']['execution_submit'] is True
            assert resolved.permissions.flags['card']['validation']['submit'] is executor_can_review
            accepted = payload(await executor_client.call_tool('okto_pulse_record_delivery_evidence', arguments, raise_on_error=False))
        handed_off = await snapshot(factory)
        assert handed_off['status'] == 'validation'
        record_id = accepted['entries'][0]['id']
        assert handed_off['records'][record_id]['actor_id'] == 'executor'
        assert handed_off['conclusions'][-1]['author_id'] == 'executor'
        assert handed_off['conclusions'][-1]['delivery_manifest']['records'][0]['id'] == record_id
        validation = {
            'board_id': adopted.BOARD, 'card_id': 'task',
            'expected_subject_version': handed_off['version'], 'idempotency_key': 'review-1',
            'confidence': 95, 'confidence_justification': 'Inspected the accepted implementation record',
            'estimated_completeness': 100, 'completeness_justification': 'All allocated implementation is present',
            'estimated_drift': 0, 'drift_justification': 'No difference from the assigned scope',
            'general_justification': 'Independent reviewer inspected the preserved implementation and report',
            'recommendation': recommendation,
        }
        async with Client(host) as executor_client:
            refused = await executor_client.call_tool('okto_pulse_submit_task_validation', validation, raise_on_error=False)
            if executor_can_review:
                outcome = json.loads(refused.content[0].text)
                assert outcome['outcome'] == 'action_required', outcome
                assert outcome['error_code'] == 'reviewer_separation_required', outcome
                assert outcome['next_action'] == {'hint': 'request_independent_task_validator'}
            else:
                assert refused.is_error and 'card.validation.submit' in str(refused), refused.content
            assert await snapshot(factory) == handed_off

        identity = 'reviewer'
        async with Client(host) as reviewer_client:
            resume = payload(await reviewer_client.call_tool('okto_pulse_get_delivery_evidence', {
                'board_id': adopted.BOARD, 'spec_id': adopted.SPEC, 'card_id': 'task', 'view': 'resume',
            }, raise_on_error=False))
            assert resume['status'] == 'validation' and resume['card_version'] == handed_off['version']
            assert resume['implementation_proofs']['items'][0]['record_id'] == record_id
            assert resume['implementation_proofs']['items'][0]['actor_id'] == 'executor'
            assert not resume['actions']['record_progress']
            conclusions = payload(await reviewer_client.call_tool('okto_pulse_get_task_conclusions', {
                'board_id': adopted.BOARD, 'card_id': 'task',
            }, raise_on_error=False))
            assert conclusions['conclusions'][-1]['delivery_manifest']['records'][0]['id'] == record_id
            assert conclusions['conclusions'][-1]['author_id'] == 'executor'
            assert await snapshot(factory) == handed_off
            reviewed = payload(await reviewer_client.call_tool('okto_pulse_submit_task_validation', validation, raise_on_error=False))
        completed = await snapshot(factory)
        assert completed['status'] == ('done' if recommendation == 'approve' else 'rejected'), reviewed
        assert len(completed['validations']) == 1
        assert completed['validations'][0]['reviewer_id'] == 'reviewer'
        assert completed['validations'][0]['recommendation'] == recommendation
        assert completed['conclusions'][:len(handed_off['conclusions'])] == handed_off['conclusions']
        assert completed['records'] == handed_off['records']
        assert bool(completed['rejections']) == (recommendation == 'reject')
        identity = 'executor'
        async with Client(host) as executor_client:
            resumed = payload(await executor_client.call_tool('okto_pulse_get_delivery_evidence', {
                'board_id': adopted.BOARD, 'spec_id': adopted.SPEC, 'card_id': 'task', 'view': 'resume',
            }, raise_on_error=False))
            assert resumed['status'] == completed['status']
            assert resumed['implementation_proofs']['items'][0]['record_id'] == record_id
            assert not resumed['recovery']['receipt_ownership_transferred']
        assert await snapshot(factory) == completed
    finally:
        await seed.close()
