"""Community-owned asyncio runners for Core application processors."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from okto_pulse.core.ports.runtime_workers import BlockingExecutionPort

logger = logging.getLogger("okto_pulse.community.workers")


class UtcWorkerClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class TrackedBlockingExecution(BlockingExecutionPort):
    """Offload blocking calls and retain them until application shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    async def run(self, operation: Callable[[], Any]) -> Any:
        task = asyncio.create_task(
            asyncio.to_thread(operation),
            name="community.worker.blocking_operation",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    async def join(self, timeout: float) -> int:
        pending = {task for task in self._tasks if not task.done()}
        if not pending:
            return 0
        _done, pending = await asyncio.wait(pending, timeout=timeout)
        return len(pending)


class PollingRunner:
    """Wakeable runner for one task-free processor."""

    def __init__(
        self,
        processor: Any,
        *,
        name: str,
        interval_seconds: float,
        operation_name: str,
        recover: Callable[[], Awaitable[int]] | None = None,
        final_iteration: bool = False,
    ) -> None:
        self.processor = processor
        self.name = name
        self.interval_seconds = interval_seconds
        self.operation_name = operation_name
        self._recover = recover
        self._final_iteration = final_iteration
        self._task: asyncio.Task[None] | None = None
        self._wake_event: asyncio.Event | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def wake_event(self) -> asyncio.Event | None:
        return self._wake_event

    def notify(self) -> None:
        if self._wake_event is not None:
            self._wake_event.set()

    async def start(self) -> "PollingRunner":
        if self.is_running:
            return self
        if self._recover is not None:
            await self._recover()
        self._wake_event = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self.run_forever(), name=self.name)
        return self

    async def process_once(self) -> int:
        operation = getattr(self.processor, self.operation_name)
        return int(await operation())

    async def run_forever(self) -> None:
        try:
            while self._running:
                try:
                    await self.process_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("worker iteration failed family=%s", self.name)
                wake = self._wake_event
                if wake is None:
                    return
                try:
                    await asyncio.wait_for(
                        wake.wait(),
                        timeout=self.interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                wake.clear()
        except asyncio.CancelledError:
            if self._final_iteration:
                try:
                    await self.process_once()
                except Exception:
                    logger.exception("worker final iteration failed family=%s", self.name)
            raise

    async def stop(self, timeout: float = 10.0) -> None:
        self._running = False
        self.notify()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._task = None
        self._wake_event = None

    def snapshot(self, **_: Any) -> dict[str, Any]:
        return {"running": self.is_running}


class ConsolidationRunner:
    """Own both consolidation polling and stale-lease recovery tasks."""

    def __init__(
        self,
        processor: Any,
        *,
        blocking_execution: BlockingExecutionPort,
        heartbeat_seconds: float,
        recovery_interval_seconds: float,
        max_concurrent_workers: int,
        join_timeout: float = 30.0,
    ) -> None:
        self.processor = processor
        self._blocking_execution = blocking_execution
        self.heartbeat_seconds = heartbeat_seconds
        self.recovery_interval_seconds = recovery_interval_seconds
        self.max_concurrent_workers = max(1, int(max_concurrent_workers))
        self.join_timeout = join_timeout
        self._running = False
        self._wake_event: asyncio.Event | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def is_running(self) -> bool:
        return self._running and any(not task.done() for task in self._tasks)

    def notify(self) -> None:
        if self._wake_event is not None:
            self._wake_event.set()

    async def start(self) -> "ConsolidationRunner":
        if self.is_running:
            return self
        await self.processor.recover_stale_claims()
        self._wake_event = asyncio.Event()
        self._running = True
        self._tasks = {
            asyncio.create_task(
                self.run_forever(),
                name="community.kg.consolidation_runner",
            ),
            asyncio.create_task(
                self.run_recovery_forever(),
                name="community.kg.consolidation_recovery_runner",
            ),
        }
        return self

    async def process_once(self) -> int:
        return int(await self.processor.process_batch())

    async def process_batch(self) -> int:
        return await self.process_once()

    async def run_forever(self) -> None:
        try:
            while self._running:
                try:
                    processed = await self.process_once()
                    if processed > 0:
                        await asyncio.sleep(0)
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("consolidation batch failed")
                wake = self._wake_event
                if wake is None:
                    return
                try:
                    await asyncio.wait_for(
                        wake.wait(),
                        timeout=self.heartbeat_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                wake.clear()
                await self.processor.run_dlq_auto_drain()
        except asyncio.CancelledError:
            raise

    async def run_recovery_forever(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self.recovery_interval_seconds)
                await self.processor.recover_stale_claims()
        except asyncio.CancelledError:
            raise

    async def stop(self, timeout: float | None = None) -> None:
        self._running = False
        self.notify()
        tasks = {task for task in self._tasks if not task.done()}
        for task in tasks:
            task.cancel()
        effective_timeout = self.join_timeout if timeout is None else timeout
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=effective_timeout)
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
            if pending:
                logger.warning(
                    "consolidation runner join timed out pending=%d timeout_s=%s",
                    len(pending),
                    effective_timeout,
                )
        blocking_pending = await self._blocking_execution.join(effective_timeout)
        if blocking_pending:
            logger.warning(
                "consolidation blocking join timed out pending=%d timeout_s=%s",
                blocking_pending,
                effective_timeout,
            )
        self._tasks.clear()
        self._wake_event = None

    def snapshot(self, *, board_id: str | None = None) -> dict[str, Any]:
        running = self.is_running
        result: dict[str, Any] = {
            "active": self.max_concurrent_workers if running else 0,
            "idle": 0,
            "draining": 0,
            "running": running,
        }
        if board_id is not None:
            result.update(self.processor.get_dlq_drain_stats(board_id))
        return result


async def start_runner(runner: Any) -> Any:
    await runner.start()
    return runner


async def stop_runner(runner: Any) -> None:
    await runner.stop()


__all__ = [
    "ConsolidationRunner",
    "PollingRunner",
    "TrackedBlockingExecution",
    "UtcWorkerClock",
    "start_runner",
    "stop_runner",
]
