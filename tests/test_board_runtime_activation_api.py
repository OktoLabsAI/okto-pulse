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
from okto_pulse.community.api.deps import get_read_unit_of_work, get_unit_of_work
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
    app.dependency_overrides[get_read_unit_of_work] = lambda: uow
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
async def test_observation_busy_has_explicit_retry_contract_and_then_recovers(api):
    from okto_pulse.core.kg.interfaces.graph_errors import GraphActivationBusy
    api.execute.side_effect = [
        GraphActivationBusy('private storage path', details={'owner_token': 'secret-token'}),
        {'board_id': 'board', 'state': 'ready'},
    ]
    response = await api.client.post('/boards/board/runtime/activate')
    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    assert response.json()['detail'] == {
        'code': 'board_runtime_busy',
        'message': 'Graph information is being updated. Please try again shortly.',
        'retryable': True,
    }
    assert 'private storage path' not in response.text and 'secret-token' not in response.text
    recovered = await api.client.post('/boards/board/runtime/activate')
    assert recovered.status_code == 200 and recovered.json()['state'] == 'ready'


@pytest.mark.asyncio
async def test_arbitrary_timeout_has_no_automatic_retry_contract(api):
    from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout
    api.execute.side_effect = GraphQueryTimeout('unknown phase')
    response = await api.client.post('/boards/board/runtime/activate')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'board_runtime_unavailable'
    assert 'retry-after' not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(('failure', 'expected'), [
    (GuardedWriteError('lock_contention', 'private storage path', retryable=True,
        details={'owner_token': 'secret-token', 'path': 'private storage path'}),
        ('GuardedWriteError', 'lock_contention', True, None, None)),
    (GuardedWriteError('safe_lifecycle_failed', 'private storage path', retryable=True,
        details={'failed_step': 'checkpoint', 'owner_token': 'secret-token'}),
        ('GuardedWriteError', 'safe_lifecycle_failed', True, None, 'checkpoint')),
    (GraphCapabilityUnavailable('private storage path', details={
        'phase': 'prepare_query_reader', 'owner_token': 'secret-token'}),
        ('GraphError', 'graph_capability_unavailable', False, 'prepare_query_reader', None)),
    (GuardedWriteError('secret-token', 'private storage path', retryable=False,
        details={'failure_phase': 'secret-token', 'failed_step': 'private storage path'}),
        ('GuardedWriteError', 'other', False, None, None)),
])
async def test_activation_logs_only_bounded_typed_failure(api, caplog, failure, expected):
    api.execute.side_effect = failure
    response = await api.client.post('/boards/board/runtime/activate')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'board_runtime_unavailable'
    records = [record for record in caplog.records
               if record.name == boards.__name__ and record.msg.startswith('board_runtime.activation_failed')]
    assert len(records) == 1
    assert records[0].args == ('board', *expected)
    assert records[0].exc_info is None
    assert 'private storage path' not in records[0].getMessage() + response.text
    assert 'secret-token' not in records[0].getMessage() + response.text


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


@pytest_asyncio.fixture
async def wal_activation(tmp_path, monkeypatch):
    """Real REST dependency/WAL, representative read and simulated graph wait."""
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from okto_pulse.community.adapters.sqlalchemy_database import install_community_sqlite_pragmas
    from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
    from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
    from okto_pulse.core.ports.authentication import Principal

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activation.db'}")
    install_community_sqlite_pragmas(engine)
    statements = []

    @event.listens_for(engine.sync_engine, 'connect')
    def bounded_contention(connection, _record):
        # A lock regression fails promptly; this changes no production timeout.
        cursor = connection.cursor()
        cursor.execute('PRAGMA busy_timeout=100')
        cursor.close()

    @event.listens_for(engine.sync_engine, 'before_cursor_execute')
    def capture(_connection, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    await initialize_current_schema(engine, current_schema_contract())
    async with engine.connect() as connection:
        assert (await connection.exec_driver_sql('PRAGMA journal_mode')).scalar() == 'wal'
    sessions = async_sessionmaker(engine, class_=AsyncSession,
        sync_session_class=CommunitySemanticSession, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def activate(_self, command, *, actor, uow):
        # Actual SQL read through the public service proves admission, not just
        # an empty handler that could miss transaction acquisition entirely.
        assert actor.board_id == command.board_id
        await uow.services.get_application_record(entity='board', record_id=command.board_id)
        entered.set()
        await release.wait()
        return {'board_id': command.board_id, 'state': 'ready'}

    monkeypatch.setattr(boards.ActivateBoardRuntimeUseCase, 'execute', activate)
    app = FastAPI()
    app.state.runtime_composition = SimpleNamespace(uow_factory=CommunityUnitOfWorkFactory(sessions))
    app.include_router(boards.router, prefix='/boards')
    app.dependency_overrides[require_principal] = lambda: Principal(subject='activation-user', realm_id='local')
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            statements.clear()
            yield SimpleNamespace(client=client, engine=engine, entered=entered,
                                  release=release, statements=statements)
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_activation_does_not_hold_sqlite_writer_during_native_work(wal_activation):
    runtime = wal_activation
    request = asyncio.create_task(runtime.client.post('/boards/board/runtime/activate'))
    try:
        await asyncio.wait_for(runtime.entered.wait(), timeout=5)
        # Simulated graph work remains suspended while an independent transaction
        # writes and commits. BEGIN IMMEDIATE on the request breaks this.
        async with runtime.engine.begin() as writer:
            await writer.exec_driver_sql("INSERT INTO app_settings (key, value) VALUES ('activation_writer_probe', '1')")
        assert not request.done()
        assert not any(sql.strip().upper() == 'BEGIN IMMEDIATE' for sql in runtime.statements)
    finally:
        runtime.release.set()
        response = await asyncio.wait_for(request, timeout=5)
    assert response.status_code == 200
    async with runtime.engine.connect() as reader:
        assert (await reader.exec_driver_sql("SELECT value FROM app_settings WHERE key='activation_writer_probe'")).scalar() == '1'


@pytest.mark.asyncio
async def test_activation_reads_while_an_unrelated_sqlite_writer_is_active(wal_activation):
    runtime = wal_activation
    runtime.release.set()
    async with runtime.engine.begin() as writer:
        await writer.exec_driver_sql("INSERT INTO app_settings (key, value) VALUES ('activation_writer_probe', '1')")
        response = await asyncio.wait_for(
            runtime.client.post('/boards/board/runtime/activate'), timeout=5)
        assert response.status_code == 200
        assert runtime.entered.is_set()
        assert not any(sql.strip().upper() == 'BEGIN IMMEDIATE' for sql in runtime.statements)
