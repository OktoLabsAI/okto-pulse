"""KG-16/24: all Card child families retract and rebuild from identical sources."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.ports.card_projection import CARD_CHILD_FAMILIES, CARD_SCENARIO_RULES
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec, ConsolidationQueue
from test_projection_materialized_parity import materialize, relationship_set, source


@pytest.mark.asyncio
@pytest.mark.timeout(480)
@pytest.mark.parametrize('card_type', ['normal', 'test', 'bug'])
async def test_all_card_child_families_match_identical_native_snapshot(tmp_path, card_type):
    now = datetime.now(timezone.utc)
    final_time = now + timedelta(seconds=10)
    snapshots = []
    rules = {family.rule for family in CARD_CHILD_FAMILIES} | CARD_SCENARIO_RULES

    def content(phase):
        values = deepcopy(source(['ac_two']))
        values['technical_requirements'] = [{'id': 'tr_one', 'text': 'Bounded response'}]
        for index, family in enumerate(CARD_CHILD_FAMILIES):
            linked = phase == 'all' or (phase == 'final' and index % 2 == 0)
            values[family.field] = [{**item, 'linked_task_ids': ['card'] if linked else []}
                                    for item in values[family.field]]
        values['test_scenarios'][0]['linked_task_ids'] = ['card'] if phase == 'all' else []
        return values

    async def seed(factory, *, final):
        async with factory() as session:
            await session.execute(update(Board).values(created_at=now))
            await session.execute(update(Spec).values(**content('final' if final else 'all'),
                created_at=now, updated_at=final_time if final else now))
            await session.execute(update(Card).values(created_at=now,
                updated_at=final_time if final else now,
                test_scenario_ids=[] if final else ['ts_one']))
            await session.commit()

    async def capture(factory):
        async with factory() as session:
            path = session.bind.url.database
        snapshot = CommunityBoardSourceReader(path).fetch('board')
        assert snapshot.complete
        snapshots.append(snapshot.rows)

    async def project(factory, phase):
        for kind, identity in [('spec', 'spec'), ('card', 'card')]:
            async with factory() as session:
                session.add(ConsolidationQueue(id=f'{phase}-{kind}', board_id='board',
                    artifact_type=kind, artifact_id=identity, source='state_transition'))
                await session.commit()
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not (await session.scalars(select(ConsolidationQueue.id))).all()

    async def churn(factory, graph):
        owned = {edge: count for edge, count in relationship_set(graph).items() if edge[3] in rules}
        expected_endpoints = {(family.target_type, f'spec:spec:{family.section}:{item["id"]}')
            for family in CARD_CHILD_FAMILIES for item in content('all')[family.field]}
        expected_endpoints.add(('TestScenario', 'spec:spec:test_scenario:ts_one'))
        assert {(edge[5][0], edge[2]) for edge in owned} == expected_endpoints
        assert len(owned) == len(expected_endpoints) and all(count == 1 for count in owned.values())
        assert {edge[3] for edge in owned} == {family.rule for family in CARD_CHILD_FAMILIES} | {
            'supports/card_scenario_observed_reciprocal@v2.1'}
        for phase in ('empty', 'final'):
            async with factory() as session:
                await session.execute(update(Spec).values(**content(phase), updated_at=final_time))
                await session.execute(update(Card).values(test_scenario_ids=[], updated_at=final_time))
                await session.commit()
            await project(factory, phase)
            actual = {edge[3] for edge in relationship_set(graph) if edge[3] in rules}
            expected = {family.rule for index, family in enumerate(CARD_CHILD_FAMILIES)
                        if phase == 'final' and index % 2 == 0}
            assert actual == expected
        before = relationship_set(graph)
        await project(factory, 'replay')
        assert relationship_set(graph) == before
        await capture(factory)

    async def capture_rebuilt(factory, graph):
        await capture(factory)

    incremental = await materialize(tmp_path / 'incremental', incremental=False,
        card_type=card_type, seed=lambda factory: seed(factory, final=False),
        exercise=churn, native_schema=True)
    rebuilt = await materialize(tmp_path / 'rebuilt', incremental=False,
        card_type=card_type, seed=lambda factory: seed(factory, final=True),
        exercise=capture_rebuilt, native_schema=True)
    assert snapshots[0] == snapshots[1]
    assert incremental == rebuilt
    assert all(count == 1 for count in rebuilt.values())
