"""A busy SQLite writer must not escape the native MCP outcome contract."""

import asyncio
import json
import sqlite3

import pytest
from fastmcp import Client
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.mcp_host import (
    CommunityMcpHostProvider,
    _transport_tool,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Base, Board, Card, Comment,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.mcp_collaboration import (
    McpAddCommentCommand, McpAddCommentUseCase,
)
from okto_pulse.core.domain.permissions import PermissionSet
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.ports.mcp_resources import (
    StaticMcpResourceCatalog, freeze_mcp_resource_catalog,
)


@pytest.mark.asyncio
async def test_busy_comment_rolls_back_releases_admission_and_recovers_once(tmp_path):
    path = tmp_path / 'comment-contention.db'
    engine = create_async_engine(
        f'sqlite+aiosqlite:///{path}', connect_args={'timeout': 0.02},
    )
    sessions = async_sessionmaker(
        engine, expire_on_commit=False, sync_session_class=CommunitySemanticSession,
    )
    actor = ActorContext('agent', 'mcp', actor_name='Test agent', actor_kind='agent',
                        board_id='board', permissions=PermissionSet({
        'card': {'comments': {'create': True}, 'interact_in': {'in_progress': True}},
    }))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Board(id='board', name='Test', owner_id='owner', realm_id='local'))
        session.add(Card(id='card', board_id='board', title='Test',
                         status='in_progress', created_by='owner'))
        await session.commit()

    attempts = []
    catalog = CoreMcpCatalog(name='contention-test', version='0.4.3')

    @catalog.tool()
    async def okto_pulse_add_comment(content: str) -> str:
        attempts.append(content)
        async with CommunityUnitOfWork(sessions(), actor=actor) as uow:
            result = await McpAddCommentUseCase().execute(
                McpAddCommentCommand('board', 'card', content), actor=actor, uow=uow,
            )
        return json.dumps(result.payload)

    resources = freeze_mcp_resource_catalog(
        StaticMcpResourceCatalog('contention-test', (), precedence=1),
    )
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog, resource_catalog=resources, projection_identity=resources.identity,
    )
    blocker = sqlite3.connect(path, timeout=0.02)
    secret = 'private-comment-content-must-not-appear-in-errors'
    try:
        blocker.execute('BEGIN IMMEDIATE')
        async with Client(host) as client:
            result = await client.call_tool(
                'okto_pulse_add_comment', {'content': secret}, raise_on_error=False,
            )
            assert result.is_error
            assert result.structured_content is not None
            body = result.structured_content
            assert body['outcome'] == 'error'
            assert body['error_code'] == 'persistence_busy'
            assert body['retryable'] is False
            assert body['next_action']['rel'] == 'verify_current_state_before_retry'
            assert 'mutation_applied' not in body
            rendered = json.dumps(body) + ''.join(c.text for c in result.content)
            for internal in (secret, 'INSERT', 'parameters', str(path), 'OperationalError'):
                assert internal not in rendered
            assert attempts == [secret]  # The host must never replay the handler.
            assert (await host._okto_pulse_admission_controller.snapshot())['active'] == 0
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Comment)) == 0
                assert await session.scalar(select(func.count()).select_from(ActivityLog)) == 0

            blocker.rollback()
            recovered = await client.call_tool('okto_pulse_add_comment', {'content': secret})
            assert recovered.structured_content['outcome'] == 'success'
            assert attempts == [secret, secret]
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Comment)) == 1
                assert await session.scalar(select(func.count()).select_from(ActivityLog)) == 1
    finally:
        blocker.rollback()
        blocker.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                                 sqlite3.SQLITE_BUSY | (2 << 8)])
async def test_busy_extended_codes_are_sanitized_without_claiming_no_prior_commit(code):
    original = sqlite3.OperationalError('private driver details')
    original.sqlite_errorcode = code
    error = OperationalError('INSERT private_sql', ('private parameter',), original)
    committed = []

    async def handler():
        committed.append('prior-effect')
        raise error

    result = await _transport_tool(handler, tool_name='probe')()
    assert result.structured_content['error_code'] == 'persistence_busy'
    assert result.structured_content['retryable'] is False
    assert result.structured_content['data'] is None
    assert committed == ['prior-effect']
    assert 'private' not in json.dumps(result.structured_content)


@pytest.mark.asyncio
async def test_other_operational_errors_and_cancellation_are_not_reclassified():
    original = sqlite3.OperationalError('no such table: absent')
    original.sqlite_errorcode = sqlite3.SQLITE_ERROR
    error = OperationalError('SELECT * FROM absent', (), original)

    async def broken():
        raise error

    with pytest.raises(OperationalError) as raised:
        await _transport_tool(broken, tool_name='probe')()
    assert raised.value is error

    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _transport_tool(cancelled, tool_name='probe')()
