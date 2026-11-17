"""Output-only R3 regression: actual composition, isolated fake workloads."""
import asyncio
import threading

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from okto_pulse.community.adapters import sqlalchemy_database
from okto_pulse.community.adapters.worker_runners import PollingRunner, WorkerDrainIncomplete

from okto_pulse.community.adapters import workers as composition


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['idle', 'inflight', 'expired_inflight'])
async def test_composed_outbox_stops_admission_and_retains_existing_native_work(monkeypatch, mode):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    entered = asyncio.Event()
    native_started, native_release, native_finished = (threading.Event() for _ in range(3))
    calls = []
    runner = None

    class OutboxWorkload:
        def __init__(self, _scope, **kwargs):
            self.executor = kwargs['blocking_execution']

        async def process_once(self):
            calls.append((runner._running, runner._stop_deadline is not None))
            entered.set()
            if mode != 'idle' and len(calls) == 1:
                def native():
                    native_started.set()
                    if not native_release.wait(5):
                        raise RuntimeError('diagnostic_release_timeout')
                    native_finished.set()
                await self.executor.run(native)
            return 0

    async def capture_start(value):
        return value

    monkeypatch.setattr(sqlalchemy_database, 'get_engine', lambda: engine)
    monkeypatch.setattr(composition, 'GlobalOutboxProcessor', OutboxWorkload)
    monkeypatch.setattr(composition, 'start_runner', capture_start)
    registry = composition.build_community_worker_registry(object())
    consolidation = await registry._specs['consolidation_worker'].start()
    await consolidation.stop()
    assert consolidation.shutdown_drained
    runner = await registry._specs['outbox_worker'].start()
    task = None
    try:
        await runner.start()
        task = runner._task
        await asyncio.wait_for(entered.wait(), 2)
        if mode != 'idle':
            assert await asyncio.to_thread(native_started.wait, 2)
        if mode == 'expired_inflight':
            with pytest.raises(WorkerDrainIncomplete):
                await runner.stop(timeout=0)
            assert not task.done()
            assert not native_finished.is_set()
            assert runner._blocking_execution.pending_count == 1
            native_release.set()
            await asyncio.wait_for(runner.observe_stopped(), 3)
        else:
            stopper = asyncio.create_task(runner.stop(timeout=1))
            await asyncio.sleep(0)
            assert not runner._running
            native_release.set()
            await asyncio.wait_for(stopper, 3)
        assert calls == [(True, False)], 'shutdown admitted a fresh outbox batch'
        if mode != 'idle':
            assert native_finished.is_set()
        assert runner._blocking_execution.pending_count == 0
        assert runner._task is None
    finally:
        native_release.set()
        if task is not None:
            if not task.done():
                await runner.stop(timeout=2)
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_other_polling_families_keep_explicit_final_iteration_support():
    entered = asyncio.Event()
    calls = []

    class Workload:
        async def process_once(self):
            calls.append(1)
            entered.set()
            return 0

    runner = PollingRunner(Workload(), name='diagnostic.optional_final_iteration',
        operation_name='process_once', interval_seconds=60, final_iteration=True)
    await runner.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runner.stop(timeout=1)
        assert len(calls) == 2
        assert runner._task is None
    finally:
        if runner._task is not None:
            await runner.stop(timeout=2)
