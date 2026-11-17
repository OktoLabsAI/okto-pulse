"""Task verdict transport uses one authorized projection and bounded note read."""

import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import ACTOR, setup
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader, CommunityReadinessClock
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.ports.delivery_readiness import ReadinessActor
from okto_pulse.core.services.delivery_readiness import DeliveryReadinessService


ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


async def test_task_summary_preserves_scope_and_denies_agent_human_action(ledger):
    session, _, _ = ledger
    await setup(session)
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    await session.commit()
    assert await ReadinessProjectionProcessor(session.bind, TrackedBlockingExecution()).process_once() == 1
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    value, edition, _, _ = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, card_id='task', actor=ACTOR)
    summary = value.task_delivery
    assert summary['contract_version'] == 'task-delivery-summary/v1'
    assert summary['spec_edition'] == edition and summary['card_id'] == 'task'
    assert summary['historical_status'] == 'in_progress'
    assert summary['next_action'] is None or summary['next_action']['kind'] in {'open_details', 'retry'}
    assert summary['state'] != 'delivered'
    owner = ReadinessActor('owner', 'human', 'local', delivery.BOARD_ID)
    human, _, _, _ = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, card_id='task', actor=owner)
    # Identity affects permissible action, never the underlying factual verdict.
    assert human.task_delivery['state'] == summary['state']
    assert human.task_delivery['snapshot_id'] == summary['snapshot_id']
    transported = await DeliveryReadinessService(reader, None).read_persisted(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, card_id='task', actor=ACTOR)
    assert transported['task_delivery'] == summary


async def test_first_projection_read_is_explicitly_unknown_with_current_card_scope(ledger):
    session, _, _ = ledger
    await setup(session)
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    await session.commit()
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    value, _, _, _ = await reader.load_persisted(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, card_id='task', actor=ACTOR)
    assert value.task_delivery['state'] == 'updating_or_unknown'
    assert value.task_delivery['pending_count'] is None
    assert value.task_delivery['historical_status'] == 'in_progress'
    assert value.task_delivery['snapshot_id'] is None
