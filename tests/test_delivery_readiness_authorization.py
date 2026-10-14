"""P05 native access, policy drift and evidence are observed in one SQL snapshot."""
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import delete, select, update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_batch import reader, statements
from test_delivery_readiness_verification import prepare_reports
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, BoardShare, Card, PermissionPreset
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
from okto_pulse.core.ports.delivery_readiness import ReadinessAccessDenied, ReadinessActor, ReadinessReadError
from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.core.services.delivery_readiness import compose_authorized_delivery_diagnostics

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]
ACTOR = ReadinessActor('readiness-agent', 'agent', 'local', delivery.BOARD_ID)


async def setup(session):
    session.add_all([
        PermissionPreset(id='readiness-base', name='Readiness base', owner_id='owner', flags=registered_permission_flags()),
        PermissionPreset(id='readiness-child', name='Readiness child', owner_id='owner',
            flags={}, base_preset_id='readiness-base'),
        Agent(id=ACTOR.actor_id, name='Private agent name', created_by='agent-creator',
            api_key='secret-marker', api_key_hash='secret-hash', preset_id='readiness-child', permission_flags={}),
        AgentBoard(id='readiness-grant', agent_id=ACTOR.actor_id, board_id=delivery.BOARD_ID,
            granted_by='owner', permission_overrides=None),
        BoardShare(id='readiness-share', board_id=delivery.BOARD_ID, user_id='shared', realm_id='local',
            permission='viewer', shared_by='owner'),
    ])
    await session.commit()


async def read(session, *, actor=ACTOR, kind=CommunityDeliveryReadinessReader, **query):
    return await reader(session, kind=kind).load(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        actor=actor, **query)


@pytest.mark.parametrize('actor', [ACTOR, ReadinessActor('owner', 'human', 'local'),
    ReadinessActor('shared', 'human', 'local')])
async def test_current_membership_and_policy_are_read_only_and_secret_free(ledger, actor):
    session, _, _ = ledger
    await setup(session)
    before = (await session.execute(select(Agent.__table__))).all()
    await session.rollback()
    with statements(session.bind) as sql:
        result = await read(session, actor=actor)
    assert result.access.actor_id == actor.actor_id
    assert result.access.allows('code_traceability.evidence.read')
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert (await session.execute(select(Agent.__table__))).all() == before
    assert 'secret-marker' not in repr(result) and 'secret-hash' not in repr(result)
    assert 'Private agent name' not in repr(result)
    assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize('failure', ['inactive', 'grant_revoked', 'realm', 'bound_board', 'unknown_kind',
    'human_without_share', 'share_wrong_realm', 'agent_owner_without_grant'])
async def test_membership_denial_happens_before_evidence_and_is_not_empty_success(ledger, failure):
    session, _, _ = ledger
    await setup(session)
    actor = ACTOR
    if failure == 'inactive':
        await session.execute(update(Agent).where(Agent.id == ACTOR.actor_id).values(is_active=False))
    elif failure in {'grant_revoked', 'agent_owner_without_grant'}:
        await session.execute(delete(AgentBoard).where(AgentBoard.id == 'readiness-grant'))
        if failure == 'agent_owner_without_grant':
            actor = replace(actor, actor_id='owner')
    elif failure == 'realm':
        actor = replace(actor, realm_id='foreign')
    elif failure == 'bound_board':
        actor = replace(actor, board_id='foreign')
    elif failure == 'unknown_kind':
        actor = replace(actor, actor_kind='unknown')
    elif failure == 'human_without_share':
        actor = ReadinessActor('stranger', 'human', 'local')
    else:
        actor = ReadinessActor('shared', 'human', 'local')
        await session.execute(update(BoardShare).where(BoardShare.id == 'readiness-share').values(realm_id='foreign'))
    await session.commit()
    with statements(session.bind) as sql, pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await read(session, actor=actor)
    assert not any('FROM specs' in s or 'FROM card_delivery_evidence' in s for s in sql)


@pytest.mark.parametrize('failure', ['direct_review', 'unknown_preset', 'preset_cycle', 'empty_ceiling', 'read_denied', 'spec_denied'])
async def test_native_permission_denial_and_owner_review_do_not_become_default_authority(ledger, failure):
    session, _, _ = ledger
    await setup(session)
    if failure == 'direct_review':
        await session.execute(update(Agent).where(Agent.id == ACTOR.actor_id).values(
            preset_id=None, permission_flags={'spec': {'entity': {'read': True}}}))
    elif failure == 'unknown_preset':
        await session.execute(update(Agent).where(Agent.id == ACTOR.actor_id).values(preset_id='unknown'))
    elif failure == 'preset_cycle':
        await session.execute(update(PermissionPreset).where(PermissionPreset.id == 'readiness-base').values(
            base_preset_id='readiness-child'))
    elif failure == 'empty_ceiling':
        await session.execute(update(AgentBoard).where(AgentBoard.id == 'readiness-grant').values(permission_overrides={}))
    else:
        flags = registered_permission_flags()
        if failure == 'read_denied':
            flags['code_traceability']['evidence']['read'] = False
        else:
            flags['spec']['entity']['read'] = False
        await session.execute(update(AgentBoard).where(AgentBoard.id == 'readiness-grant').values(permission_overrides=flags))
    await session.commit()
    native = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context(ACTOR.actor_id,
        board_id=delivery.BOARD_ID)
    assert not native.permissions.has('code_traceability.evidence.read') or not native.permissions.has('spec.entity.read')
    await session.rollback()
    with statements(session.bind) as sql, pytest.raises(ReadinessAccessDenied):
        await read(session)
    assert not any('FROM specs' in s for s in sql)


async def test_policy_changes_invalidate_snapshot_and_guidance_without_changing_proof(ledger):
    session, store, _ = ledger
    await setup(session)
    await delivery.record(store, delivery.command())
    await session.commit()
    before = await read(session)
    old = compose_authorized_delivery_diagnostics(assess_obligation_delivery(before.facts), before, actor_id=ACTOR.actor_id)
    flags = registered_permission_flags()
    flags['spec']['tests']['execute'] = False
    flags['code_traceability']['target']['read'] = False
    await session.execute(update(PermissionPreset).where(PermissionPreset.id == 'readiness-base').values(flags=deepcopy(flags)))
    await session.commit()
    after = await read(session)
    assert before.facts == after.facts
    assert before.snapshot_id != after.snapshot_id
    assert before.access.allows('spec.tests.execute') and not after.access.allows('spec.tests.execute')
    assert aggregate_delivery_metrics(assess_obligation_delivery(before.facts)) == aggregate_delivery_metrics(assess_obligation_delivery(after.facts))
    current = compose_authorized_delivery_diagnostics(assess_obligation_delivery(after.facts), after, actor_id=ACTOR.actor_id)
    assert old.graph.actions and all(a.state == 'forbidden' for a in current.graph.actions)
    assert all(d.detail_state == 'restricted' for d in current.details)


async def test_concurrent_grant_revocation_retries_authority_and_never_returns_old_allowed_facts(ledger):
    session, _, _ = ledger
    await setup(session)
    class RacingReader(CommunityDeliveryReadinessReader):
        async def _load_transaction(self, read_session, board_id, spec_id, card_id, counts):
            result = await super()._load_transaction(read_session, board_id, spec_id, card_id, counts)
            await session.execute(delete(AgentBoard).where(AgentBoard.id == 'readiness-grant'))
            await session.commit()
            # The reader sees the old grant inside its original transaction.
            assert await read_session.scalar(select(AgentBoard.id).where(AgentBoard.id == 'readiness-grant'))
            return result
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await read(session, kind=RacingReader)


async def test_authority_batch_query_count_is_independent_of_card_count(ledger):
    session, _, _ = ledger
    await setup(session)
    with statements(session.bind) as first:
        await read(session)
    session.add_all([Card(id=f'extra-{i}', board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        title='Extra', created_by='owner', card_type='normal', status='in_progress') for i in range(30)])
    await session.commit()
    with statements(session.bind) as second:
        await read(session)
    assert len(first) == len(second) == 17  # Original 13 plus Board/Agent/grant/preset batch.


async def test_current_card_read_denial_is_applied_before_delivery_load(ledger):
    session, _, _ = ledger
    await setup(session)
    flags = registered_permission_flags()
    flags['card']['entity']['read'] = False
    await session.execute(update(AgentBoard).where(AgentBoard.id == 'readiness-grant').values(
        permission_overrides=flags))
    await session.commit()
    with pytest.raises(ReadinessAccessDenied) as exc:
        await read(session, card_id='task')
    assert exc.value.required_permission == 'card.entity.read'


async def test_human_permissions_match_native_user_resolution_even_when_its_created_agent_is_inactive(ledger):
    session, _, _ = ledger
    await setup(session)
    await session.execute(update(Agent).where(Agent.id == ACTOR.actor_id).values(created_by='shared', is_active=False))
    flags = registered_permission_flags()
    flags['spec']['tests']['execute'] = False
    await session.execute(update(PermissionPreset).where(PermissionPreset.id == 'readiness-base').values(flags=flags))
    await session.commit()
    native = await CommunitySqlAlchemyApplicationPersistence().resolve_user_permissions(session,
        user_id='shared', board_id=delivery.BOARD_ID)
    await session.rollback()
    current = await read(session, actor=ReadinessActor('shared', 'human', 'local'))
    assert current.access.allows('code_traceability.evidence.read') == native.has('code_traceability.evidence.read')
    assert not current.access.allows('spec.tests.execute') and not native.has('spec.tests.execute')


async def test_share_role_changes_execution_availability_in_same_snapshot_without_changing_proof(ledger, tmp_path):
    session, _, _, _, _ = await prepare_reports(ledger, tmp_path, 'inspection', [None])
    await setup(session)
    actor = ReadinessActor('shared', 'human', 'local')
    before = await read(session, actor=actor)
    original = compose_authorized_delivery_diagnostics(assess_obligation_delivery(before.facts), before, actor_id='shared')
    assert before.access.allows('spec.tests.execute') and not before.access.spec_write_access
    assert original.graph.actions and all(a.kind == 'execute_verification' and a.state == 'forbidden'
        for a in original.graph.actions)
    await session.execute(update(BoardShare).where(BoardShare.id == 'readiness-share').values(permission='editor'))
    await session.commit()
    after = await read(session, actor=actor)
    changed = compose_authorized_delivery_diagnostics(assess_obligation_delivery(after.facts), after, actor_id='shared')
    assert after.access.spec_write_access
    assert all(a.state == 'ready' for a in changed.graph.actions)
    assert before.facts == after.facts and before.snapshot_id != after.snapshot_id
