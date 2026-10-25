"""Persisted reads reauthorize each page and bind cursors to the actor."""

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import ACTOR, setup
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader, CommunityReadinessClock
from okto_pulse.community.adapters.sqlalchemy_models import AgentBoard
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.core.ports.delivery_readiness import ReadinessActor, ReadinessReadError

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


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
    assert (await reader.requeue_terminal(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, actor=ACTOR))['requeued']
    async with session.bind.connect() as connection:
        assert (await connection.exec_driver_sql("SELECT previous_error_code FROM readiness_operation_audit "
            "WHERE scope_id=?", (f'{delivery.SPEC_ID}:1',))).scalar_one() == 'forced'
    await session.execute(delete(AgentBoard).where(AgentBoard.agent_id == ACTOR.actor_id))
    await session.commit()
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await reader.load_persisted(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
            actor=ACTOR, unit_limit=1, cursor=page['next_cursor'])
