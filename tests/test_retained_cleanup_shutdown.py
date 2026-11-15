"""Retained cleanup and close-order regressions for the isolated v3 candidate."""
import asyncio
from contextlib import closing
import sqlite3
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import CommunityDatabaseRuntime
from okto_pulse.community.adapters import sqlalchemy_unit_of_work as uow_mod
from okto_pulse.community.adapters.worker_runners import PollingRunner
from okto_pulse.community.runtime import run_async_server
from okto_pulse.community.shutdown_ownership import ShutdownOwnership


@pytest.mark.asyncio
async def test_final_cleanup_observer_holds_both_kinds_without_disposal():
    class Engine:
        async def dispose(self):
            pytest.fail('Cleanup observation must not dispose the engine')

    runtime = CommunityDatabaseRuntime(Engine(), lambda: None)
    session_release, uow_release = asyncio.Event(), asyncio.Event()
    session = asyncio.create_task(session_release.wait())
    uow = asyncio.create_task(uow_release.wait())
    runtime._pending_session_closes.add(session)
    uow_mod._pending_uow_cleanups.add(uow)
    observing = asyncio.create_task(runtime.observe_retained_cleanup())
    try:
        await asyncio.sleep(0)
        assert not observing.done()
        session_release.set()
        await session
        assert not observing.done() and not uow.done()
        assert uow.cancelling() == 0
        uow_release.set()
        await observing
    finally:
        session_release.set()
        uow_release.set()
        await asyncio.gather(session, uow, observing)
        uow_mod._pending_uow_cleanups.discard(uow)


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [0, 1])
async def test_generic_blocking_port_requires_capability_only_if_work_remains(pending):
    calls = []

    class GenericPort:
        async def join(self, timeout):
            calls.append(timeout)
            return pending

    runner = PollingRunner(object(), name='test.generic-owner', interval_seconds=1,
                           operation_name='process_once',
                           blocking_execution=GenericPort())
    if pending:
        with pytest.raises(RuntimeError, match='worker_blocking_shutdown_observer_unavailable'):
            await runner.observe_stopped()
    else:
        await runner.observe_stopped()
    assert calls == [0.0]


@pytest.mark.asyncio
async def test_coordinator_cancellation_cannot_dispose_db_while_graph_thread_is_live():
    entered, release = threading.Event(), threading.Event()
    events = []
    ownership = ShutdownOwnership(('consolidation_worker',))

    async def settled():
        pass

    def native_graph_close():
        entered.set()
        release.wait()
        events.append('graph_terminal')

    async def graph_close():
        await asyncio.to_thread(native_graph_close)

    async def database_close():
        events.append('database_disposed')

    ownership.start(observe_producers=settled, drain_cleanup=settled,
                    close_graph=graph_close, close_database=database_close)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        ownership.task.cancel()
        await asyncio.sleep(0)
        ownership.task.cancel()
        await asyncio.sleep(0)
        assert not ownership.task.done()
        assert events == []
        release.set()
        await ownership.wait_before_runner_exit()
        assert events == ['graph_terminal', 'database_disposed']
        assert ownership.deadline_failed
    finally:
        release.set()
        await ownership.wait_before_runner_exit()


def test_real_sql_cleanup_finishes_before_actual_runner_exit(tmp_path):
    events = []
    holders = {}
    db_path = tmp_path / 'isolated.sqlite3'

    async def application():
        rollback_started, release = asyncio.Event(), asyncio.Event()

        class Session(AsyncSession):
            async def rollback(self):
                rollback_started.set()
                try:
                    await release.wait()
                    await super().rollback()
                    events.append('rollback_finished')
                except asyncio.CancelledError:
                    events.append('rollback_cancelled')
                    raise

        engine = create_async_engine(f'sqlite+aiosqlite:///{db_path.as_posix()}')
        holders['engine'] = engine
        runtime = CommunityDatabaseRuntime(engine, async_sessionmaker(engine, class_=Session))
        async with engine.begin() as connection:
            await connection.execute(text('CREATE TABLE probe (value INTEGER)'))

        async def owner():
            async with runtime.cancel_safe_session_scope() as session:
                await session.execute(text('INSERT INTO probe VALUES (1)'))

        parent = asyncio.create_task(owner())
        await asyncio.wait_for(rollback_started.wait(), 5)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        asyncio.get_running_loop().call_soon(release.set)
        await runtime.observe_retained_cleanup()
        events.append('application_returned')

    try:
        run_async_server(application())
        assert events == ['rollback_finished', 'application_returned']
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute('SELECT count(*) FROM probe').fetchone()[0] == 0
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        if 'engine' in holders:
            run_async_server(holders['engine'].dispose())
