"""BASE T19/T20 through production SQLite persistence and event adapters."""
from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec, DomainEventRow, ActivityLog
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_spec_resource_propagation import CommunitySqlAlchemySpecResourcePropagationStore
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import CardCreate
from okto_pulse.core.ports.spec_resource_propagation import register_spec_resource_propagation_store
from okto_pulse.core.services.main import CardService
from test_delivery_reused_impact import register_report_adapters


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['commit', 'rollback', 'foreign_spec', 'foreign_board'])
async def test_card_creation_and_bidirectional_scenario_links_are_atomic(tmp_path, outcome):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'creation.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False,
        sync_session_class=CommunitySemanticSession, info={'realm_scope': RealmScope.local()})
    register_report_adapters()
    register_spec_resource_propagation_store(CommunitySqlAlchemySpecResourcePropagationStore())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        scenarios = [{'id': identity, 'title': identity, 'status': 'draft', 'linked_task_ids': []}
                     for identity in ('one', 'two')]
        async with sessions() as db:
            db.add_all([Board(id='board', realm_id='local', name='Board', owner_id='owner'),
                        Board(id='foreign', realm_id='local', name='Other', owner_id='other')])
            db.add(Spec(id='spec', board_id='board', title='Spec', created_by='owner',
                        status='approved', test_scenarios=deepcopy(scenarios)))
            db.add(Spec(id='other', board_id='foreign' if outcome == 'foreign_board' else 'board',
                        title='Other Spec', created_by='owner',
                        test_scenarios=[{'id': 'alien', 'title': 'Alien', 'linked_task_ids': []}]))
            await db.commit()
        async with sessions() as db:
            request = CardCreate(title='Created test', spec_id='spec', card_type='test',
                test_scenario_ids=['one', 'alien'] if outcome.startswith('foreign') else ['one', 'two'])
            if outcome.startswith('foreign'):
                with pytest.raises(ValueError, match='not found in spec'):
                    await CardService(db).create_card('board', 'owner', request)
                await db.commit()
            else:
                card = await CardService(db).create_card('board', 'owner', request)
                identity = card.id
                if outcome == 'rollback':
                    await db.rollback()
                else:
                    await db.commit()
        async with sessions() as db:
            cards = (await db.scalars(select(Card))).all()
            spec = await db.get(Spec, 'spec')
            if outcome == 'commit':
                assert len(cards) == 1 and cards[0].id == identity
                assert cards[0].test_scenario_ids == ['one', 'two']
                assert all(item['linked_task_ids'] == [identity] for item in spec.test_scenarios)
                assert (await db.scalars(select(DomainEventRow))).all()
                assert (await db.scalars(select(ActivityLog))).all()
            else:
                assert cards == []
                assert spec.test_scenarios == scenarios
                assert (await db.scalars(select(DomainEventRow))).all() == []
                assert (await db.scalars(select(ActivityLog))).all() == []
            assert (await db.get(Spec, 'other')).test_scenarios[0]['linked_task_ids'] == []
    finally:
        await engine.dispose()
