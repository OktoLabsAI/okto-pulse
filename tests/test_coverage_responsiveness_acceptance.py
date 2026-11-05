"""Coverage admission refuses late work and drains owned work on cancellation."""
import asyncio
import threading
import time

import pytest
from okto_pulse.community.adapters import graph_operation_guards as guards
from okto_pulse.core.kg.blocking_io import run_blocking_graph_io
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout


def test_closing_guard_uses_caller_deadline_without_late_pin(monkeypatch):
    guard = guards._BoardCloseGuard('r7-closing')
    monkeypatch.setitem(guards._board_close_guards, 'r7-closing', guard)
    started = time.monotonic()
    with guard.closing() as (drained, _):
        assert drained
        with pytest.raises(GraphQueryTimeout):
            with guards.board_graph_operation_window('r7-closing', deadline=started + 0.02):
                pytest.fail('native work admitted while closing')
        assert guard.readers == 0
    assert time.monotonic() - started < 1
    assert guard.readers == 0


def test_expired_guard_never_pins_and_normal_errors_release(monkeypatch):
    guard = guards._BoardCloseGuard('r7-expired')
    monkeypatch.setitem(guards._board_close_guards, 'r7-expired', guard)
    with pytest.raises(GraphQueryTimeout):
        with guards.board_graph_operation_window('r7-expired', deadline=time.monotonic() - 1):
            pytest.fail('expired caller entered')
    assert guard.readers == 0
    with pytest.raises(RuntimeError, match='owned failure'):
        with guards.board_graph_operation_window('r7-expired', deadline=time.monotonic() + 1):
            assert guard.readers == 1
            raise RuntimeError('owned failure')
    assert guard.readers == 0


@pytest.mark.asyncio
async def test_cancellation_drains_worker_before_releasing_its_pin(monkeypatch):
    guard = guards._BoardCloseGuard('r7-cancel')
    monkeypatch.setitem(guards._board_close_guards, 'r7-cancel', guard)
    entered, release = threading.Event(), threading.Event()
    def owned_work():
        with guards.board_graph_operation_window('r7-cancel'):
            entered.set()
            assert release.wait(2)
    task = asyncio.create_task(run_blocking_graph_io(owned_work, task_name='r7-test-cancel'))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set() and guard.readers == 1
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and guard.readers == 1
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert guard.readers == 0
