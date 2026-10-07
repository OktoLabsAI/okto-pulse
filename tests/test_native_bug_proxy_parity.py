"""KG-24: Bug origin proxies retain inferred provenance through rebuild."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.ports.card_projection import BUG_ORIGIN_PROXY_FAMILIES
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec, ConsolidationQueue
from test_projection_materialized_parity import materialize, relationship_set, source


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_bug_proxy_retraction_matches_identical_native_snapshot(tmp_path):
    now = datetime.now(timezone.utc)
    final_time = now + timedelta(seconds=10)
    snapshots = []

    def content(phase):
        values = deepcopy(source(['ac_two']))
        values['technical_requirements'] = [{'id': 'tr_one', 'text': 'Bounded response'}]
        for index, family in enumerate(BUG_ORIGIN_PROXY_FAMILIES):
            values[family.field][0]['linked_task_ids'] = (
                ['card'] if phase == 'all' or (phase == 'final' and index % 2 == 0) else [])
        return values

    async def seed(factory, *, final):
        async with factory() as session:
            await session.execute(update(Board).values(created_at=now))
            await session.execute(update(Spec).values(**content('final' if final else 'all'),
                created_at=now, updated_at=final_time if final else now))
            await session.execute(update(Card).values(created_at=now, updated_at=now))
            for identity in ('bug-first', 'bug-second'):
                session.add(Card(id=identity, board_id='board', spec_id='spec', title=identity,
                    status='done', card_type='bug', origin_task_id='card', created_by='owner',
                    test_scenario_ids=[], observed_behavior='Observed', expected_behavior='Expected',
                    steps_to_reproduce='Repeat', conclusions=[{'summary': 'Completed'}],
                    created_at=now, updated_at=now))
            await session.commit()

    def proxies(graph):
        return {edge: count for edge, count in relationship_set(graph).items()
                if str(edge[3]).startswith('violates/bug_origin_proxy_')}

    async def capture(factory):
        async with factory() as session:
            path = session.bind.url.database
        snapshot = CommunityBoardSourceReader(path).fetch('board')
        assert snapshot.complete
        snapshots.append(snapshot.rows)

    async def project(factory, phase):
        for kind, identity in [('spec', 'spec'), ('card', 'card'),
                               ('card', 'bug-first'), ('card', 'bug-second')]:
            async with factory() as session:
                session.add(ConsolidationQueue(id=f'{phase}-{identity}', board_id='board',
                    artifact_type=kind, artifact_id=identity, source='state_transition'))
                await session.commit()
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not (await session.scalars(select(ConsolidationQueue.id))).all()

    async def churn(factory, graph):
        initial = proxies(graph)
        assert len(initial) == len(BUG_ORIGIN_PROXY_FAMILIES) * 2
        assert all(count == 1 for count in initial.values())
        assert all(edge[-2:] == ('inferred_origin_proxy:card:card', 0.8) for edge in initial)
        for phase in ('empty', 'final'):
            async with factory() as session:
                await session.execute(update(Spec).values(**content(phase), updated_at=final_time))
                await session.commit()
            await project(factory, phase)
            expected = {(f'card:{bug}', f'spec:spec:{family.section}:{content(phase)[family.field][0]["id"]}')
                for index, family in enumerate(BUG_ORIGIN_PROXY_FAMILIES)
                if phase == 'final' and index % 2 == 0 for bug in ('bug-first', 'bug-second')}
            assert {edge[1:3] for edge in proxies(graph)} == expected
        final = relationship_set(graph)
        await project(factory, 'replayed')
        assert relationship_set(graph) == final
        await capture(factory)

    async def capture_rebuilt(factory, graph):
        await capture(factory)

    incremental = await materialize(tmp_path / 'incremental', incremental=False,
        card_type='normal', seed=lambda factory: seed(factory, final=False),
        exercise=churn, native_schema=True)
    rebuilt = await materialize(tmp_path / 'rebuilt', incremental=False,
        card_type='normal', seed=lambda factory: seed(factory, final=True),
        exercise=capture_rebuilt, native_schema=True)
    assert snapshots[0] == snapshots[1]
    assert incremental == rebuilt
    assert all(count == 1 for count in rebuilt.values())
