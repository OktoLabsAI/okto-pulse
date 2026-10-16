"""KG-26: a valid unprojected endpoint converges through the private queue."""

import asyncio
import time

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, ConsolidationAudit, ConsolidationQueue, IdeationQAItem,
    QAItem, RefinementQAItem, Spec, SpecQAItem,
)
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_native_endpoint_wait_creates_no_user_work_and_retries_without_queue_edits(tmp_path):
    async def exercise(factory, graph):
        async def user_work():
            async with factory() as session:
                counts = tuple([await session.scalar(select(func.count()).select_from(model))
                                for model in (Card, QAItem, SpecQAItem, RefinementQAItem, IdeationQAItem)])
                return counts, (await session.get(Card, 'card')).status, (await session.get(Spec, 'spec')).status

        unchanged = await user_work()
        async with factory() as session:
            audit_count = await session.scalar(select(func.count()).select_from(ConsolidationAudit))
            spec = await session.get(Spec, 'spec')
            card = await session.get(Card, 'card')
            spec.test_scenarios = [{'id': 'next', 'title': 'Next scenario',
                                    'linked_criteria': ['ac_two'], 'linked_task_ids': ['card']}]
            card.test_scenario_ids = ['next']
            session.add(ConsolidationQueue(id='card-awaits-endpoint', board_id='board',
                artifact_type='card', artifact_id='card', source='state_transition'))
            await session.commit()
        processor = ConsolidationProcessor(relational_scope_factory=factory)
        assert await processor.process_batch() == 0
        async with factory() as session:
            pending = await session.get(ConsolidationQueue, 'card-awaits-endpoint')
            assert pending.status == 'pending'
            assert pending.attempts == 0
            assert pending.last_error.startswith('relational_projection_endpoint_pending:')
            assert pending.next_retry_at is not None
            assert await session.scalar(select(func.count()).select_from(ConsolidationAudit)) == audit_count
        assert await user_work() == unchanged
        assert not any(edge[1:3] == ('card:card', 'spec:spec:test_scenario:ts_one')
                       for edge in relationship_set(graph))
        async with factory() as session:
            session.add(ConsolidationQueue(id='project-valid-endpoint', board_id='board',
                artifact_type='spec', artifact_id='spec', source='state_transition'))
            await session.commit()
        deadline = time.monotonic() + 60
        while True:
            await processor.process_batch()
            async with factory() as session:
                remaining = await session.scalar(select(func.count()).select_from(ConsolidationQueue))
            if remaining == 0:
                break
            assert time.monotonic() < deadline, 'Normal retry did not converge.'
            await asyncio.sleep(0.05)
        assert await user_work() == unchanged
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(ConsolidationAudit)) == audit_count + 2
        assert any(edge[1:3] == ('card:card', 'spec:spec:test_scenario:next')
                   for edge in relationship_set(graph))

    await materialize(tmp_path / 'endpoint', incremental=False, card_type='normal',
                      native_schema=True, exercise=exercise)
