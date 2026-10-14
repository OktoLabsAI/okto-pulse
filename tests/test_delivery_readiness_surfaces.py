"""P07 REST/MCP parity through the composed service and native SQL reader."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import ACTOR, setup
from test_delivery_readiness_batch import statements
from test_code_traceability_rest import _projection_rest_app
from okto_pulse.community.api import code_traceability as api
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityReadinessClock
from okto_pulse.community.adapters.sqlalchemy_models import AgentBoard
from okto_pulse.core.application.service_catalog import build_application_service_catalog
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.mcp.code_traceability_tools import register_code_traceability_tools
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.permission_policy import registered_permission_flags

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


async def surfaces(session, monkeypatch):
    await setup(session)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(CommunityReadinessClock, 'now', lambda self: now)
    monkeypatch.setattr('okto_pulse.core.get_settings', lambda: SimpleNamespace(
        guideline_policy_cursor_signing_key='readiness-test-installation-key-000000000'))
    uow = SimpleNamespace(services=build_application_service_catalog(session), commit=AsyncMock())
    rest = _projection_rest_app(uow)
    rest.dependency_overrides[api.require_principal] = lambda: Principal(
        subject=ACTOR.actor_id, realm_id='local', actor_kind='agent',
        claims={'permissions': registered_permission_flags()})

    @asynccontextmanager
    async def scope(**kwargs):
        yield uow

    async def agent(board_id):
        return SimpleNamespace(agent_id=ACTOR.actor_id, agent_name='Readiness agent',
            realm_id='local', board_id=board_id, permissions=registered_permission_flags())

    catalog = CoreMcpCatalog(name='readiness', version='1')
    register_code_traceability_tools(catalog, get_board_agent=agent, get_uow=lambda: scope, get_settings=SimpleNamespace)
    tool = await catalog.get_tool('okto_pulse_get_delivery_evidence')
    return rest, tool, uow


def url():
    return f'/boards/{delivery.BOARD_ID}/specs/{delivery.SPEC_ID}/delivery-evidence'


@pytest.mark.parametrize('card_id', [None, 'task', 'test'])
async def test_S01_S02_S04_S11_composed_surfaces_share_metrics_details_and_significant_nulls(ledger, monkeypatch, card_id):
    session, store, _ = ledger
    rest, tool, uow = await surfaces(session, monkeypatch)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    query = {'view': 'readiness', **({'card_id': card_id} if card_id else {})}
    with statements(session.bind) as sql:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
            response = await client.get(url(), params=query)
        result = await tool.fn(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, **query)
    assert response.status_code == 200, response.text
    assert not result.is_error, result
    assert response.headers['cache-control'] == 'no-store'
    assert result.payload == response.json()
    payload = result.payload
    assert payload['scope']['card_id'] == card_id
    assert payload['implementation']['percent'] == (None if card_id == 'test' else 100)
    assert payload['verification']['percent'] == 100
    assert 'next_cursor' in payload['page']
    assert 'other_blocker_count' in payload['lifecycle']
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert not session.new and not session.dirty and not session.deleted
    uow.commit.assert_not_awaited()


@pytest.mark.parametrize('patch', [{'record_id': 'record'}, {'edition': 1}, {'issue_id': 'issue'},
    {'view': 'ledger', 'phase': 'verification'}])
async def test_S05_query_errors_are_structured_on_both_surfaces(ledger, monkeypatch, patch):
    session, _, _ = ledger
    rest, tool, _ = await surfaces(session, monkeypatch)
    query = {'view': 'readiness', **patch}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
        response = await client.get(url(), params=query)
    result = await tool.fn(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, **query)
    assert response.status_code == 422, response.text
    assert result.is_error and result.code == response.json()['detail']['code'] == 'readiness_query_invalid'


async def test_S06_S07_S09_pages_and_detail_keep_metrics_and_refuse_changed_snapshot(ledger, monkeypatch):
    session, store, _ = ledger
    rest, tool, _ = await surfaces(session, monkeypatch)
    query = dict(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, view='readiness', limit=1)
    first = await tool.fn(**query)
    assert not first.is_error, first
    page = first.payload
    assert page['page']['has_more'] and page['issues']
    detail_query = page['issues'][0]['detail_query']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
        detail = await client.get(url(), params={k: v for k, v in detail_query.items()
            if k not in {'board_id', 'spec_id'} and v is not None})
    assert detail.status_code == 200, detail.text
    mcp_detail = await tool.fn(**detail_query)
    assert not mcp_detail.is_error and mcp_detail.payload == detail.json()
    second = await tool.fn(**query, cursor=page['page']['next_cursor'])
    assert not second.is_error, second
    for field in ('implementation', 'verification', 'snapshot_id', 'summary'):
        assert second.payload[field] == page[field]
    await delivery.record(store, delivery.command())
    await session.commit()
    stale = await tool.fn(**query, cursor=page['page']['next_cursor'])
    assert stale.is_error and stale.code == 'readiness_cursor_stale'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
        response = await client.get(url(), params={'view': 'readiness', 'snapshot_id': page['snapshot_id']})
    assert response.status_code == 409 and response.json()['detail']['code'] == stale.code


async def test_scope_error_is_not_empty_success(ledger, monkeypatch):
    session, _, _ = ledger
    rest, tool, _ = await surfaces(session, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
        response = await client.get(url(), params={'view': 'readiness', 'card_id': 'foreign'})
    result = await tool.fn(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, view='readiness', card_id='foreign')
    assert response.status_code == 404, response.text
    assert result.is_error and result.code == response.json()['detail']['code'] == 'readiness_scope_mismatch'


async def test_S12_current_denial_overrides_old_authenticated_permission_claims(ledger, monkeypatch):
    session, _, _ = ledger
    rest, tool, _ = await surfaces(session, monkeypatch)
    flags = registered_permission_flags()
    flags['code_traceability']['evidence']['read'] = False
    await session.execute(update(AgentBoard).where(AgentBoard.agent_id == ACTOR.actor_id).values(permission_overrides=flags))
    await session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
        response = await client.get(url(), params={'view': 'readiness'})
    result = await tool.fn(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, view='readiness')
    assert response.status_code == 403, response.text
    assert result.is_error and result.code == response.json()['detail']['code'] == 'readiness_permission_denied'


async def test_readiness_rest_query_rejects_unknown_fields(ledger, monkeypatch):
    session, _, _ = ledger
    rest, _, _ = await surfaces(session, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
        response = await client.get(url(), params={'view': 'readiness', 'verified': 'true'})
    assert response.status_code == 422 and response.json()['detail']['code'] == 'readiness_query_invalid'


async def test_P08_board_summary_matches_authorized_native_details_without_writes(ledger, monkeypatch):
    session, store, _ = ledger
    rest, tool, uow = await surfaces(session, monkeypatch)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    with statements(session.bind) as sql:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
            response = await client.get(url().replace('/delivery-evidence', '/delivery-readiness-summary'))
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    summary = response.json()
    assert summary['contract_version'] == 'delivery-readiness/v1'
    assert summary['metrics_version'] == 2
    contexts = [summary['delivery_readiness'], *[row['delivery_readiness'] for row in summary['cards']]]
    assert {row['card_id'] for row in summary['cards']} == {'task', 'test'}
    for context in contexts:
        result = await tool.fn(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
            view='readiness', **({'card_id': context['scope']['card_id']} if context['scope']['card_id'] else {}))
        assert not result.is_error, result
        for field in ('scope', 'implementation', 'verification', 'proof_complete'):
            assert context[field] == result.payload[field]
        # A dedicated Card read also assesses that Card's native lifecycle gates.
        # Its snapshot includes that scoped gate assessment; board projections
        # intentionally share the Spec batch and do not run N Card gate queries.
        if context['scope']['card_id'] is None:
            assert context['snapshot_id'] == result.payload['snapshot_id']
        assert context['snapshot_id'] == summary['delivery_readiness']['snapshot_id']
    test_context = next(row['delivery_readiness'] for row in summary['cards'] if row['card_id'] == 'test')
    assert test_context['implementation']['percent'] is None
    assert all(statement.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for statement in sql)
    assert not session.new and not session.dirty and not session.deleted
    uow.commit.assert_not_awaited()


@pytest.mark.parametrize('permission', ['evidence', 'card'])
async def test_P08_summary_reauthorizes_current_access(ledger, monkeypatch, permission):
    session, _, _ = ledger
    rest, _, uow = await surfaces(session, monkeypatch)
    flags = registered_permission_flags()
    if permission == 'evidence':
        flags['code_traceability']['evidence']['read'] = False
    else:
        flags['card']['entity']['read'] = False
    await session.execute(update(AgentBoard).where(AgentBoard.agent_id == ACTOR.actor_id).values(permission_overrides=flags))
    await session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url='http://test') as client:
        response = await client.get(url().replace('/delivery-evidence', '/delivery-readiness-summary'))
    assert response.status_code == 403, response.text
    assert response.json()['detail']['code'] == 'readiness_permission_denied'
    uow.commit.assert_not_awaited()
