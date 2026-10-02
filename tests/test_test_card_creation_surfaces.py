"""BASE T20: actual public handlers reject foreign scenario sets atomically."""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec, Agent, AgentBoard, DomainEventRow, ActivityLog
from okto_pulse.community.adapters.sqlalchemy_database import build_community_engine, build_community_session_factory
from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_spec_resource_propagation import CommunitySqlAlchemySpecResourcePropagationStore
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.boards import router
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.mcp import server
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port, register_knowledge_mutation_audit_sink
from okto_pulse.core.ports.spec_resource_propagation import register_spec_resource_propagation_store
from okto_pulse.core.services.main import AgentService
from test_delivery_reused_impact import register_report_adapters
from test_knowledge_propagation_parent_adapter import _native_spec


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['rest', 'mcp'])
@pytest.mark.parametrize('case', ['valid', 'foreign_spec', 'foreign_board', 'denied'])
@pytest.mark.parametrize('knowledge_v2', [False, True])
async def test_public_create_rejects_foreign_scenarios_without_partial_writes(tmp_path, monkeypatch, transport, case, knowledge_v2):
    engine = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'surfaces.db'}")
    sessions = build_community_session_factory(engine)
    register_report_adapters()
    register_spec_resource_propagation_store(CommunitySqlAlchemySpecResourcePropagationStore())
    factory = CommunityUnitOfWorkFactory(sessions)
    store = CommunitySqlAlchemyKnowledgePropagationStore(sessions)
    register_knowledge_propagation_port(store)
    register_knowledge_mutation_audit_sink(store)
    permissions = ['card.entity.create_test'] if case != 'denied' else ['board.read']
    principal = Principal('owner', realm_id='local', actor_kind='agent', claims={'permissions': permissions})
    context = SimpleNamespace(agent_id='owner', agent_name='Author', board_id='board', realm_id='local', permissions=permissions)
    try:
        await initialize_current_schema(engine, current_schema_contract())
        async with sessions() as db:
            db.add_all([Board(id='board', realm_id='local', name='Board', owner_id='owner'),
                        Board(id='foreign', realm_id='local', name='Other', owner_id='other')])
            db.add(Agent(id='owner', name='Author', api_key='fixture', api_key_hash=AgentService.hash_api_key('fixture'),
                         created_by='owner', permissions=permissions))
            db.add(AgentBoard(id='grant', board_id='board', agent_id='owner', granted_by='owner'))
            db.add(_native_spec(id='spec', board_id='board', title='Spec', created_by='owner', status='approved',
                        test_scenarios=[{'id': 'one', 'title': 'One', 'status': 'draft', 'linked_task_ids': []}]))
            db.add(_native_spec(id='other', board_id='foreign' if case == 'foreign_board' else 'board',
                        title='Other', created_by='owner',
                        test_scenarios=[{'id': 'alien', 'title': 'Alien', 'linked_task_ids': []}]))
            await db.commit()
        data = dict(title='Public Test Card', spec_id='spec', card_type='test',
                    test_scenario_ids=['one', 'alien'] if case.startswith('foreign') else ['one'])
        if knowledge_v2:
            data['knowledge_propagation'] = dict(contract_version=2, selection_state='omitted',
                                                  knowledge_ids=[], idempotency_key='create-test-card')
        if transport == 'rest':
            app = FastAPI()
            app.state.runtime_composition = SimpleNamespace(uow_factory=factory)
            app.include_router(router, prefix='/boards')
            app.dependency_overrides[require_principal] = lambda: principal

            async def unit():
                actor = RESTAdapterContract.actor_from_principal(principal, board_id='board')
                async with factory(actor=actor) as uow:
                    yield uow

            app.dependency_overrides[get_unit_of_work] = unit
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/boards/board/cards', json=data)
            assert response.status_code == (201 if case == 'valid' else 403 if case == 'denied' else 400), response.text
            if case.startswith('foreign'):
                assert 'not found in spec' in response.text
        else:
            async def actor(board_id):
                assert board_id == 'board'
                return context

            monkeypatch.setattr(server, '_get_agent_ctx', actor)
            monkeypatch.setattr(server, 'get_unit_of_work_factory_for_mcp', lambda: factory)
            response = json.loads(await server.okto_pulse_create_card.fn(board_id='board', **data))
            if case == 'valid':
                assert 'error' not in response, response
            elif case == 'denied':
                assert 'permission' in json.dumps(response).lower(), response
            else:
                assert 'not found in spec' in response['error'], response
        async with sessions() as db:
            cards = (await db.scalars(select(Card))).all()
            spec = await db.get(Spec, 'spec')
            if case == 'valid':
                assert len(cards) == 1
                assert cards[0].test_scenario_ids == ['one']
                assert spec.test_scenarios[0]['linked_task_ids'] == [cards[0].id]
            else:
                assert cards == []
                assert spec.test_scenarios[0]['linked_task_ids'] == []
                assert (await db.scalars(select(DomainEventRow))).all() == []
                assert (await db.scalars(select(ActivityLog))).all() == []
            assert (await db.get(Spec, 'other')).test_scenarios[0]['linked_task_ids'] == []
    finally:
        await engine.dispose()
