"""Source diagnostics share the audit transaction, history and undo boundary."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_audit_repo import CommunityAuditRepository
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, ConsolidationAudit, GlobalUpdateOutbox
from okto_pulse.core.kg.interfaces.audit_dtos import ConsolidationAuditData, OutboxEventData
from okto_pulse.core.ports.projection_findings import ProjectionFindingSnapshot, ProjectionReferenceFinding


@pytest_asyncio.fixture
async def storage(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "audit.db"}')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Board(id='board', name='Board', owner_id='owner'))
        await session.commit()
    try:
        yield engine, factory, CommunityAuditRepository(factory)
    finally:
        await engine.dispose()


def snapshot(*, empty=False):
    finding = ProjectionReferenceFinding('board', 'card', 'card', 'card_scenarios',
        'card:card:test_scenario_ids', 'spec:spec:test_scenario:missing', 'target_absent')
    return ProjectionFindingSnapshot('board', 'card', 'card', 'card_scenarios', 'a' * 64,
        () if empty else (finding,))


async def stage(repository, session, number, value=None, **changes):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=number)
    audit = ConsolidationAuditData(session_id=f'session-{number}', board_id='board',
        artifact_id='card', artifact_type='card', agent_id='system:historical_consolidation',
        started_at=now, committed_at=now, content_hash='b' * 64).model_copy(update=changes)
    event = OutboxEventData(event_id=f'event-{number}', board_id='board', session_id=audit.session_id,
        event_type='consolidation_committed', payload={'artifact_id': 'card'})
    await repository.stage_consolidation_records(session, audit, [], event, reference_findings=value)


async def latest(repository, **changes):
    return await repository.get_latest_reference_findings(**(dict(board_id='board', artifact_id='card',
        artifact_type='card', namespace='card_scenarios') | changes))


@pytest.mark.asyncio
async def test_receipt_rollback_commit_empty_closure_and_undo(storage):
    _, factory, repository = storage
    assert await latest(repository) is None
    async with factory() as session:
        await stage(repository, session, 1, snapshot())
        assert (await session.get(ConsolidationAudit, 'session-1')).reference_findings == snapshot().to_payload()
        await session.rollback()
    assert await latest(repository) is None
    async with factory() as session:
        assert (await session.execute(select(GlobalUpdateOutbox))).scalars().all() == []
        await stage(repository, session, 2, snapshot())
        await session.commit()
    assert await latest(repository) == snapshot()
    async with factory() as session:
        await stage(repository, session, 3, agent_id='cognitive_closeout_worker')
        await session.commit()
    assert await latest(repository) == snapshot()
    async with factory() as session:
        assert (await session.execute(text("SELECT reference_findings FROM consolidation_audit WHERE session_id='session-3'"))).scalar_one() is None
        await stage(repository, session, 4, snapshot(empty=True))
        await session.commit()
    assert await latest(repository) == snapshot(empty=True)
    await repository.mark_audit_undone('session-4')
    assert await latest(repository) == snapshot()
    async with factory() as session:
        assert (await session.get(ConsolidationAudit, 'session-4')).reference_findings == snapshot(empty=True).to_payload()
    assert 'reference_findings' not in (await repository.get_audit_by_session('session-2')).model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['board', 'owner', 'type', 'writer'])
async def test_rejects_cross_scope_or_cognitive_source_diagnostics_before_writes(storage, damage):
    _, factory, repository = storage
    value = snapshot(empty=True)
    changes = {}
    if damage == 'board': value = replace(value, board_id='foreign')
    if damage == 'owner': value = replace(value, owner_id='foreign')
    if damage == 'type': value = replace(value, owner_type='spec')
    if damage == 'writer': changes['agent_id'] = 'cognitive_closeout_worker'
    async with factory() as session:
        with pytest.raises(ValueError, match='audit_scope_invalid'):
            await stage(repository, session, 1, value, **changes)
        await session.commit()
        assert (await session.execute(select(ConsolidationAudit))).scalars().all() == []
        assert (await session.execute(select(GlobalUpdateOutbox))).scalars().all() == []


@pytest.mark.asyncio
async def test_read_scope_and_corruption_are_not_clean_results(storage):
    _, factory, repository = storage
    async with factory() as session:
        await stage(repository, session, 1, snapshot())
        await session.commit()
    for changes in ({'board_id': 'foreign'}, {'artifact_id': 'foreign'},
                    {'artifact_type': 'spec'}, {'namespace': 'other'}):
        assert await latest(repository, **changes) is None
    async with factory() as session:
        row = await session.get(ConsolidationAudit, 'session-1')
        payload = row.reference_findings.copy()
        payload['source_fingerprint'] = 'invalid'
        row.reference_findings = payload
        await session.commit()
    with pytest.raises(ValueError, match='fingerprint_invalid'):
        await latest(repository)
