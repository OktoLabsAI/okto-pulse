"""Transport error paths retain the assessed gate and a bounded readiness query."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import test_delivery_evidence_integration as delivery
from okto_pulse.community.api import cards, specs, code_traceability
from okto_pulse.core.application import use_cases
from okto_pulse.core.domain.delivery_assessment import assess_delivery_gate, assess_obligation_delivery
from okto_pulse.core.domain.delivery_evidence import DeliveryScope
from okto_pulse.core.mcp import server
from okto_pulse.core.mcp.code_traceability_tools import _error_outcome
from okto_pulse.core.models.schemas import CardMove, SpecMove
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError
from okto_pulse.core.application.delivery_errors import DeliveryGateBlocked

ledger = delivery.ledger


@pytest.mark.parametrize('code,status', [('readiness_query_invalid', 422), ('readiness_scope_mismatch', 404),
    ('readiness_cursor_stale', 409), ('readiness_snapshot_changed', 409),
    ('readiness_limit_exceeded', 503), ('readiness_assessment_unavailable', 503)])
def test_read_error_http_classification_preserves_the_mcp_code_and_native_reason(code, status):
    error = ReadinessReadError(code, native_code='native_context_unavailable')
    rest, mcp = code_traceability._http_error(error), _error_outcome(error)
    assert rest.status_code == status and rest.detail['code'] == mcp.code == code
    assert rest.detail['retryable'] == mcp.retryable
    assert rest.detail['details']['native_code'] == mcp.details['native_code']


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
@pytest.mark.parametrize('entity', ['spec', 'card'])
async def test_existing_move_routes_and_tools_publish_compact_delivery_error(ledger, monkeypatch, entity):
    _, store, _ = ledger
    facts = await store.load_snapshot(DeliveryScope(delivery.BOARD_ID, delivery.SPEC_ID, 1))
    assessed = assess_obligation_delivery(facts, **({'card_id': 'task', 'card_type': 'normal'} if entity == 'card' else {}))
    error = DeliveryGateBlocked(assessed, assess_delivery_gate(assessed))
    assert not error.gate.allowed
    # Isolate boundary projection; real native closeout and rollback run in
    # test_delivery_readiness_closeout and the canonical report integration.
    rest_case = cards.MoveCardUseCase if entity == 'card' else specs.MoveSpecUseCase
    mcp_case = use_cases.McpMoveCardUseCase if entity == 'card' else use_cases.McpMoveSpecUseCase
    monkeypatch.setattr(rest_case, 'execute', AsyncMock(side_effect=error))
    monkeypatch.setattr(mcp_case, 'execute', AsyncMock(side_effect=error))
    uow = SimpleNamespace()
    report = dict(status='done', conclusion='Implementation report for this contribution', completeness=100,
        completeness_justification='Assigned contribution implemented', drift=0, drift_justification='Scope unchanged')
    with pytest.raises(HTTPException) as caught:
        if entity == 'card':
            await cards.move_card('task', CardMove(**report), user_id='owner', uow=uow)
        else:
            await specs.move_spec(delivery.SPEC_ID, SpecMove(status='done'), user_id='owner', uow=uow)
    assert caught.value.status_code == 409
    assert caught.value.detail == error.to_error_dict()

    @asynccontextmanager
    async def scope(**kwargs):
        yield uow

    monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=SimpleNamespace(
        agent_id='agent', agent_name='Agent', realm_id='local', permissions=())))
    monkeypatch.setattr(server, 'get_unit_of_work_factory_for_mcp', lambda: scope)
    tool = await server.mcp.get_tool(f'okto_pulse_move_{entity}')
    payload = json.loads(await tool.fn(board_id=delivery.BOARD_ID, **(
        {'card_id': 'task', **report} if entity == 'card' else {'spec_id': delivery.SPEC_ID, 'status': 'done'})))
    assert payload == {'error': error.code, **caught.value.detail}
    assert len(json.dumps(payload).encode()) < 8192
