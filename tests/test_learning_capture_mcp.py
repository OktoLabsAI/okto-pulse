"""Live MCP registry delegates to the same signed-evidence SQL capture path."""

from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

import test_learning_capture_writer as writer
from okto_pulse.core.mcp import server
from okto_pulse.core.ports.permission_policy import set_permission_flag

runtime = writer.runtime


@pytest.fixture
async def mcp_capture(runtime, monkeypatch):
    factory, _, store, request = runtime
    flags = writer.actor().permissions
    ctx = SimpleNamespace(agent_id='author', agent_name='Capture author', permissions=flags)
    async def agent(board_id):
        return ctx if board_id == writer.BOARD else None
    @asynccontextmanager
    async def unit(*, actor):
        assert actor.actor_id == ctx.agent_id and actor.board_id == writer.BOARD
        async with factory() as session:
            value = writer.uow(session)
            value.commit = session.commit
            try:
                yield value
            finally:
                await session.rollback()
    monkeypatch.setattr(server, '_get_agent_ctx', agent)
    monkeypatch.setattr(server, 'get_unit_of_work_factory_for_mcp', lambda: unit)
    tools = await server.mcp.get_tools()
    read = tools['okto_pulse_kg_get_learning_capture_context'].fn
    create = tools['okto_pulse_kg_create_learning_capture'].fn
    body = asdict(request)
    body['scenario_ids'] = list(body['scenario_ids'])
    yield read, create, body, store, flags


async def test_mcp_preview_capture_commit_and_exact_retry(mcp_capture):
    read, create, body, store, _ = mcp_capture
    observed = json.loads(await read(board_id=writer.BOARD, bug_id=body['bug_id']))
    assert observed['scenarios'][0]['authenticated'] is True
    assert await store.enumerate(writer.BOARD) == ()
    body.update(expected_source_digest=observed['source_digest'],
        expected_source_version=observed['source_policy_version'])
    first = json.loads(await create(**body))
    assert first['status'] == 'captured_pending_materialization'
    assert json.loads(await create(**body)) == first
    record, = await store.enumerate(writer.BOARD)
    assert record.payload['author_id'] == 'author'


@pytest.mark.parametrize('flag', writer.LEARNING_CAPTURE_CREATE_PERMISSIONS)
async def test_mcp_each_missing_permission_refuses_capture(mcp_capture, flag):
    _, create, body, store, flags = mcp_capture
    set_permission_flag(flags, flag, False)
    assert json.loads(await create(**body))['code'] == 'permission_denied'
    assert await store.enumerate(writer.BOARD) == ()


async def test_mcp_rejects_stale_basis_and_malformed_request(mcp_capture):
    _, create, body, store, _ = mcp_capture
    stale = {**body, 'expected_source_version': body['expected_source_version'] + 1}
    assert json.loads(await create(**stale))['code'] == 'learning_capture_source_changed_or_unavailable'
    invalid = {**body, 'expected_source_version': True}
    assert json.loads(await create(**invalid))['code'] == 'learning_capture_request_invalid'
    assert await store.enumerate(writer.BOARD) == ()


async def test_mcp_missing_auth_and_bug_do_not_write_or_leak(mcp_capture, monkeypatch):
    read, create, body, store, _ = mcp_capture
    denied = json.loads(await create(**{**body, 'board_id': 'foreign'}))
    assert 'error' in denied
    missing = json.loads(await read(board_id=writer.BOARD, bug_id='missing'))
    assert missing['code'] == 'bug_not_found'
    from okto_pulse.core.application.kg_operations import CoreKnowledgeGraphOperations
    async def failed(*args, **kwargs):
        raise RuntimeError('sensitive provider path')
    monkeypatch.setattr(CoreKnowledgeGraphOperations, 'get_learning_capture_source', failed)
    error = json.loads(await read(board_id=writer.BOARD, bug_id=body['bug_id']))
    assert error == {'error': 'learning_capture_unavailable', 'code': 'learning_capture_unavailable'}
    assert await store.enumerate(writer.BOARD) == ()


async def test_mcp_history_requires_cognitive_read_authority_and_returns_persisted_capture(mcp_capture):
    _, create, body, store, flags = mcp_capture
    listing = (await server.mcp.get_tools())['okto_pulse_kg_list_learning_captures'].fn
    assert json.loads(await create(**body))['status'] == 'captured_pending_materialization'
    # Historical absent flags retain their compatibility default. Exercise an
    # explicit denial rather than changing that established authority rule.
    set_permission_flag(flags, 'kg.query.learning_from_bugs', False)
    denied = json.loads(await listing(board_id=writer.BOARD, bug_id=body['bug_id']))
    assert denied['code'] == 'permission_denied'
    set_permission_flag(flags, 'kg.query.learning_from_bugs', True)
    page = json.loads(await listing(board_id=writer.BOARD, bug_id=body['bug_id'], limit=1))
    item, = page['items']
    assert item['capture']['content'] == body['content']
    assert item['capture']['author_id'] == 'author'
    assert page['next_cursor'] is None
    bad = json.loads(await listing(board_id=writer.BOARD, bug_id=body['bug_id'], limit=0))
    assert bad['code'] == 'learning_capture_page_invalid'
    assert len(await store.enumerate(writer.BOARD)) == 1
