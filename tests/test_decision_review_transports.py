"""REST/MCP parity and authority through the shared use case and native SQL."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, func

from test_decision_reviews import reviews as reviews_fixture, command
from test_code_traceability_rest import _projection_rest_app
from okto_pulse.community.api import code_traceability as api
from okto_pulse.community.adapters.sqlalchemy_models import Board, DeliveryEvidenceRecordRow as Record
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.mcp.code_traceability_tools import register_code_traceability_tools

reviews = reviews_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['rest', 'mcp'])
@pytest.mark.parametrize('authorized', [False, True])
async def test_shared_authority_cannot_be_borrowed_from_decision_edit(reviews, transport, authorized):
    session, store = reviews
    cmd = await command(store)
    board = await session.get(Board, 'board')
    board.owner_id = 'reviewer'  # HTTP authenticated owner; not the Decision author.
    await session.commit()
    permissions = ['spec.entity.read', 'code_traceability.evidence.read', 'spec.structured_entity.decision.update']
    if authorized:
        permissions += ['spec.validation.submit', 'spec.interact_in.in_progress']
    uow = CommunityUnitOfWork(session, realm_scope=RealmScope.local())
    from test_delivery_reused_impact import register_report_adapters
    register_report_adapters()
    body = cmd.model_dump(mode='json', exclude={'board_id', 'spec_id'})
    if transport == 'rest':
        app = _projection_rest_app(uow)
        app.dependency_overrides[api.require_principal] = lambda: Principal(subject='reviewer', realm_id='local',
            actor_kind='agent', claims={'permissions': permissions})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/boards/board/specs/spec/decision-reviews', json=body)
        accepted, data = response.status_code == 200, response.json()
    else:
        @asynccontextmanager
        async def scope(**kwargs):
            yield uow
        async def agent(board_id):
            return SimpleNamespace(agent_id='reviewer', agent_name='Reviewer', board_id=board_id,
                realm_id='local', permissions=permissions)
        catalog = CoreMcpCatalog(name='decision-review-test', version='1')
        register_code_traceability_tools(catalog, get_board_agent=agent, get_uow=lambda: scope, get_settings=SimpleNamespace)
        tool = await catalog.get_tool('okto_pulse_record_decision_reviews')
        result = await tool.fn(board_id='board', spec_id='spec', review=body)
        accepted, data = not result.is_error, result
    assert accepted is authorized, data
    assert await session.scalar(select(func.count()).select_from(Record)) == int(authorized)
