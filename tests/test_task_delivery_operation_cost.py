"""Measure the material-event route through the real application writer.

Counts below are application payload calls/UTF-8 bytes, excluding unchanged
session discovery and mandatory gate reads. They are not a token estimate.
"""
import json

import pytest
from sqlalchemy import select, func

from test_delivery_report import db, setup, request
from test_delivery_progress import command as progress_command
from okto_pulse.community.adapters.sqlalchemy_models import Card, CardDeliveryEvidenceRecordRow as Record
from okto_pulse.core.application.use_cases.delivery_evidence import RecordCardDeliveryEvidenceUseCase


@pytest.mark.asyncio
async def test_material_events_handoff_conflict_and_exact_retry_are_bounded(db, record_property):
    session, uow, actor = await setup(db)
    writer = RecordCardDeliveryEvidenceUseCase()
    observed = []

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8'))

    async def call(name, command, *, rejected=False):
        payload = command.model_dump(mode='json')
        try:
            result = await writer.execute(command, actor=actor, uow=uow)
        except ValueError as exc:
            if not rejected:
                raise
            result = {'error': str(exc)}
        else:
            assert not rejected, 'stale intent unexpectedly accepted'
        observed.append({'operation': name, 'request_bytes': size(payload), 'response_bytes': size(result)})
        return result

    try:
        card = await session.get(Card, 'c')
        assert card.status == 'in_progress'
        observed.append({'operation': 'initial_state', 'request_bytes': size({'card_id': 'c'}),
                         'response_bytes': size({'status': card.status, 'version': card.policy_version})})
        first = await call('material_event_1', progress_command(idempotency_key='event-one',
            justification='Parser implementation changed; tests still remain'))
        second = await call('material_event_2', progress_command(idempotency_key='event-two',
            justification='Boundary behavior corrected; ready to hand off'))
        assert first['id'] != second['id']
        # No timed heartbeat and no duplicate comment are required by the writer.
        assert await session.scalar(select(func.count()).select_from(Record)) == 2
        final = request()
        batch = final.batch.model_copy(update={'expected_delivery_revision': 2})
        final = final.model_copy(update={'batch': batch})
        stale = final.model_copy(update={'batch': batch.model_copy(update={'expected_card_version': 2})})
        failure = await call('stale_handoff', stale, rejected=True)
        assert 'version_conflict' in failure['error']
        assert await session.scalar(select(func.count()).select_from(Record)) == 2
        accepted = await call('reviewed_handoff', final)
        replay = await call('lost_response_exact_retry', final)
        assert replay == {**accepted, 'replayed': True}
        assert accepted['report']['accepted_state']['card_status'] == 'validation'
        assert await session.scalar(select(func.count()).select_from(Record)) == 3
        records = list((await session.scalars(select(Record))).all())
        assert {first['id'], second['id'], accepted['entries'][0]['id']} == {r.id for r in records}
        assert len((await session.get(Card, 'c')).conclusions) == 1
        metrics = {'calls': len(observed), 'request_bytes': sum(x['request_bytes'] for x in observed),
                   'response_bytes': sum(x['response_bytes'] for x in observed), 'operations': observed,
                   'durable_checkpoints': 3, 'reports': 1, 'heartbeat_calls': 0}
        record_property('task_delivery_operation_cost', json.dumps(metrics, sort_keys=True))
        assert metrics['calls'] == 6
        assert metrics['request_bytes'] + metrics['response_bytes'] < 32768
    finally:
        await session.close()
