import asyncio
import threading
import warnings
import aiosqlite
import pytest
from sqlalchemy.dialects.sqlite.aiosqlite import AsyncAdapt_aiosqlite_connection
from sqlalchemy.util import greenlet_spawn
from types import SimpleNamespace
from okto_pulse.community.adapters.sqlalchemy_database import _install_aiosqlite_stop_guard, install_community_sqlite_pragmas


async def connection_with_counter():
    connection = await aiosqlite.connect(':memory:')
    calls = []
    original = connection.stop
    def counted():
        calls.append(1)
        return original()
    connection.stop = counted
    _install_aiosqlite_stop_guard(SimpleNamespace(driver_connection=connection))
    return connection, calls


async def hold_driver(connection):
    started, release = threading.Event(), threading.Event()
    def held():
        started.set()
        assert release.wait(3)
    work = asyncio.create_task(connection._execute(held))
    assert await asyncio.to_thread(started.wait, 1)
    return work, release


@pytest.mark.asyncio
async def test_normal_close_stops_worker_once_and_is_repeatable():
    connection, calls = await connection_with_counter()
    await asyncio.wait_for(connection.close(), 1)
    await asyncio.wait_for(connection.close(), 1)
    await asyncio.to_thread(connection._thread.join, 1)
    assert not connection._thread.is_alive()
    assert calls == [1]


@pytest.mark.asyncio
async def test_cancelled_stop_waiter_cannot_cancel_retained_completion():
    connection, calls = await connection_with_counter()
    work, release = await hold_driver(connection)
    first = connection.stop()
    second = connection.stop()
    assert first is not second
    first.cancel()
    assert first.cancelled()
    assert not second.done()
    assert calls == [1]
    release.set()
    await work
    await asyncio.wait_for(second, 1)
    await asyncio.to_thread(connection._thread.join, 1)
    assert not connection._thread.is_alive()
    await connection.stop()
    assert calls == [1]


@pytest.mark.asyncio
async def test_no_loop_stop_and_destructor_do_not_enqueue_duplicate_sentinels():
    connection, calls = await connection_with_counter()
    work, release = await hold_driver(connection)
    def outside_loop():
        assert connection.stop() is None
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', ResourceWarning)
            connection.__del__()
        assert connection.stop() is None
    await asyncio.to_thread(outside_loop)
    assert calls == [1]
    # None is not completion: worker is still held until the native operation
    # is released and the worker actually consumes its single stop sentinel.
    assert connection._thread.is_alive()
    release.set()
    await work
    await asyncio.to_thread(connection._thread.join, 1)
    assert not connection._thread.is_alive()
    assert connection._connection is None


@pytest.mark.asyncio
async def test_sqlalchemy_cancelled_termination_keeps_graceful_close_observable():
    connection, calls = await connection_with_counter()
    work, release = await hold_driver(connection)
    entered = asyncio.Event()
    graceful = None
    original_close = connection.close
    async def close():
        nonlocal graceful
        graceful = asyncio.current_task()
        entered.set()
        await original_close()
    connection.close = close
    adapter = AsyncAdapt_aiosqlite_connection(aiosqlite, connection)
    termination = asyncio.create_task(greenlet_spawn(adapter.terminate))
    await asyncio.wait_for(entered.wait(), 1)
    termination.cancel()
    with pytest.raises(asyncio.CancelledError):
        await termination
    assert graceful is not None and not graceful.done()
    release.set()
    await work
    await asyncio.wait_for(graceful, 1)
    await asyncio.to_thread(connection._thread.join, 1)
    assert not connection._thread.is_alive()
    assert calls == [1]


@pytest.mark.asyncio
async def test_engine_hook_owns_cancelled_termination_without_patching_other_engines():
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    unrelated = create_async_engine('sqlite+aiosqlite:///:memory:')
    install_community_sqlite_pragmas(engine)
    try:
        async with unrelated.connect() as other:
            other_raw = await other.get_raw_connection()
            assert not getattr(other_raw.driver_connection, '_pulse_stop_guard_installed', False)
        async with engine.connect() as sql_connection:
            raw = await sql_connection.get_raw_connection()
            driver = raw.driver_connection
            work, release = await hold_driver(driver)
            entered = asyncio.Event()
            graceful = None
            original_close = driver.close
            async def close():
                nonlocal graceful
                graceful = asyncio.current_task()
                entered.set()
                await original_close()
            driver.close = close
            termination = asyncio.create_task(greenlet_spawn(raw.dbapi_connection.terminate))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                termination.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await termination
                assert graceful is not None and not graceful.done()
                release.set()
                await work
                await asyncio.wait_for(graceful, 1)
                await asyncio.to_thread(driver._thread.join, 1)
                assert not driver._thread.is_alive()
            finally:
                release.set()
                await asyncio.gather(work, termination, return_exceptions=True)
                if graceful is not None:
                    await asyncio.gather(graceful, return_exceptions=True)
                await sql_connection.invalidate()
    finally:
        await engine.dispose()
        await unrelated.dispose()
