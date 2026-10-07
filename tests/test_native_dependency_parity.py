"""KG-16/24: typed dependencies converge with identical native rebuild sources."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Card, CardDependency, Spec, SpecDependency, ConsolidationQueue,
)
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_native_typed_dependencies_match_identical_rebuild_snapshot(tmp_path):
    now = datetime.now(timezone.utc)
    final_time = now + timedelta(seconds=10)
    snapshots = []
    assignments = [('a', 'card', 'pre-normal'), ('b', 'card', 'pre-bug'),
                   ('c', 'dep-normal', 'card'), ('d', 'dep-test', 'pre-normal')]

    def spec_dependency(identity, target):
        return SpecDependency(id=identity, board_id='board', dependent_spec_id='spec',
            prerequisite_spec_id=target, prerequisite_spec_ref=target, active=True,
            resolved_on_create=True, retrospective=False, introduced_at_spec_version=1,
            source_version_on_create=1, source_status_on_create='done', target_status_on_create='done',
            target_version_on_create=1, target_title_on_create=target, target_edition_on_create=1,
            add_idempotency_key='add-' + identity, add_request_digest='a' * 64,
            created_at=now, created_by_id='owner', created_by_type='user', created_by_name='Owner')

    async def add(session):
        for identity, dependent, prerequisite in assignments:
            session.add(CardDependency(id=identity, card_id=dependent, depends_on_id=prerequisite,
                created_at=now))
        session.add(spec_dependency('old-dependency', 'old'))
        await session.flush()

    async def remove(session):
        await session.execute(delete(CardDependency).where(CardDependency.id.in_(['a', 'c'])))
        await session.execute(update(SpecDependency).where(SpecDependency.id == 'old-dependency').values(
            active=False, prerequisite_spec_id=None, removed_at=final_time,
            removed_by_id='owner', removed_by_type='user', removed_by_name='Owner',
            removal_reason='Source replacement', removed_at_spec_version=1,
            remove_idempotency_key='remove-old', remove_request_digest='b' * 64))
        session.add(spec_dependency('new-dependency', 'next'))
        # Dependency triggers use wall time; compare identical final source timestamps.
        await session.execute(update(Card).where(
            Card.id.in_(["card", "dep-normal", "dep-test"])
        ).values(updated_at=final_time))


    async def seed(factory, *, final):
        async with factory() as session:
            await session.execute(update(Board).values(created_at=now))
            await session.execute(update(Spec).values(created_at=now, updated_at=now))
            await session.execute(update(Card).values(created_at=now, updated_at=now))
            for identity in ('old', 'next'):
                session.add(Spec(id=identity, board_id='board', title=identity,
                    status='done', created_by='owner', created_at=now, updated_at=now,
                    architecture_adoption=ArchitectureAdoptionScope(board_id='board', spec_id=identity,
                        adopted_in_edition=1, actor_id='owner', inherited_resource_ids=()).model_dump(mode='json')))
            for identity, kind in [('pre-normal', 'normal'), ('pre-bug', 'bug'),
                                   ('dep-normal', 'normal'), ('dep-test', 'test')]:
                session.add(Card(id=identity, board_id='board', spec_id='spec', title=identity,
                    status='done', card_type=kind, created_by='owner', test_scenario_ids=[],
                    observed_behavior='Observed', expected_behavior='Expected', steps_to_reproduce='Repeat',
                    conclusions=[{'summary': 'Completed'}], created_at=now, updated_at=now))
            await session.flush()
            if final:
                await add(session)
                await remove(session)
            await session.commit()

    async def capture(factory):
        async with factory() as session:
            path = session.bind.url.database
        snapshot = CommunityBoardSourceReader(path).fetch('board')
        assert snapshot.complete
        snapshots.append(snapshot.rows)

    async def project(factory, phase):
        for kind, identity in [('spec', 'spec'), ('card', 'card'),
                               ('card', 'dep-normal'), ('card', 'dep-test')]:
            async with factory() as session:
                session.add(ConsolidationQueue(id=f'{phase}-{identity}', board_id='board',
                    artifact_type=kind, artifact_id=identity, source='state_transition'))
                await session.commit()
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not (await session.scalars(select(ConsolidationQueue.id))).all()

    def dependencies(graph):
        return {edge: count for edge, count in relationship_set(graph).items()
                if str(edge[3]).startswith(('precedes/card_dependency/', 'precedes/spec_dependency/'))}

    async def churn(factory, graph):
        assert not dependencies(graph)
        async with factory() as session:
            await add(session)
            await session.commit()
        await project(factory, 'added')
        initial = dependencies(graph)
        assert len(initial) == 5 and all(count == 1 for count in initial.values())
        assert {(edge[4][0], edge[5][0]) for edge in initial} == {
            ('Entity', 'Entity'), ('Entity', 'Bug'), ('Bug', 'Entity'), ('Bug', 'Bug')}
        async with factory() as session:
            await remove(session)
            await session.commit()
        await project(factory, 'removed')
        assert {edge[1:3] for edge in dependencies(graph)} == {
            ('card:pre-bug', 'card:card'), ('card:pre-normal', 'card:dep-test'), ('spec:next', 'spec:spec')}
        final = relationship_set(graph)
        await project(factory, 'replayed')
        assert relationship_set(graph) == final
        await capture(factory)

    async def capture_rebuilt(factory, graph):
        await capture(factory)

    incremental = await materialize(tmp_path / 'incremental', incremental=False,
        card_type='bug', seed=lambda factory: seed(factory, final=False),
        exercise=churn, native_schema=True)
    rebuilt = await materialize(tmp_path / 'rebuilt', incremental=False,
        card_type='bug', seed=lambda factory: seed(factory, final=True),
        exercise=capture_rebuilt, native_schema=True)
    assert snapshots[0] == snapshots[1]
    assert incremental == rebuilt
    assert all(count == 1 for count in rebuilt.values())
