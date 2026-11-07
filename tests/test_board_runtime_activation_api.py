"""Explicit activation transport; no native graph or live service access."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from okto_pulse.community.api import boards
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.base import ActorContext, EntityNotFoundError, PermissionDeniedError
from okto_pulse.core.kg.guarded_write import GuardedWriteError
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable, GraphCorruption


@pytest_asyncio.fixture
async def api(monkeypatch):
    actor = ActorContext('actor', 'rest', actor_kind='user', board_id='board', realm_id='local')
    principal, uow = object(), object()
    actor_calls = []
    def adapt(value, *, board_id):
        actor_calls.append((value, board_id))
        return actor
    monkeypatch.setattr(boards.RESTAdapterContract, 'actor_from_principal', adapt)
    execute = AsyncMock(return_value={'board_id': 'board', 'state': 'ready'})
    monkeypatch.setattr(boards.ActivateBoardRuntimeUseCase, 'execute', execute)
    app = FastAPI()
    app.include_router(boards.router, prefix='/boards')
    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_unit_of_work] = lambda: uow
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield SimpleNamespace(client=client, actor=actor, principal=principal, uow=uow,
                              actor_calls=actor_calls, execute=execute)


@pytest.mark.asyncio
async def test_activation_is_explicit_post_and_delegates_scoped_actor(api):
    response = await api.client.post('/boards/board/runtime/activate')
    assert response.status_code == 200
    assert response.json() == {'board_id': 'board', 'state': 'ready'}
    assert api.actor_calls == [(api.principal, 'board')]
    args, kwargs = api.execute.call_args
    assert args[0].board_id == 'board'
    assert kwargs == {'actor': api.actor, 'uow': api.uow}
    assert (await api.client.get('/boards/board/runtime/activate')).status_code == 405
    assert api.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('failure', 'status_code'), [
    (EntityNotFoundError('board', 'board'), 404),
    (PermissionDeniedError('Missing Board permission'), 403),
    (GraphCapabilityUnavailable('private storage path', details={'path': 'private storage path'}), 503),
    (GraphCorruption('private storage path'), 503),
    (GuardedWriteError('writer_lease_lost', 'private storage path', retryable=False), 503),
])
async def test_activation_preserves_access_failures_and_hides_native_details(api, failure, status_code):
    api.execute.side_effect = failure
    response = await api.client.post('/boards/board/runtime/activate')
    assert response.status_code == status_code
    assert 'private storage path' not in response.text
    if status_code == 503:
        assert response.json()['detail'] == {
            'code': 'board_runtime_unavailable',
            'message': 'Graph information is unavailable. Refresh the board to try again.',
        }


@pytest.mark.asyncio
async def test_activation_cancellation_is_not_converted_to_ready_or_unavailable(api):
    api.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await boards.activate_board_runtime('board', principal=api.principal, uow=api.uow)


@pytest.mark.asyncio
async def test_ordinary_board_read_does_not_activate_runtime(api, monkeypatch):
    board = object()
    read = AsyncMock(return_value=SimpleNamespace(board=board))
    monkeypatch.setattr(boards.GetBoardUseCase, 'execute', read)
    assert await boards.get_board('board', compact=True, principal=api.principal, uow=api.uow) is board
    assert read.call_args.args[0].compact is True
    api.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_programming_error_is_not_reported_as_optional_graph_failure(api):
    api.execute.side_effect = ValueError('programming failure')
    with pytest.raises(ValueError, match='programming failure'):
        await boards.activate_board_runtime('board', principal=api.principal, uow=api.uow)
