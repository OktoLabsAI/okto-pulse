"""Real SQL ledgers, signed proofs and reserved amendment confirmations."""
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

import test_delivery_evidence_integration as delivery
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader
from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.community.adapters.sqlalchemy_amendment_revision import CommunitySqlAlchemyAmendmentRevisionStore
from okto_pulse.core.ports.application_persistence import register_application_persistence_port
from okto_pulse.core.ports.amendment_revision import register_amendment_revision_store
from okto_pulse.core.services.amendment_revision import AmendmentRevisionService
from okto_pulse.core.services.amendment_coverage import current_coverage_basis
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, assess_delivery_gate
from okto_pulse.core.domain.delivery_evidence import DeliveryScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.enums import CardStatus, SpecStatus
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError

ledger = delivery.ledger
pytestmark = pytest.mark.asyncio


async def prepare(ledger):
    session, store, _ = ledger
    session.info['realm_scope'] = RealmScope.local()
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_amendment_revision_store(CommunitySqlAlchemyAmendmentRevisionStore())
    impl = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[impl['id']]))
    revision = await session.get(Spec, delivery.SPEC_ID)
    revision.status = SpecStatus.DONE
    session.add(Spec(id='original', board_id=delivery.BOARD_ID, title='Original',
        status=SpecStatus.DONE, edition=1, version=1, created_by='owner',
        architecture_adoption=ArchitectureAdoptionScope(board_id=delivery.BOARD_ID,
            spec_id='original', adopted_in_edition=1, actor_id='owner', inherited_resource_ids=()).model_dump(mode='json'),
        **{field: [] for _, field in delivery.COLLECTIONS if field not in {'functional_requirements', 'acceptance_criteria'}},
        functional_requirements=[{'id': 'original-fr', 'text': 'Original expected behavior',
            'linked_task_ids': ['origin-task'],
            'verification': {'mode': 'explicit', 'required_profiles': ['functional']},
            'implementation_plan': {'contributions': [{'card_id': 'origin-task', 'scope': 'whole_requirement'}]}}],
        acceptance_criteria=[{'id': 'original-ac', 'text': 'Original behavior is observable',
            'verification_profile': 'functional', 'linked_task_ids': ['origin-task'],
            'requirement_links': [{'requirement_type': 'functional_requirement', 'requirement_id': 'original-fr'}]}],
        test_scenarios=[{**delivery.SCENARIO, 'id': 'original-scenario', 'status': 'ready',
            'linked_criteria': ['original-ac'], 'linked_task_ids': ['original-test']}],
        execution_contract=new_execution_contract(board_id=delivery.BOARD_ID,
            spec_id='original', edition=1, actor_id='owner', origin='new_spec')))
    await session.flush()
    session.add(Card(id='origin-task', board_id=delivery.BOARD_ID, spec_id='original',
        title='Original task', status=CardStatus.DONE, created_by='owner'))
    session.add(Card(id='original-test', board_id=delivery.BOARD_ID, spec_id='original',
        title='Original verification', card_type='test', status=CardStatus.DONE,
        test_scenario_ids=['original-scenario'], created_by='owner'))
    await session.flush()
    session.add(Card(id='origin-bug', board_id=delivery.BOARD_ID, spec_id='original',
        title='Regression', description='Exact problem', details='Normative scope',
        card_type='bug', origin_task_id='origin-task', expected_behavior='Correct behavior',
        observed_behavior='Regression', status=CardStatus.IN_PROGRESS, created_by='owner'))
    await session.commit()
    service = AmendmentRevisionService(session)
    amendment = await service.create(board_id=delivery.BOARD_ID, original_spec_id='original',
        origin_bug_id='origin-bug', revision_spec_id=delivery.SPEC_ID,
        origin_task_ids=['origin-task'], regression_test_task_ids=['test'],
        regression_scenario_ids=[delivery.SCENARIO['id']], author='owner')
    await service.set_lineage_state(amendment.id, 'complete', 'owner')
    await service.set_status(amendment.id, 'done', 'owner')
    await session.commit()
    amendment = await service.get(amendment.id)
    basis = await current_coverage_basis(session, amendment=amendment, task_id='test',
        scenario_id=delivery.SCENARIO['id'], scenario_spec_id=delivery.SPEC_ID)
    assert basis is not None
    confirmation = dict(validator_id='reviewer', amendment_revision_id=amendment.id,
        regression_test_task_id='test', regression_scenario_id=delivery.SCENARIO['id'],
        evidence_ref=basis.evidence_ref, basis=asdict(basis))
    return session, store, service, amendment, confirmation, impl


async def assessment(store):
    snapshot = await store.load_snapshot(DeliveryScope(delivery.BOARD_ID, 'original', 1))
    return assess_obligation_delivery(snapshot, card_id='origin-bug', card_type='bug')


async def test_native_confirmation_freezes_delivery_and_bug_edit_revokes_only_credit(ledger):
    session, store, service, amendment, confirmation, _ = await prepare(ledger)
    frozen = await store.amendment_delivery_attribution(amendment, confirmation)
    assert frozen is not None
    confirmation['delivery_attribution'] = frozen
    await service.set_coverage_confirmation(amendment.id, confirmation=confirmation, actor='reviewer')
    await session.commit()
    assert assess_delivery_gate(await assessment(store)).allowed
    persisted = (await service.get(amendment.id)).validation_metadata['coverage_confirmation']
    assert persisted['delivery_attribution'] == frozen
    snapshot = await CommunityDeliveryReadinessReader(session.bind,
        clock=SimpleNamespace(now=lambda: datetime.now(timezone.utc))).load(
        board_id=delivery.BOARD_ID, spec_id='original', card_id='origin-bug')
    projected = assess_obligation_delivery(snapshot.facts, card_id='origin-bug', card_type='bug')
    assert assess_delivery_gate(projected).allowed
    bug = await session.get(Card, 'origin-bug')
    bug.expected_behavior = 'A changed requirement after confirmation'
    await session.commit()
    assert not assess_delivery_gate(await assessment(store)).allowed
    assert (await service.get(amendment.id)).validation_metadata['coverage_confirmation'] == persisted


async def test_legacy_confirmation_is_not_implicit_delivery(ledger):
    session, store, service, amendment, confirmation, _ = await prepare(ledger)
    await service.set_coverage_confirmation(amendment.id, confirmation=confirmation, actor='reviewer')
    await session.commit()
    assert not assess_delivery_gate(await assessment(store)).allowed


async def test_revoked_source_implementation_invalidates_origin_delivery(ledger):
    session, store, service, amendment, confirmation, impl = await prepare(ledger)
    confirmation['delivery_attribution'] = await store.amendment_delivery_attribution(amendment, confirmation)
    assert confirmation['delivery_attribution'] is not None
    await service.set_coverage_confirmation(amendment.id, confirmation=confirmation, actor='reviewer')
    await session.commit()
    assert assess_delivery_gate(await assessment(store)).allowed
    await delivery.record(store, delivery.command('revoke', card_id='task', record_id=impl['id']), human=True)
    await session.commit()
    assert not assess_delivery_gate(await assessment(store)).allowed


@pytest.mark.parametrize('mutation', ['reopen', 'reset'])
async def test_revision_reopen_or_regression_reset_removes_closeout_credit(ledger, mutation):
    session, store, service, amendment, confirmation, _ = await prepare(ledger)
    confirmation['delivery_attribution'] = await store.amendment_delivery_attribution(amendment, confirmation)
    assert confirmation['delivery_attribution'] is not None
    await service.set_coverage_confirmation(amendment.id, confirmation=confirmation, actor='reviewer')
    await session.commit()
    assert assess_delivery_gate(await assessment(store)).allowed
    revision = await session.get(Spec, delivery.SPEC_ID)
    if mutation == 'reopen':
        revision.status = SpecStatus.IN_PROGRESS
    else:
        revision.test_scenarios = [{**s, 'status': 'ready'} for s in revision.test_scenarios]
    await session.commit()
    assert not assess_delivery_gate(await assessment(store)).allowed


@pytest.mark.parametrize('mutation', ['revision_reopened', 'bug_semantics_changed', 'amendment_cancelled'])
async def test_other_session_cannot_leave_cached_attribution_current(ledger, mutation):
    session, store, service, amendment, confirmation, _ = await prepare(ledger)
    confirmation['delivery_attribution'] = await store.amendment_delivery_attribution(amendment, confirmation)
    assert confirmation['delivery_attribution'] is not None
    await service.set_coverage_confirmation(amendment.id, confirmation=confirmation, actor='reviewer')
    await session.commit()
    assert assess_delivery_gate(await assessment(store)).allowed
    # Keep identity-map objects alive across transactions, including a revision
    # Spec that the original native inventory would not refresh itself.
    held = (await session.get(Card, 'origin-bug'), await session.get(Spec, delivery.SPEC_ID),
        await service.get(amendment.id))
    await session.commit()
    async with build_community_session_factory(session.bind)() as writer:
        writer.info['realm_scope'] = RealmScope.local()
        if mutation == 'revision_reopened':
            (await writer.get(Spec, delivery.SPEC_ID)).status = SpecStatus.IN_PROGRESS
        elif mutation == 'bug_semantics_changed':
            (await writer.get(Card, 'origin-bug')).expected_behavior = 'New obligation'
        else:
            await AmendmentRevisionService(writer).set_status(amendment.id, 'cancelled', 'owner')
        await writer.commit()
    assert held
    assert not assess_delivery_gate(await assessment(store)).allowed


@pytest.mark.parametrize('boundary', ['absent_board_realm', 'conflicting_session_realm'])
async def test_internal_snapshot_rejects_unproven_realm(ledger, boundary):
    session, _, _ = ledger
    reader = CommunityDeliveryReadinessReader(session.bind,
        clock=SimpleNamespace(now=lambda: datetime.now(timezone.utc)))
    if boundary == 'absent_board_realm':
        board = await session.get(Board, delivery.BOARD_ID)
        board.realm_id = ''
        await session.commit()
        session.info.pop('realm_scope', None)
    else:
        session.info['realm_scope'] = RealmScope.tenant('unrelated-realm')
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await reader._load_transaction(session, delivery.BOARD_ID, delivery.SPEC_ID,
            None, {'queries': 0, 'facts': 0}, defer_facts=True)


def _read_attribution_in_spawn(database, evidence_root):
    """Exercise the production worker scope without inheriting API providers."""
    import asyncio
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine
    from okto_pulse.community.adapters.readiness_projection_worker import _projection_verifier_scope
    from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
    from okto_pulse.core.ports.test_evidence import (
        resolve_test_evidence_execution_issuer, resolve_test_verification_report_issuer,
    )
    from okto_pulse.core.ports.verification_currentness import resolve_verification_reservation_port

    async def read():
        engine = create_async_engine(f'sqlite+aiosqlite:///{database}')

        @event.listens_for(engine.sync_engine, 'before_cursor_execute')
        def readonly(_conn, _cursor, statement, _parameters, _context, _many):
            assert statement.lstrip().upper().startswith(('SELECT', 'PRAGMA', 'BEGIN'))

        try:
            with _projection_verifier_scope(evidence_root):
                assert resolve_test_evidence_execution_issuer() is None
                assert resolve_test_verification_report_issuer() is None
                assert resolve_verification_reservation_port() is None
                snapshot, _revision = await CommunityDeliveryReadinessReader(engine,
                    clock=SimpleNamespace(now=lambda: datetime.now(timezone.utc))).load_for_projection(
                    board_id=delivery.BOARD_ID, spec_id='original',
                    blocking_execution=TrackedBlockingExecution())
                result = assess_obligation_delivery(snapshot.facts,
                    card_id='origin-bug', card_type='bug')
                return assess_delivery_gate(result).allowed
        finally:
            await engine.dispose()

    return asyncio.run(read())


@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
@pytest.mark.parametrize('ledger_present', [True, False])
async def test_spawned_projection_captures_confirmed_amendment(ledger, tmp_path, ledger_present):
    import asyncio
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    session, store, service, amendment, confirmation, _ = await prepare(ledger)
    confirmation['delivery_attribution'] = await store.amendment_delivery_attribution(amendment, confirmation)
    assert confirmation['delivery_attribution'] is not None
    await service.set_coverage_confirmation(amendment.id, confirmation=confirmation, actor='reviewer')
    await session.commit()
    evidence_root = tmp_path / ('evidence' if ledger_present else 'absent-evidence')
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context('spawn')) as executor:
        allowed = await asyncio.get_running_loop().run_in_executor(
            executor, _read_attribution_in_spawn, session.bind.url.database, evidence_root)
    assert allowed is ledger_present
    if not ledger_present:
        assert not evidence_root.exists()
