from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from okto_pulse.community.adapters.worker_runners import (
    ConsolidationRunner,
    PollingRunner,
)
from okto_pulse.community.adapters.core_import_boundary import (
    audit_community_core_import_boundary,
)


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
    assert runner._tasks == set()
    assert processor.recoveries == 1
    assert blocking.join_calls == [0.5]


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
