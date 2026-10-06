"""AC-VER-17: an authenticated executor cannot dispense a locked active OR."""
from copy import deepcopy

import pytest
from fastmcp import Client
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.mcp_auth import make_community_mcp_authenticator
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent, AgentBoard, Card, DomainEventRow, PermissionPreset, Spec, SpecHistory,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.mcp import server
from okto_pulse.core.ports import McpCredential
from okto_pulse.core.ports.mcp_resources import freeze_mcp_resource_catalog
from okto_pulse.core.ports.permission_policy import builtin_permission_presets, set_permission_flag
from okto_pulse.core.runtime_registry import register_unit_of_work_factory
from okto_pulse.core.services.main import AgentService

from test_verification_start_transition import adopted_context as source_context, four_profiles
from test_single_agent_spec_execution import call

adopted_context = source_context


@pytest.mark.asyncio
async def test_executor_cannot_drop_or_qualification_or_reopen_without_authority(adopted_context, tmp_path, monkeypatch):
    db = adopted_context
    await four_profiles(db, tmp_path)
    spec = await db.get(Spec, 'spec', populate_existing=True)
    spec.status = 'in_progress'
    card = await db.get(Card, 'task')
    card.status = 'in_progress'
    card.created_by = card.assignee_id = 'executor'
    root = next(row for row in builtin_permission_presets() if row['name'] == 'Full Control')
    db.add(PermissionPreset(id='executor-preset', name=root['name'], flags=root['flags'], is_builtin=True))
    flags = deepcopy(root['flags'])
    set_permission_flag(flags, 'spec.move.in_progress_to_draft', False)
    db.add(Agent(id='executor', name='Executor', created_by='author', api_key='fixture-executor',
        api_key_hash=AgentService.hash_api_key('fixture-executor'), is_active=True,
        preset_id='executor-preset', permission_flags=flags))
    db.add(AgentBoard(id='executor-board', agent_id='executor', board_id='board', granted_by='author'))
    await db.commit()
    factory = async_sessionmaker(db.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={'realm_scope': RealmScope.local()})
    await db.close()
    register_unit_of_work_factory(CommunityUnitOfWorkFactory(factory))
    server.register_mcp_authenticator(make_community_mcp_authenticator(session_factory=factory))
    server._permission_cache.clear()
    monkeypatch.setattr(server, 'active_api_key_credential', lambda: McpCredential(
        source='x_api_key_header', value='fixture-executor'))
    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(server.mcp,
        resource_catalog=frozen, projection_identity=frozen.identity)

    async def snapshot():
        async with factory() as reader:
            current = await reader.get(Spec, 'spec')
            return deepcopy(dict(status=current.status, version=current.version, edition=current.edition,
                requirements=current.observability_requirements, criteria=current.acceptance_criteria,
                scenarios=current.test_scenarios, evaluations=current.evaluations,
                history=await reader.scalar(select(func.count()).select_from(SpecHistory)),
                events=await reader.scalar(select(func.count()).select_from(DomainEventRow))))

    scope = {'board_id': 'board', 'spec_id': 'spec'}
    async with Client(host) as client:
        actor = await server._get_agent_ctx('board')
        assert actor.agent_id == 'executor' and not actor.permissions.owner_review_required
        assert actor.permissions.flags['spec']['observability_requirements']['edit']
        assert not actor.permissions.flags['spec']['move']['in_progress_to_draft']
        before = await snapshot()
        diagnostic = await call(client, 'okto_pulse_get_requirement_verification', **scope,
            requirement_type='observability_requirement', requirement_id='or')
        assert diagnostic['items'][0]['verification']['required_profiles'] == ['operational']
        assert diagnostic['items'][0]['qualification_resolved']
        for payload in ({'verification': {'mode': 'none'}}, {'verification': None},
                        {'status': 'not_applicable'}, {'verifiable': False}):
            refused = await client.call_tool('okto_pulse_update_spec_entity', {
                **scope, 'entity_type': 'observability_requirement', 'entity_id': 'or',
                'operation': 'update', 'expected_spec_version': before['version'],
                'payload_json': payload}, raise_on_error=False)
            assert 'subject_edit_requires_draft' in str(refused), refused.content
            assert await snapshot() == before
        refused = await client.call_tool('okto_pulse_move_spec', {**scope, 'status': 'draft'}, raise_on_error=False)
        assert 'spec.move.in_progress_to_draft' in str(refused), refused.content
        assert await snapshot() == before
        after = await call(client, 'okto_pulse_get_requirement_verification', **scope,
            requirement_type='observability_requirement', requirement_id='or')
        assert after == diagnostic
        assert before['requirements'][0]['status'] == 'active'
        assert all(item['status'] == 'ready' and not item.get('evidence') for item in before['scenarios'])
