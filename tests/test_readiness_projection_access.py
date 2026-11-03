"""Persisted reads reauthorize each page and bind cursors to the actor."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import ACTOR, setup
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_persisted_readiness import CommunityPersistedReadinessStore
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader, CommunityReadinessClock
from okto_pulse.community.adapters.sqlalchemy_models import AgentBoard, Board, Card, CardDependency, Spec
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.core.ports.delivery_readiness import (
    ReadinessAccessDenied, ReadinessActor, ReadinessReadError,
)
from okto_pulse.core.ports.permission_policy import registered_permission_flags

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


async def test_projection_skips_second_policy_query_without_concurrent_commit(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    original_access = reader._access_persisted
    calls = 0

    async def count_access(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original_access(*args, **kwargs)

    monkeypatch.setattr(reader, '_access_persisted', count_access)
    result, _, _, _ = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR)
    assert result.freshness == 'current'
    assert calls == 1
    async with session.bind.connect() as connection:
        stored = (await connection.exec_driver_sql(
            'SELECT payload FROM readiness_summary WHERE generation_id=? ORDER BY card_key',
            (result.generation_id,))).scalars().all()
    assert len(stored) > 1
    assert result.summaries == tuple(json.loads(payload) for payload in stored)


async def test_projection_pages_are_actor_bound_and_reauthorized(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    import okto_pulse.core as core
    monkeypatch.setattr(core, 'get_settings', lambda: SimpleNamespace(
        guideline_policy_cursor_signing_key='readiness-test-installation-key-000000000'))
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    first, edition, _, page = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR, unit_limit=1)
    assert first.freshness == 'current' and first.generation_id
    assert edition == 1 and page['total'] > 1 and page['next_cursor']
    next_page, _, _, continuation = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR, unit_limit=1, cursor=page['next_cursor'])
    assert next_page.generation_id == first.generation_id
    assert next_page.units[0] != first.units[0]
    assert continuation['offset'] == 1
    with pytest.raises(ReadinessReadError, match='readiness_query_invalid'):
        await reader.load_persisted(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
            actor=ReadinessActor('owner', 'human', 'local'), unit_limit=1,
            cursor=page['next_cursor'])
    async with session.bind.begin() as connection:
        await connection.exec_driver_sql("UPDATE readiness_job SET state='terminal',error_code='forced' "
            "WHERE scope_id=?", (f'{delivery.SPEC_ID}:1',))
    with pytest.raises(ReadinessAccessDenied):
        await reader.requeue_terminal(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, actor=ReadinessActor('shared', 'human', 'local'),
            edition=1,
            expected_revision=first.source_revision, idempotency_key='unauthorized',
            reason='Must not requeue')
    with pytest.raises(ReadinessReadError, match='readiness_requeue_conflict'):
        await reader.requeue_terminal(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, actor=ACTOR, edition=2,
            expected_revision=first.source_revision, idempotency_key='wrong-edition',
            reason='Must not cross editions')
    assert (await reader.requeue_terminal(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR,
        edition=1,
        expected_revision=first.source_revision, idempotency_key='repair-one',
        reason='Storage recovered'))['queued']
    async with session.bind.connect() as connection:
        assert (await connection.exec_driver_sql("SELECT previous_error_code FROM readiness_operation_audit "
            "WHERE scope_id=?", (f'{delivery.SPEC_ID}:1',))).scalar_one() == 'forced'
    await session.execute(delete(AgentBoard).where(AgentBoard.agent_id == ACTOR.actor_id))
    await session.commit()
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await reader.load_persisted(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
            actor=ACTOR, unit_limit=1, cursor=page['next_cursor'])


async def test_projection_revocation_during_read_fails_closed(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    original_read = CommunityPersistedReadinessStore.read
    calls = 0

    async def revoke_after_read(store, **kwargs):
        nonlocal calls
        result = await original_read(store, **kwargs)
        calls += 1
        if calls == 1:
            flags = registered_permission_flags()
            flags['code_traceability']['evidence']['read'] = False
            async with session.bind.begin() as connection:
                await connection.execute(update(AgentBoard).where(
                    AgentBoard.agent_id == ACTOR.actor_id).values(permission_overrides=flags))
        return result

    monkeypatch.setattr(CommunityPersistedReadinessStore, 'read', revoke_after_read)
    with pytest.raises(ReadinessAccessDenied):
        await reader.load_persisted(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, actor=ACTOR, unit_limit=1)
    assert calls == 1


async def test_projection_changing_scope_retries_once_then_fails_closed(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    original_read = CommunityPersistedReadinessStore.read
    calls = 0

    async def change_after_read(store, **kwargs):
        nonlocal calls
        result = await original_read(store, **kwargs)
        calls += 1
        async with session.bind.begin() as connection:
            await connection.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
                version=Spec.version + 1))
        return result

    monkeypatch.setattr(CommunityPersistedReadinessStore, 'read', change_after_read)
    with pytest.raises(ReadinessReadError, match='readiness_snapshot_changed'):
        await reader.load_persisted(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, actor=ACTOR, unit_limit=1)
    assert calls == 2


async def test_projection_source_revision_change_during_read_is_updating(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    original_read = CommunityPersistedReadinessStore.read
    calls = 0
    first_revision = None

    async def change_source_after_read(store, **kwargs):
        nonlocal calls, first_revision
        result = await original_read(store, **kwargs)
        calls += 1
        if calls == 1:
            first_revision = result.source_revision
            async with session.bind.begin() as connection:
                await connection.exec_driver_sql(
                    'UPDATE specs SET description=? WHERE id=?',
                    (f'new source revision {calls}', delivery.SPEC_ID))
        return result

    monkeypatch.setattr(CommunityPersistedReadinessStore, 'read', change_source_after_read)
    result, _, _, _ = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR)
    assert calls == 1
    assert result.source_revision > first_revision
    assert result.captured_revision == first_revision
    assert result.freshness == 'updating'


async def test_projection_cursor_keeps_original_generation_until_retention(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    import okto_pulse.core as core
    monkeypatch.setattr(core, 'get_settings', lambda: SimpleNamespace(
        guideline_policy_cursor_signing_key='readiness-test-installation-key-000000000'))
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    old, _, _, page = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR, unit_limit=1)
    assert old.generation_id and page['next_cursor']

    async with session.bind.begin() as connection:
        await connection.exec_driver_sql("UPDATE specs SET description='second revision' WHERE id=?",
            (delivery.SPEC_ID,))
    await asyncio.sleep(.06)
    assert await worker.process_once() == 1
    new, _, _, _ = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR, unit_limit=1)
    assert new.generation_id != old.generation_id
    continued, _, _, next_page = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR, unit_limit=1, cursor=page['next_cursor'])
    assert continued.generation_id == old.generation_id
    assert continued.summaries == old.summaries
    assert continued.units[0] != old.units[0]
    assert next_page['offset'] == 1

    async with session.bind.begin() as connection:
        await connection.exec_driver_sql("UPDATE readiness_generation SET state='expired' WHERE id=?",
            (old.generation_id,))
    with pytest.raises(ReadinessReadError, match='readiness_cursor_stale'):
        await reader.load_persisted(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, actor=ACTOR, unit_limit=1, cursor=page['next_cursor'])


async def test_delivery_summary_reads_one_generation_and_live_metadata_without_proof_scans(ledger, tmp_path, monkeypatch):
    from test_execution_contract_integration import adopt_fixture
    from test_delivery_readiness_authorization import statements
    from okto_pulse.core.services.delivery_summary import page_delivery_summary
    session, _, _ = ledger
    await setup(session)
    await adopt_fixture(session, tmp_path, monkeypatch)
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    async def forbidden(*_args, **_kwargs):
        pytest.fail('The simple list must not load or evaluate source proof')
    monkeypatch.setattr(reader, 'load', forbidden)
    monkeypatch.setattr(delivery.CommunityDeliveryEvidenceStore, 'load_snapshot', forbidden)
    monkeypatch.setattr(delivery.CommunityDeliveryEvidenceStore, '_execution_plan', forbidden)
    with statements(session.bind) as sql:
        population = await reader.load_delivery_summary(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, actor=ACTOR)
    assert {item.card_id for item in population.items} == {'task', 'test'}
    assert population.projection.freshness == 'current'
    assert all(item.updated_at and item.updated_at.utcoffset() is not None for item in population.items)
    assert all(statement.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for statement in sql)
    result = page_delivery_summary(population)
    assert 'secret-marker' not in result.model_dump_json() and 'Private agent name' not in result.model_dump_json()
    assert result.unique_obligation_count == 2 and result.task_count == 2


async def test_delivery_summary_rechecks_revoked_authority_instead_of_returning_an_empty_list(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    original = CommunityPersistedReadinessStore.read
    async def revoke(store, **kwargs):
        result = await original(store, **kwargs)
        flags = registered_permission_flags()
        flags['card']['entity']['read'] = False
        async with session.bind.begin() as connection:
            await connection.execute(update(AgentBoard).where(AgentBoard.agent_id == ACTOR.actor_id)
                .values(permission_overrides=flags))
        return result
    monkeypatch.setattr(CommunityPersistedReadinessStore, 'read', revoke)
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    with pytest.raises(ReadinessAccessDenied):
        await reader.load_delivery_summary(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, actor=ACTOR)


async def test_delivery_summary_retries_a_concurrent_metadata_change_and_preserves_new_title(ledger, monkeypatch):
    session, _, _ = ledger
    await setup(session)
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    original = CommunityPersistedReadinessStore.read
    calls = 0
    async def change(store, **kwargs):
        nonlocal calls
        result = await original(store, **kwargs)
        calls += 1
        if calls == 1:
            async with session.bind.begin() as connection:
                await connection.execute(update(Card).where(Card.id == 'task').values(title='Current task title'))
        return result
    monkeypatch.setattr(CommunityPersistedReadinessStore, 'read', change)
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    result = await reader.load_delivery_summary(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, actor=ACTOR)
    assert calls == 2
    assert next(row.title for row in result.items if row.card_id == 'task') == 'Current task title'
    assert not result.complete and result.projection.freshness == 'updating'


async def test_delivery_summary_metadata_capacity_is_unknown_and_never_changes_the_denominator(ledger, monkeypatch):
    from okto_pulse.core.services.delivery_summary import page_delivery_summary
    session, _, _ = ledger
    await setup(session)
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    monkeypatch.setattr('okto_pulse.community.adapters.sqlalchemy_delivery_readiness.MAX_CARDS', 1)
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    population = await reader.load_delivery_summary(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, actor=ACTOR)
    assert len(population.items) == 1 and not population.complete
    result = page_delivery_summary(population)
    assert result.task_count is None and result.unique_obligation_count is None
    assert all(group.count is None for group in result.groups)


async def test_summary_dependency_reason_matches_native_closed_states_and_hides_foreign_status(ledger):
    session, _, _ = ledger
    await setup(session)
    await session.execute(update(Card).where(Card.id == 'task').values(status='not_started'))
    await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress'))
    session.add(CardDependency(id='dependency', card_id='task', depends_on_id='test'))
    await session.commit()
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    async def metadata():
        *_, cards, _complete = await reader.load_persisted(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, actor=ACTOR, include_summary_cards=True)
        return next(row for row in cards if row.card_id == 'task')
    assert (await metadata()).dependency_blocked is True
    for status in ('done', 'cancelled'):
        await session.execute(update(Card).where(Card.id == 'test').values(status=status))
        await session.commit()
        assert (await metadata()).dependency_blocked is False
    session.add(Board(id='foreign-board', name='Hidden board', owner_id='someone', realm_id='local'))
    await session.flush()
    session.add(Card(id='foreign-task', board_id='foreign-board', title='Hidden title',
        card_type='normal', status='done', position=0, created_by='someone'))
    await session.flush()
    session.add(CardDependency(id='foreign-dependency', card_id='task', depends_on_id='foreign-task'))
    await session.commit()
    result = await metadata()
    assert result.dependency_blocked is None
    assert 'Hidden title' not in result.model_dump_json()


async def test_summary_captures_human_requirement_and_restores_it_with_the_exact_generation_manifest(ledger, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from test_execution_contract_integration import adopt_fixture
    from okto_pulse.core.application.use_cases.base import ActorContext
    from okto_pulse.core.application.use_cases.task_requirement_decision import (
        GetTaskRequirementOptionsUseCase, RecordTaskRequirementDecisionUseCase,
    )
    from okto_pulse.core.models.task_requirement_decision import TaskRequirementDecisionCommand, TaskRequirementOptionsQuery
    session, store, _ = ledger
    await setup(session)
    await adopt_fixture(session, tmp_path, monkeypatch)
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    await session.commit()
    human = ActorContext(actor_id='owner', actor_kind='human', source='system', board_id=delivery.BOARD_ID,
        permissions=['spec.entity.read', 'card.entity.read', 'code_traceability.evidence.read',
                     'code_traceability.waiver.create', 'code_traceability.waiver.clear'])
    unit = SimpleNamespace(services=SimpleNamespace(delivery_evidence=store), commit=AsyncMock(side_effect=session.commit))
    preview = await GetTaskRequirementOptionsUseCase().execute(TaskRequirementOptionsQuery(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, card_ids=['task']), actor=human, uow=unit)
    decision = await RecordTaskRequirementDecisionUseCase().execute(TaskRequirementDecisionCommand(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, contract_version='task-requirement-decision/v1',
        card_ids=['task'], decision='waived', basis=preview['basis'], idempotency_key='summary-captured-choice'),
        actor=human, uow=unit)
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    population = await reader.load_delivery_summary(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, actor=ACTOR)
    task = next(row for row in population.items if row.card_id == 'task')
    assert task.group == 'optional_or_waived' and task.requirement == 'waived'
    assert task.status == 'in_progress' and not task.historical_done
    visible = await worker.store.read(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, edition=1)
    cold = await CommunityPersistedReadinessStore(session.bind).load_verified_predecessor(
        scope_id=delivery.SPEC_ID + ':1', board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        edition=1, expected_revision=visible.captured_revision)
    assert cold is not None
    captured = next(row for row in cold.summaries if row.card_id == 'task').requirement
    assert captured.decision == 'waived' and captured.current
    assert captured.decision_id == decision['decision_ids'][0]
    from okto_pulse.core.services.persisted_delivery_readiness import readiness_manifest_digest
    assert readiness_manifest_digest(board_id=cold.board_id, spec_id=cold.spec_id, edition=cold.spec_edition,
        captured_revision=cold.captured_revision, source_snapshot_id=cold.source_snapshot_id,
        evaluator_version=cold.evaluator_version, summaries=[row.payload() for row in cold.summaries],
        units=[row.row() for row in cold.units], diagnostics=[row.row() for row in cold.diagnostics]) == cold.manifest_sha256
    preview = await GetTaskRequirementOptionsUseCase().execute(TaskRequirementOptionsQuery(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, card_ids=['task']), actor=human, uow=unit)
    await RecordTaskRequirementDecisionUseCase().execute(TaskRequirementDecisionCommand(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, contract_version='task-requirement-decision/v1',
        card_ids=['task'], decision='required', basis=preview['basis'], idempotency_key='summary-restored-choice'),
        actor=human, uow=unit)
    updating = await reader.load_delivery_summary(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, actor=ACTOR)
    assert updating.projection.freshness == 'updating' and not updating.complete
    assert next(row for row in updating.items if row.card_id == 'task').requirement == 'unknown'
    await asyncio.sleep(.06)
    assert await worker.process_once() == 1
    restored = await reader.load_delivery_summary(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, actor=ACTOR)
    task = next(row for row in restored.items if row.card_id == 'task')
    assert task.requirement == 'required' and task.group == 'in_progress'
    assert task.group != 'delivered'


async def test_summary_signed_pages_reauthorize_and_refuse_live_metadata_changes_within_one_generation(ledger, monkeypatch):
    from okto_pulse.community.adapters.delivery_readiness_cursor import CommunityReadinessCursorCodec
    from okto_pulse.core.models.delivery_summary import DeliverySummaryQuery
    from okto_pulse.core.services.delivery_readiness import DeliveryReadinessService
    session, _, _ = ledger
    await setup(session)
    await session.execute(update(Card).where(Card.id.in_(['task','test'])).values(status='in_progress'))
    await session.commit()
    worker = ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution())
    assert await worker.process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    async def forbidden(*args, **kwargs):
        pytest.fail('Summary pagination must not evaluate proof')
    monkeypatch.setattr(reader, 'load', forbidden)
    service = DeliveryReadinessService(reader, CommunityReadinessCursorCodec(b'summary-test-installation-key-0000000'))
    query = DeliverySummaryQuery(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, group='in_progress', limit=1)
    first = await service.delivery_summary(query, actor=ACTOR)
    token = first['groups'][1]['next_cursor']
    assert token and len(first['groups'][1]['items']) == 1
    second = await service.delivery_summary(query.model_copy(update={'cursor':token}), actor=ACTOR)
    assert first['population_id'] == second['population_id']
    assert first['groups'][1]['items'][0]['card_id'] != second['groups'][1]['items'][0]['card_id']

    # Two successive live edits use the same retained generation. The second
    # must invalidate a cursor already bound to an updating first edit, too.
    for title in ('First changed title','Second changed title'):
        await session.execute(update(Card).where(Card.id=='task').values(title=title))
        await session.commit()
        with pytest.raises(ReadinessReadError, match='readiness_cursor_stale'):
            await service.delivery_summary(query.model_copy(update={'cursor':token}), actor=ACTOR)
        fresh = await service.delivery_summary(query, actor=ACTOR)
        assert fresh['projection']['generation_id'] == first['projection']['generation_id']
        assert fresh['projection']['freshness'] == 'updating'
        assert fresh['task_count'] is None
        token = fresh['groups'][1]['next_cursor']
        assert token

    flags = registered_permission_flags()
    flags['card']['entity']['read'] = False
    await session.execute(update(AgentBoard).where(AgentBoard.agent_id==ACTOR.actor_id).values(permission_overrides=flags))
    await session.commit()
    with pytest.raises(ReadinessAccessDenied):
        await service.delivery_summary(query.model_copy(update={'cursor':token}), actor=ACTOR)
