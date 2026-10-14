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
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, BoardShare, Card, PermissionPreset, Spec
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
from okto_pulse.core.ports.delivery_readiness import ReadinessAccessDenied, ReadinessActor, ReadinessReadError
from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.core.services.delivery_readiness import compose_authorized_delivery_diagnostics, EvaluatedDeliveryReadiness

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]
ACTOR = ReadinessActor('readiness-agent', 'agent', 'local', delivery.BOARD_ID)


async def setup(session):
    # The reader now composes real Core completion checks through native ports.
    from okto_pulse.core.ports.application_persistence import register_application_persistence_port
    from okto_pulse.core.ports.relational_application import register_relational_application_adapter
    from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
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


@pytest.mark.parametrize('card_id', [None, 'task', 'test'])
async def test_S10_capacity_is_authorized_unknown_with_native_detail(ledger, monkeypatch, card_id):
    session, _, _ = ledger
    await setup(session)
    monkeypatch.setattr('okto_pulse.community.adapters.sqlalchemy_delivery_readiness.MAX_CARDS', 1)
    with statements(session.bind) as sql:
        result = await read(session, card_id=card_id)
    assert result.access.actor_id == ACTOR.actor_id
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert not result.facts.implementations and not result.facts.tests
    assert not result.facts.obligations and not result.scenarios
    assert [c.id for c in result.cards] == ([card_id] if card_id else [])
    projected = EvaluatedDeliveryReadiness(result, actor_id=ACTOR.actor_id)
    assessment = projected.assessment(card_id)
    metrics = aggregate_delivery_metrics(assessment)
    assert metrics.verification.status == 'unknown' and metrics.verification.percent is None
    assert not assessment.completeness.population_known
    assert projected.diagnostics(card_id).graph.causes[0].native_reason_codes == result.unavailable_reason_codes
    assert (await read(session, card_id=card_id)).snapshot_id == result.snapshot_id


async def test_capacity_never_hides_foreign_card_or_revoked_grant(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    monkeypatch.setattr('okto_pulse.community.adapters.sqlalchemy_delivery_readiness.MAX_CARDS', 1)
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await read(session, card_id='foreign')
    await session.execute(delete(AgentBoard).where(AgentBoard.agent_id == ACTOR.actor_id))
    await session.commit()
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await read(session)


async def test_authority_capacity_cannot_be_returned_as_authorized_unknown(ledger):
    session, _, _ = ledger
    await setup(session)
    class AuthorityOverflow(CommunityDeliveryReadinessReader):
        async def _rows(self, session, statement, limit, counts):
            if 'permission_presets' in str(statement):
                raise ReadinessReadError('readiness_limit_exceeded', native_code='readiness_population_limit')
            return await super()._rows(session, statement, limit, counts)
        async def _capacity_snapshot(self, *args):
            pytest.fail('unresolved authority must not produce authorized unknown')
    with pytest.raises(ReadinessReadError, match='readiness_limit_exceeded'):
        await read(session, kind=AuthorityOverflow)


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


@pytest.mark.parametrize('realm_id', ['local', 'tenant-readiness'])
async def test_native_spec_gate_is_evaluated_in_authorized_snapshot_without_writes(ledger, realm_id):
    session, _, _ = ledger
    await setup(session)
    from okto_pulse.community.adapters.sqlalchemy_models import Board
    await session.execute(update(Board).where(Board.id == delivery.BOARD_ID).values(realm_id=realm_id))
    actor = replace(ACTOR, realm_id=realm_id)
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        acceptance_criteria=[{'id': 'uncovered', 'title': 'Private criterion text'}],
        test_scenarios=[], skip_test_coverage=False))
    await session.commit()
    with statements(session.bind) as sql:
        batch = await read(session, actor=actor)
    assert batch.access.realm_id == realm_id
    assert ('spec', 'in_progress') in batch.access.completion_states
    assert batch.native_lifecycle.check_status == 'blocked'
    assert batch.native_lifecycle.blocking_codes == ('coverage_incomplete',)
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert 'Private criterion text' not in repr(batch.native_lifecycle)
    assert (await read(session, actor=actor)).snapshot_id == batch.snapshot_id
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await read(session, actor=replace(actor, realm_id='wrong-realm'))
    flags = registered_permission_flags()
    flags['spec']['interact_in']['in_progress'] = False
    await session.execute(update(AgentBoard).where(AgentBoard.agent_id == ACTOR.actor_id).values(
        permission_overrides=flags))
    await session.commit()
    restricted = await read(session, actor=actor)
    assert restricted.facts == batch.facts
    assert restricted.native_lifecycle.check_status == 'not_evaluated'
    assert restricted.native_lifecycle.blocking_codes == ()
    assert restricted.snapshot_id != batch.snapshot_id


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


@pytest.mark.parametrize('native_gate', [False, True])
async def test_authority_batch_query_count_is_independent_of_card_count(ledger, native_gate, caplog):
    caplog.set_level('DEBUG', logger='okto_pulse.community.adapters.sqlalchemy_delivery_readiness')
    session, _, _ = ledger
    await setup(session)
    if native_gate:
        # A deterministic native gate path for both populations, without mocks.
        await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
            acceptance_criteria=[{'id': 'uncovered', 'title': 'Uncovered criterion'}],
            test_scenarios=[], skip_test_coverage=False))
    else:
        flags = registered_permission_flags()
        flags['spec']['interact_in']['in_progress'] = False
        await session.execute(update(AgentBoard).where(AgentBoard.id == 'readiness-grant').values(
            permission_overrides=flags))
    await session.commit()
    with statements(session.bind) as first:
        await read(session)
    session.add_all([Card(id=f'extra-{i}', board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        title='Extra', created_by='owner', card_type='normal', status='in_progress') for i in range(30)])
    await session.commit()
    with statements(session.bind) as second:
        await read(session)
    # Native coverage check adds scalar Spec/Board reads; neither path loads per Card.
    assert len(first) == len(second) == (19 if native_gate else 17)
    logs = [r for r in caplog.records if r.msg.startswith('readiness_batch_read')]
    assert len(logs) == 2
    assert [r.args[1] for r in logs] == [len(first), len(second)]
    assert all('secret-marker' not in r.getMessage() for r in logs)


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


@pytest.mark.parametrize('status', ['in_progress', 'validation'])
async def test_native_task_validation_gate_preserves_complete_proof(ledger, status):
    session, store, _ = ledger
    await setup(session)
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    await session.commit()
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.execute(update(Card).where(Card.id == 'task').values(status=status))
    await session.commit()
    with statements(session.bind) as sql:
        batch = await read(session, card_id='task')
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert batch.native_lifecycle.check_status == 'blocked'
    assert batch.native_lifecycle.blocking_codes == ('task_validation_required',)
    context = EvaluatedDeliveryReadiness(batch, actor_id=ACTOR.actor_id).context('task')
    assert context.implementation.percent == context.verification.percent == 100
    assert context.delivery_gate.status == 'allowed'
    assert context.lifecycle.qualification_is_current is False


@pytest.mark.parametrize('case', ['empty_links', 'missing_scenario', 'ready', 'tampered'])
async def test_native_test_card_admission_matches_readiness_without_writes(ledger, case):
    from okto_pulse.community.adapters.sqlalchemy_models import Board
    from okto_pulse.core.services.main import CardService
    from okto_pulse.core.services.gate_contracts import GateContractError
    session, _, evidence = ledger
    await setup(session)
    await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress'))
    if case in {'empty_links', 'missing_scenario'}:
        await session.execute(update(Card).where(Card.id == 'test').values(
            test_scenario_ids=[] if case == 'empty_links' else ['unresolved-scenario']))
    else:
        scenario = {**delivery.SCENARIO, 'status': 'ready' if case == 'ready' else 'passed',
            'evidence': evidence if case == 'ready' else {**evidence, 'execution_receipt': 'forged'}}
        await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(test_scenarios=[scenario]))
    await session.commit()
    with statements(session.bind) as sql:
        batch = await read(session, card_id='test')
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    card = await session.get(Card, 'test')
    board = await session.get(Board, delivery.BOARD_ID)
    with pytest.raises(GateContractError) as native:
        await CardService(session).validate_completion_admission(card, board)
    assert batch.native_lifecycle.blocking_codes == (native.value.code,) == ('test_card_completion_blocked',)
    assert batch.native_lifecycle.check_status == 'blocked'
    assert card.status.value == 'in_progress'
    assert not session.new and not session.dirty and not session.deleted


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


@pytest.mark.parametrize('outcome', ['passed', 'failed'])
@pytest.mark.parametrize('associated', [False, True])
async def test_authenticated_test_outcome_can_finish_execution_without_requiring_pass(ledger, tmp_path, outcome, associated):
    from okto_pulse.community.adapters.sqlalchemy_models import Board
    from okto_pulse.core.services.main import CardService
    session, store, refs, implementation, scenarios = await prepare_reports(ledger, tmp_path, 'inspection', [outcome])
    if associated:
        await delivery.record(store, delivery.command('test', scenario_id=scenarios[0]['id'],
            obligation_refs=refs, implementation_ids=[implementation['id']]))
        await session.commit()
    await setup(session)
    await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress'))
    await session.commit()
    card = await session.get(Card, 'test')
    board = await session.get(Board, delivery.BOARD_ID)
    with statements(session.bind) as sql:
        await CardService(session).validate_completion_admission(card, board)
        batch = await read(session, card_id='test')
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert 'test_card_completion_blocked' not in batch.native_lifecycle.reason_codes
    assert not batch.native_lifecycle.blocking_codes
    context = EvaluatedDeliveryReadiness(batch, actor_id=ACTOR.actor_id).context('test')
    # Execution may finish after a failure, but verified delivery additionally
    # requires the native association to the actual implementation being tested.
    assert context.verification.percent == (100 if outcome == 'passed' and associated else 0)
    assert card.status.value == 'in_progress'
    assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize('dependency_status', ['in_progress', 'done', 'cancelled'])
async def test_native_card_dependency_block_is_read_only_and_preserves_proof(ledger, dependency_status):
    from okto_pulse.community.adapters.sqlalchemy_models import Board, CardDependency
    from okto_pulse.core.domain.enums import CardStatus
    from okto_pulse.core.services.main import CardService
    from okto_pulse.core.services.card_errors import CardOperationError
    session, store, _ = ledger
    await setup(session)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.execute(update(Board).where(Board.id == delivery.BOARD_ID).values(settings={'require_task_validation': False}))
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    session.add(Card(id='prerequisite', board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        title='Private prerequisite title', status=dependency_status, card_type='normal',
        position=0, created_by='owner'))
    session.add(CardDependency(card_id='task', depends_on_id='prerequisite'))
    await session.commit()
    card = await session.get(Card, 'task')
    with statements(session.bind) as sql:
        if dependency_status == 'in_progress':
            with pytest.raises(CardOperationError) as caught:
                await CardService(session).validate_dependency_admission(card, CardStatus.DONE)
            assert caught.value.code == 'dependencies_incomplete'
        else:
            await CardService(session).validate_dependency_admission(card, CardStatus.DONE)
        batch = await read(session, card_id='task')
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert batch.native_lifecycle.blocking_codes == (('dependencies_incomplete',) if dependency_status == 'in_progress' else ())
    context = EvaluatedDeliveryReadiness(batch, actor_id=ACTOR.actor_id).context('task')
    assert context.implementation.percent == context.verification.percent == 100
    assert 'Private prerequisite' not in context.model_dump_json()
    assert card.status.value == 'in_progress'
    assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize('mode', ['advisory', 'blocking'])
async def test_native_card_reference_gate_preserves_human_policy_and_proof(ledger, mode):
    from okto_pulse.community.adapters.sqlalchemy_models import Board
    session, store, _ = ledger
    await setup(session)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.execute(update(Board).where(Board.id == delivery.BOARD_ID).values(
        settings={'require_task_validation': False, 'missing_link_gate': mode}))
    await session.execute(update(Card).where(Card.id == 'task').values(
        status='in_progress', linked_test_task_ids=['private-missing-reference']))
    await session.commit()
    with statements(session.bind) as sql:
        batch = await read(session, card_id='task')
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert batch.native_lifecycle.blocking_codes == (('missing_links_open',) if mode == 'blocking' else ())
    context = EvaluatedDeliveryReadiness(batch, actor_id=ACTOR.actor_id).context('task')
    assert context.implementation.percent == context.verification.percent == 100
    assert 'private-missing-reference' not in context.model_dump_json()
    assert not session.new and not session.dirty and not session.deleted


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
