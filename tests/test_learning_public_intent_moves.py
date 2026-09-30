"""Compound transports preserve the target conflict after rolling back the report."""
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.models.schemas import CardMove
from okto_pulse.core.ports.permission_policy import set_permission_flag
from test_learning_capture_intents import target
from test_learning_capture_writer import BOARD, runtime as _runtime
from test_learning_public_intents import intent
from test_learning_submission_writer import prepare, unit, author, independent_gates as _independent_gates

runtime = _runtime
independent_gates = _independent_gates
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('transport', ['rest', 'mcp'])
async def test_compound_stale_target_returns_current_identity_and_keeps_report_unwritten(runtime, independent_gates, monkeypatch, transport):
    factory, _, store, _ = runtime
    bug_id, draft = await prepare(runtime, 'validation')
    existing = await target(runtime)
    current = replace(existing, payload={**existing.payload, 'context': 'Later curation'}, record_fingerprint='')
    await store.append(current)
    payload = draft.model_dump()
    payload['learning_submission']['intent'] = intent(existing, 'supersede')
    data = CardMove.model_validate(payload)
    principal = author()
    set_permission_flag(principal.permissions, 'kg.query.learning_from_bugs', True)
    @asynccontextmanager
    async def units(**kwargs):
        async with factory() as session:
            try: yield unit(session)
            finally: await session.rollback()
    if transport == 'rest':
        import httpx
        from fastapi import FastAPI
        from okto_pulse.community.api.cards import router
        from okto_pulse.community.api.auth_deps import require_user
        from okto_pulse.community.api.deps import get_unit_of_work
        from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
        monkeypatch.setattr(RESTAdapterContract, 'actor', staticmethod(lambda *args, **kwargs: principal))
        app = FastAPI()
        app.include_router(router, prefix='/api/v1/cards')
        async def dependency():
            async with units() as value: yield value
        app.dependency_overrides[get_unit_of_work] = dependency
        app.dependency_overrides[require_user] = lambda: 'owner'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/v1/cards/{bug_id}/move', json=data.model_dump(mode='json'))
        assert response.status_code == 409, response.text
        error = response.json()['detail']
    else:
        from okto_pulse.core.mcp import server
        context = SimpleNamespace(agent_id='owner', agent_name='Author', permissions=principal.permissions)
        monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=context))
        monkeypatch.setattr(server, 'get_unit_of_work_factory_for_mcp', lambda: units)
        tool = (await server.mcp.get_tools())['okto_pulse_move_card']
        payload = data.model_dump(mode='json', exclude_none=True)
        payload['learning_submission'] = data.learning_submission
        error = json.loads(await tool.fn(board_id=BOARD, card_id=bug_id, **payload))
    assert error['code'] == 'learning_capture_target_changed'
    assert error['current_target']['fingerprint'] == current.record_fingerprint
    assert error['current_target']['source_revision'] == 1
    assert not any('capture_format' in row.payload for row in await store.enumerate(BOARD))
    async with factory() as session:
        bug = await session.get(Card, bug_id)
        assert bug.status.value == 'in_progress' and not bug.conclusions
