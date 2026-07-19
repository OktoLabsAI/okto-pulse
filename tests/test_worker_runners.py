from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from okto_pulse.community.adapters.worker_runners import (
    ConsolidationRunner,
    PollingRunner,
    TrackedBlockingExecution,
)
from okto_pulse.community.adapters.core_import_boundary import (
    audit_community_core_import_boundary,
)
from okto_pulse.core.kg.blocking_io import run_blocking_graph_io
from okto_pulse.core.ports.runtime_workers import WorkerDrainIncomplete


class _Processor:
    def __init__(self) -> None:
        self.calls = 0
        self.recoveries = 0
        self.first_call = asyncio.Event()

    async def process_once(self) -> int:
        self.calls += 1
        self.first_call.set()
        return 0

    async def process_batch(self) -> int:
        return await self.process_once()

    async def recover_stale_claims(self) -> int:
        self.recoveries += 1
        return 0

    async def run_dlq_auto_drain(self) -> None:
        return None

    def get_dlq_drain_stats(self, _board_id: str) -> dict[str, object]:
        return {"last_run_at": None, "requeued_count": 0}


class _BlockingExecution:
    def __init__(self) -> None:
        self.join_calls: list[float] = []

    async def run(self, operation):
        return operation()

    async def join(self, timeout: float) -> int:
        self.join_calls.append(timeout)
        return 0


class _FailedAttemptThenIdleProcessor(_Processor):
    def __init__(self) -> None:
        super().__init__()
        self.last_attempted_count = 0
        self.follow_up = asyncio.Event()

    async def process_batch(self) -> int:
        self.calls += 1
        self.first_call.set()
        if self.calls == 1:
            self.last_attempted_count = 1
        else:
            self.last_attempted_count = 0
            self.follow_up.set()
        return 0


class _TransientRecoveryFailureProcessor(_Processor):
    def __init__(self) -> None:
        super().__init__()
        self.recovered_after_failure = asyncio.Event()

    async def recover_stale_claims(self) -> int:
        self.recoveries += 1
        if self.recoveries == 2:
            raise RuntimeError("transient sqlite lock")
        if self.recoveries >= 3:
            self.recovered_after_failure.set()
        return 0


@pytest.mark.asyncio
async def test_tracked_blocking_failure_is_consumed_after_parent_cancel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = TrackedBlockingExecution()
    started = threading.Event()
    release = threading.Event()

    def operation() -> None:
        started.set()
        assert release.wait(timeout=1)
        raise RuntimeError("payload-must-not-be-logged")

    parent = asyncio.create_task(executor.run(operation))
    assert await asyncio.to_thread(started.wait, 1)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent

    with caplog.at_level(
        logging.ERROR,
        logger="okto_pulse.community.workers",
    ):
        release.set()
        assert await executor.join(1) == 0
        await asyncio.sleep(0)

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "")
        == "community.worker.blocking_operation_failed"
    ]
    assert len(records) == 1
    assert records[0].error_type == "RuntimeError"
    assert "payload-must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_tracked_blocking_execution_preserves_global_write_context() -> None:
    from okto_pulse.core.kg.write_barrier import (
        has_active_global_guard,
        under_global_safe_write,
    )

    executor = TrackedBlockingExecution()

    with under_global_safe_write("outbox-test", "context_propagation"):
        active_in_native_thread = await executor.run(has_active_global_guard)

    assert active_in_native_thread is True


@pytest.mark.asyncio
async def test_tracked_join_observes_submission_after_initial_snapshot() -> None:
    executor = TrackedBlockingExecution()
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()
    second_release = threading.Event()

    def first_operation() -> None:
        first_started.set()
        assert first_release.wait(timeout=2)

    def second_operation() -> None:
        second_started.set()
        assert second_release.wait(timeout=2)

    first_parent = asyncio.create_task(executor.run(first_operation))
    assert await asyncio.to_thread(first_started.wait, 1)
    join_task = asyncio.create_task(executor.join(1))
    # Let join capture and begin waiting on the first generation.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    second_parent = asyncio.create_task(executor.run(second_operation))
    assert await asyncio.to_thread(second_started.wait, 1)
    first_release.set()
    await first_parent
    await asyncio.sleep(0.01)

    assert not join_task.done()
    second_release.set()
    await second_parent
    assert await join_task == 0


@pytest.mark.asyncio
async def test_consolidation_timeout_preserves_runner_and_native_handles() -> None:
    executor = TrackedBlockingExecution()
    native_started = threading.Event()
    native_release = threading.Event()

    class _NativeProcessor(_Processor):
        async def process_batch(self) -> int:
            self.first_call.set()

            def native_operation() -> None:
                native_started.set()
                assert native_release.wait(timeout=2)

            await run_blocking_graph_io(
                native_operation,
                task_name="test.consolidation.native",
                blocking_execution=executor,
            )
            return 0

    processor = _NativeProcessor()
    runner = ConsolidationRunner(
        processor,
        blocking_execution=executor,
        heartbeat_seconds=60,
        recovery_interval_seconds=60,
        max_concurrent_workers=1,
        join_timeout=0.01,
    )
    await runner.start()
    assert await asyncio.to_thread(native_started.wait, 1)

    with pytest.raises(WorkerDrainIncomplete) as captured:
        await runner.stop()

    assert captured.value.pending_tasks >= 1
    assert captured.value.pending_operations >= 1
    assert runner._processing_task is not None
    assert runner._processing_task in runner._tasks
    assert executor.pending_count >= 1
    assert runner.shutdown_drained is False

    native_release.set()
    await asyncio.gather(*runner._tasks, return_exceptions=True)
    await runner.stop(timeout=0.5)
    assert runner._tasks == set()
    assert runner._processing_task is None
    assert executor.pending_count == 0
    assert runner.shutdown_drained is True


@pytest.mark.asyncio
async def test_polling_runner_propagates_cancelled_after_final_iteration() -> None:
    processor = _Processor()
    runner = PollingRunner(
        processor,
        name="test.final_iteration",
        interval_seconds=60,
        operation_name="process_once",
        final_iteration=True,
    )
    runner._running = True
    runner._wake_event = asyncio.Event()
    task = asyncio.create_task(runner.run_forever())
    await asyncio.wait_for(processor.first_call.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert processor.calls == 2


@pytest.mark.asyncio
async def test_outbox_final_iteration_completes_its_dedicated_native_drain() -> None:
    executor = TrackedBlockingExecution()
    native_finalized = threading.Event()

    class _OutboxProcessor(_Processor):
        async def process_once(self) -> int:
            self.calls += 1
            self.first_call.set()
            if self.calls == 2:
                await run_blocking_graph_io(
                    native_finalized.set,
                    task_name="test.outbox.final_native",
                    blocking_execution=executor,
                )
            return 0

    processor = _OutboxProcessor()
    runner = PollingRunner(
        processor,
        name="community.kg.outbox_runner",
        interval_seconds=60,
        operation_name="process_once",
        final_iteration=True,
        blocking_execution=executor,
    )
    await runner.start()
    await asyncio.wait_for(processor.first_call.wait(), timeout=1)

    await runner.stop(timeout=0.5)

    assert processor.calls == 2
    assert native_finalized.is_set()
    assert executor.pending_count == 0
    assert runner._task is None


@pytest.mark.asyncio
async def test_outbox_final_iteration_is_blocked_without_consolidation_drain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor = _Processor()
    executor = TrackedBlockingExecution()
    consolidation = ConsolidationRunner(
        _Processor(),
        blocking_execution=TrackedBlockingExecution(),
        heartbeat_seconds=60,
        recovery_interval_seconds=60,
        max_concurrent_workers=1,
    )
    runner = PollingRunner(
        processor,
        name="community.kg.outbox_runner",
        interval_seconds=60,
        operation_name="process_once",
        final_iteration=True,
        blocking_execution=executor,
        final_iteration_guard=lambda: consolidation.shutdown_drained,
    )
    await runner.start()
    await asyncio.wait_for(processor.first_call.wait(), timeout=1)

    with caplog.at_level(
        logging.CRITICAL,
        logger="okto_pulse.community.workers",
    ):
        await runner.stop(timeout=0.5)

    assert processor.calls == 1
    assert executor.pending_count == 0
    assert any(
        getattr(record, "event", "")
        == "community.worker.final_iteration_skipped"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_consolidation_runner_stops_and_joins_every_owned_task() -> None:
    processor = _Processor()
    blocking = _BlockingExecution()
    runner = ConsolidationRunner(
        processor,
        blocking_execution=blocking,
        heartbeat_seconds=60,
        recovery_interval_seconds=60,
        max_concurrent_workers=4,
        join_timeout=0.5,
    )

    await runner.start()
    await asyncio.wait_for(processor.first_call.wait(), timeout=1)
    assert runner.is_running
    await runner.stop()

    assert not runner.is_running
    assert runner.shutdown_drained is True
    assert runner._tasks == set()
    assert processor.recoveries == 1
    assert len(blocking.join_calls) == 1
    assert 0 < blocking.join_calls[0] <= 0.5


@pytest.mark.asyncio
async def test_consolidation_runner_snapshot_is_app_local() -> None:
    processor = _Processor()
    blocking = _BlockingExecution()
    runner = ConsolidationRunner(
        processor,
        blocking_execution=blocking,
        heartbeat_seconds=60,
        recovery_interval_seconds=60,
        max_concurrent_workers=3,
    )

    assert runner.snapshot(board_id="board-a")["active"] == 0
    await runner.start()
    assert runner.snapshot(board_id="board-a") == {
        "active": 3,
        "idle": 0,
        "draining": 0,
        "running": True,
        "last_run_at": None,
        "requeued_count": 0,
    }
    await runner.stop(timeout=0.5)


@pytest.mark.asyncio
async def test_failed_attempt_gets_one_immediate_follow_up_without_busy_cycle() -> None:
    processor = _FailedAttemptThenIdleProcessor()
    runner = ConsolidationRunner(
        processor,
        blocking_execution=_BlockingExecution(),
        heartbeat_seconds=60,
        recovery_interval_seconds=60,
        max_concurrent_workers=1,
    )

    await runner.start()
    await asyncio.wait_for(processor.follow_up.wait(), timeout=1)
    await asyncio.sleep(0.05)

    # The attempted (but failed) row released its board for one immediate
    # scheduling pass. Once that pass found no runnable row, the runner slept.
    assert processor.calls == 2
    await runner.stop(timeout=0.5)


@pytest.mark.asyncio
async def test_processing_task_death_is_not_masked_by_recovery_task() -> None:
    processor = _Processor()
    runner = ConsolidationRunner(
        processor,
        blocking_execution=_BlockingExecution(),
        heartbeat_seconds=60,
        recovery_interval_seconds=60,
        max_concurrent_workers=1,
    )

    await runner.start()
    await asyncio.wait_for(processor.first_call.wait(), timeout=1)
    assert runner._processing_task is not None
    assert runner._recovery_task is not None
    runner._processing_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner._processing_task

    assert not runner.is_running
    assert not runner._recovery_task.done()
    await runner.stop(timeout=0.5)


@pytest.mark.asyncio
async def test_recovery_loop_survives_transient_failure_and_reclaims_again() -> None:
    processor = _TransientRecoveryFailureProcessor()
    runner = ConsolidationRunner(
        processor,
        blocking_execution=_BlockingExecution(),
        heartbeat_seconds=60,
        recovery_interval_seconds=0.01,
        max_concurrent_workers=1,
    )

    await runner.start()
    await asyncio.wait_for(processor.recovered_after_failure.wait(), timeout=1)

    assert processor.recoveries >= 3
    assert runner.is_running
    await runner.stop(timeout=0.5)


def test_worker_runner_boundary_has_no_core_implementation_reach_in() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    report = audit_community_core_import_boundary(repo_root)
    forbidden = [
        item
        for item in report["full_inventory"]
        if item["module"].startswith(
            ("okto_pulse.core.kg.workers", "okto_pulse.core.events")
        )
    ]

    assert report["ok"] is True
    assert forbidden == []
