"""KG-24: native append-only Amendment lineage converges with clean rebuild.

Domain association/lifecycle services and the real store are exercised here;
transport authorization is covered separately, not bypassed by this projection test.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.ports.amendment_revision import (
    register_amendment_revision_store, reset_amendment_revision_store_for_tests,
)
from okto_pulse.core.services.amendment_revision import AmendmentRevisionService
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_amendment_revision import CommunitySqlAlchemyAmendmentRevisionStore
from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision, Board, Card, ConsolidationQueue, Spec,
)
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_append_only_amendment_matches_rebuild_of_identical_native_snapshot(tmp_path):
    now = datetime.now(timezone.utc)
    final_time = now + timedelta(seconds=10)
    snapshots = []
    register_amendment_revision_store(CommunitySqlAlchemyAmendmentRevisionStore())

    async def seed(factory, *, final):
        async with factory() as session:
            await session.execute(update(Board).values(created_at=now))
            await session.execute(update(Spec).values(created_at=now, updated_at=now))
            await session.execute(update(Card).values(created_at=now, updated_at=now))
            for identity in ('test-a', 'test-b'):
                session.add(Card(id=identity, board_id='board', spec_id='spec',
                    card_type='test', title=identity, status='done', created_by='owner',
                    conclusions=[{'summary': 'Regression recorded'}],
                    created_at=now, updated_at=now))
            session.add(AmendmentHotfixRevision(
                id='amendment-one', board_id='board', original_spec_id='spec',
                origin_bug_id='card', created_by='owner', origin_task_ids=[],
                affected_task_ids=[], regression_scenario_ids=['ts_one'],
                regression_test_task_ids=['test-a', 'test-b'] if final else ['test-a'],
                automated_regression_refs=[], status='done' if final else 'draft',
                lineage_state='complete' if final else 'incomplete',
                created_at=now, updated_at=final_time if final else now))
            await session.commit()

    async def capture(factory):
        async with factory() as session:
            path = session.bind.url.database
        snapshot = CommunityBoardSourceReader(path).fetch('board')
        assert snapshot.complete
        snapshots.append(snapshot.rows)

    def owned(graph):
        return {edge: count for edge, count in relationship_set(graph).items()
                if edge[1] == 'amendment_hotfix_revision:amendment-one'}

    async def project(factory, identity):
        async with factory() as session:
            session.add(ConsolidationQueue(id=identity, board_id='board',
                artifact_type='amendment_hotfix_revision', artifact_id='amendment-one',
                source='state_transition'))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not (await session.scalars(select(ConsolidationQueue.id))).all()

    async def churn(factory, graph):
        initial = owned(graph)
        assert len(initial) == 4 and all(count == 1 for count in initial.values())
        assert all(edge[4][1] == 'working' for edge in initial)
        spec_before = {edge: count for edge, count in relationship_set(graph).items()
                       if edge[1] != 'amendment_hotfix_revision:amendment-one'}
        async with factory() as session:
            service = AmendmentRevisionService(session)
            for incoming in (['test-b'], ['test-b'], []):
                result = await service.associate_artifacts('amendment-one',
                    regression_test_task_ids=incoming, actor='owner')
                assert result.regression_test_task_ids == ['test-a', 'test-b']
            await service.set_lineage_state('amendment-one', 'complete', 'owner')
            await service.set_status('amendment-one', 'done', 'owner')
            await session.execute(update(AmendmentHotfixRevision).values(updated_at=final_time))
            await session.commit()
        await project(factory, 'amendment-change')
        final = owned(graph)
        assert len(final) == 5 and all(count == 1 for count in final.values())
        assert all(edge[4][1] == 'canonical' for edge in final)
        assert {edge[2] for edge in final if edge[3] ==
                'belongs_to/amendment_to_regression_test_task@v2.0'} == {'card:test-a', 'card:test-b'}
        assert not any(edge[0].startswith('supersedes') for edge in final)
        assert {edge: count for edge, count in relationship_set(graph).items()
                if edge[1] != 'amendment_hotfix_revision:amendment-one'} == spec_before
        await project(factory, 'amendment-replay')
        assert owned(graph) == final
        await capture(factory)

    async def capture_rebuilt(factory, graph):
        await capture(factory)

    try:
        incremental = await materialize(tmp_path / 'incremental', incremental=False,
            card_type='bug', seed=lambda factory: seed(factory, final=False),
            exercise=churn, native_schema=True)
        rebuilt = await materialize(tmp_path / 'rebuilt', incremental=False,
            card_type='bug', seed=lambda factory: seed(factory, final=True),
            exercise=capture_rebuilt, native_schema=True)
        assert snapshots[0] == snapshots[1]
        assert incremental == rebuilt
        assert all(count == 1 for count in rebuilt.values())
    finally:
        reset_amendment_revision_store_for_tests()
