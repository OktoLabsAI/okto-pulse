"""Persisted reads reauthorize each page and bind cursors to the actor."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import ACTOR, setup
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_persisted_readiness import CommunityPersistedReadinessStore
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader, CommunityReadinessClock
from okto_pulse.community.adapters.sqlalchemy_models import AgentBoard, Spec
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
