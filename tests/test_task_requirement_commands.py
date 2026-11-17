from types import SimpleNamespace
from unittest.mock import AsyncMock
import copy
import pytest
from sqlalchemy import select, func, text, update, event
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.exc import IntegrityError
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory

from test_delivery_progress import db
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, Spec, ActivityLog, DomainEventRow,
    TaskRequirementDecisionRow as Decision, TaskRequirementDecisionOperationRow as Operation,
)
from okto_pulse.community.adapters.readiness_invalidation_schema import install_readiness_invalidation_triggers
from okto_pulse.core.application.use_cases.base import ActorContext, PermissionDeniedError
from okto_pulse.core.application.use_cases.task_requirement_decision import GetTaskRequirementOptionsUseCase, RecordTaskRequirementDecisionUseCase, GetTaskRequirementHistoryUseCase
from okto_pulse.core.models.task_requirement_decision import TaskRequirementOptionsQuery, TaskRequirementDecisionCommand, TaskRequirementHistoryQuery


FLAGS = ['spec.entity.read', 'card.entity.read', 'code_traceability.evidence.read',
         'code_traceability.waiver.create', 'code_traceability.waiver.clear']


def actor(kind='user', flags=None):
    return ActorContext(actor_id='owner', actor_kind=kind, source='system', board_id='b', permissions=FLAGS if flags is None else flags)


def uow(session, store):
    return SimpleNamespace(services=SimpleNamespace(delivery_evidence=store), commit=AsyncMock(side_effect=session.commit))


def query(cards=None):
    return TaskRequirementOptionsQuery(board_id='b', spec_id='s', card_ids=cards or ['c'])


def command(preview, **changes):
    return TaskRequirementDecisionCommand(**(dict(board_id='b', spec_id='s',
        contract_version='task-requirement-decision/v1', card_ids=[row['card_id'] for row in preview['cards']],
        decision='waived', basis=preview['basis'], note=None, idempotency_key='decision-1') | changes))


async def options(unit, *, who=None, cards=None):
    return await GetTaskRequirementOptionsUseCase().execute(query(cards), actor=who or actor(), uow=unit)


async def test_human_decision_replay_restore_and_read_only_options(db):
    engine, session, store = db
    unit = uow(session, store)
    writes = []
    def observed(_connection, _cursor, statement, *_args):
        if statement.lstrip().split()[0].upper() in {'UPDATE', 'INSERT', 'DELETE'}:
            writes.append(statement)
    event.listen(engine.sync_engine, 'before_cursor_execute', observed)
    try:
        preview = await options(unit)
        assert not writes
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', observed)
    assert preview['allowed_decisions'] == ['waived', 'optional', 'required']
    assert preview['cards'][0]['decision'] == 'required' and preview['can_apply']
    request = command(preview, note='   ')
    result = await RecordTaskRequirementDecisionUseCase().execute(request, actor=actor(), uow=unit)
    assert result['status'] == 'applied' and not result['replayed']
    row = (await session.scalars(select(Decision))).one()
    assert row.payload['user_note'] is None and row.payload['actor_id'] == 'owner'
    assert (await session.get(Card, 'c')).status == 'in_progress'
    assert await session.scalar(select(func.count()).select_from(ActivityLog)) == 1
    assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 1
    assert await RecordTaskRequirementDecisionUseCase().execute(request, actor=actor(), uow=unit) == {**result, 'replayed': True}
    with pytest.raises(ValueError, match='idempotency_conflict'):
        await RecordTaskRequirementDecisionUseCase().execute(request.model_copy(update={'note': 'different'}), actor=actor(), uow=unit)
    restore = command(await options(unit), decision='required', idempotency_key='restore')
    await RecordTaskRequirementDecisionUseCase().execute(restore, actor=actor(), uow=unit)
    rows = list((await session.scalars(select(Decision).order_by(Decision.revision))).all())
    assert [row.decision for row in rows] == ['waived', 'required']
    assert rows[1].payload['supersedes'] == rows[0].id
    assert await session.scalar(select(func.count()).select_from(Operation)) == 2


async def test_agent_cannot_write_and_stale_or_frozen_basis_preserves_state(db):
    _, session, store = db
    unit = uow(session, store)
    agent_preview = await options(unit, who=actor('agent'))
    assert agent_preview['allowed_decisions'] == [] and not agent_preview['can_apply']
    with pytest.raises(PermissionDeniedError, match='human_authorization_required'):
        await RecordTaskRequirementDecisionUseCase().execute(command(agent_preview), actor=actor('agent'), uow=unit)
    preview = await options(unit)
    await session.execute(update(Card).where(Card.id == 'c').values(policy_version=2))
    await session.commit()
    with pytest.raises(ValueError, match='stale_basis'):
        await RecordTaskRequirementDecisionUseCase().execute(command(preview), actor=actor(), uow=unit)
    await session.rollback()
    await session.execute(update(Card).where(Card.id == 'c').values(status='done'))
    await session.commit()
    frozen = await options(unit)
    assert not frozen['can_apply'] and frozen['cards'][0]['reason_code'] == 'reopen_required'
    with pytest.raises(ValueError, match='reopen_required'):
        await RecordTaskRequirementDecisionUseCase().execute(command(frozen), actor=actor(), uow=unit)
    await session.rollback()
    assert await session.scalar(select(func.count()).select_from(Decision)) == 0


async def test_event_failure_rolls_back_decisions_activity_and_readiness_invalidation(db):
    engine, session, store = db
    async with engine.begin() as connection:
        await connection.run_sync(install_readiness_invalidation_triggers)
        await connection.execute(text("CREATE TRIGGER fail_requirement_event BEFORE INSERT ON domain_events WHEN NEW.event_type='task.requirement_decision_applied' BEGIN SELECT RAISE(ABORT, 'injected_event_failure'); END"))
    unit = uow(session, store)
    preview = await options(unit)
    before = await session.scalar(text('SELECT coalesce(max(source_revision),0) FROM readiness_scope'))
    with pytest.raises(IntegrityError, match='injected_event_failure'):
        await RecordTaskRequirementDecisionUseCase().execute(command(preview), actor=actor(), uow=unit)
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Decision)) == 0
    assert await session.scalar(select(func.count()).select_from(Operation)) == 0
    assert await session.scalar(select(func.count()).select_from(ActivityLog)) == 0
    assert await session.scalar(text('SELECT coalesce(max(source_revision),0) FROM readiness_scope')) == before
    assert await session.scalar(text('SELECT count(*) FROM readiness_job')) == 0


async def test_command_refreshes_a_cached_task_after_another_transaction_changes_it(db):
    engine, writer, _ = db
    async with build_community_session_factory(engine)() as reader:
        cached_card = await reader.get(Card, 'c')
        unit = uow(reader, CommunityDeliveryEvidenceStore(reader))
        preview = await options(unit)
        assert cached_card.policy_version == 1
        # The preview transaction ends, but its identity-map object is retained.
        await reader.commit()
        await writer.execute(update(Card).where(Card.id == 'c').values(policy_version=2))
        await writer.commit()
        assert cached_card.policy_version == 1
        with pytest.raises(ValueError, match='stale_basis'):
            await RecordTaskRequirementDecisionUseCase().execute(command(preview), actor=actor(), uow=unit)
        await reader.rollback()
        assert await reader.scalar(select(func.count()).select_from(Decision)) == 0


async def test_one_frozen_task_rejects_the_entire_selection_without_partial_decisions(db):
    _, session, store = db
    session.add(Card(id='c2', board_id='b', spec_id='s', title='Second task', status='in_progress',
                     card_type='normal', position=1, created_by='owner'))
    await session.commit()
    spec = await session.get(Spec, 's')
    requirements = copy.deepcopy(spec.functional_requirements)
    requirements.append({'id': 'fr2', 'text': 'Second requirement', 'linked_task_ids': ['c2'],
        'verification': {'mode': 'explicit', 'required_profiles': ['functional']},
        'implementation_plan': {'contributions': [{
            'card_id': 'c2', 'scope': 'selected_criteria', 'criterion_ids': ['ac2'], 'summary': 'Second contribution'}]}})
    spec.functional_requirements = requirements
    criteria = copy.deepcopy(spec.acceptance_criteria)
    criteria.append({'id': 'ac2', 'text': 'Second criterion', 'verification_profile': 'functional',
        'linked_task_ids': ['c2'],
        'requirement_links': [{'requirement_type': 'functional_requirement', 'requirement_id': 'fr2'}]})
    spec.acceptance_criteria = criteria
    await session.commit()
    unit = uow(session, store)
    await options(unit, cards=['c', 'c2'])
    await session.execute(update(Card).where(Card.id == 'c2').values(status='done'))
    await session.commit()
    preview = await options(unit, cards=['c', 'c2'])
    assert not preview['can_apply']
    with pytest.raises(ValueError, match='reopen_required'):
        await RecordTaskRequirementDecisionUseCase().execute(command(preview), actor=actor(), uow=unit)
    await session.rollback()
    assert await session.scalar(select(func.count()).select_from(Decision)) == 0
    assert await session.scalar(select(func.count()).select_from(Operation)) == 0


async def test_history_pages_preserve_author_time_notes_restore_and_previous_editions(db):
    _, session, store = db
    unit = uow(session, store)
    request = command(await options(unit), note='x' * 600)
    result = await RecordTaskRequirementDecisionUseCase().execute(request, actor=actor(), uow=unit)
    await RecordTaskRequirementDecisionUseCase().execute(command(await options(unit),
        decision='required', idempotency_key='restore-history'), actor=actor(), uow=unit)
    query = TaskRequirementHistoryQuery(board_id='b', spec_id='s', card_ids=['c'], limit=1)
    history = GetTaskRequirementHistoryUseCase()
    page = await history.execute(query, actor=actor('agent'), uow=unit)
    assert page['total'] == 2 and page['next_before_revision'] == 2
    assert page['items'][0]['decision'] == 'required' and page['items'][0]['actor_id'] == 'owner'
    assert page['items'][0]['recorded_at']
    previous = await history.execute(query.model_copy(update={'before_revision': 2}), actor=actor(), uow=unit)
    assert previous['items'][0]['decision'] == 'waived'
    assert previous['items'][0]['note_truncated'] and len(previous['items'][0]['note']) == 500
    detail = await history.execute(query.model_copy(update={'record_id': result['decision_ids'][0]}), actor=actor(), uow=unit)
    assert detail['items'][0]['note'] == 'x' * 600 and not detail['items'][0]['note_truncated']
    await session.execute(update(Spec).where(Spec.id == 's').values(edition=2))
    await session.commit()
    current = await history.execute(query, actor=actor(), uow=unit)
    assert current['edition'] == 2 and current['items'] == []
    old = await history.execute(query.model_copy(update={'edition': 1}), actor=actor(), uow=unit)
    assert old['total'] == 2 and old['current_edition'] == 2
    with pytest.raises(ValueError, match='edition_not_found'):
        await history.execute(query.model_copy(update={'edition': 3}), actor=actor(), uow=unit)
    with pytest.raises(ValueError, match='decision_not_found'):
        await history.execute(query.model_copy(update={'edition': 1, 'record_id': 'foreign'}), actor=actor(), uow=unit)
    assert await session.scalar(select(func.count()).select_from(Decision)) == 2
