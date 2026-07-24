"""Community-owned asyncio runners for Core application processors."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from okto_pulse.core.ports.runtime_workers import (
    BlockingExecutionPort,
    WorkerDrainIncomplete,
)

logger = logging.getLogger("okto_pulse.community.workers")


class UtcWorkerClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class TrackedBlockingExecution(BlockingExecutionPort):
    """Offload blocking calls and retain them until application shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        # A blocking operation can outlive the coroutine that submitted it
        # when that parent is cancelled.  Only that detached case needs a
        # completion log: failures returned to an awaiting caller are already
        # observed and classified by the caller's normal error boundary.
        self._detached_tasks: set[asyncio.Task[Any]] = set()
        self._parent_observed_tasks: set[asyncio.Task[Any]] = set()

    def _consume_completed(self, task: asyncio.Task[Any]) -> None:
        """Retire a native operation and defer outcome classification.

        Task callbacks run before the coroutine awaiting ``shield(task)`` is
        necessarily resumed.  Deferring one loop turn lets ``run`` mark an
        ordinary propagated exception as observed, avoiding a duplicate and
        misleading ``blocking_operation_failed`` record.
        """
        self._tasks.discard(task)
        asyncio.get_running_loop().call_soon(self._consume_outcome, task)

    def _consume_outcome(self, task: asyncio.Task[Any]) -> None:
        """Observe the task result and log only an actually detached failure."""
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - background outcome boundary
            if task in self._detached_tasks and task not in self._parent_observed_tasks:
                logger.error(
                    "community.worker.blocking_operation_failed error_type=%s",
                    type(exc).__name__,
                    extra={
                        "event": "community.worker.blocking_operation_failed",
                        "error_type": type(exc).__name__,
                    },
                )
        finally:
            self._detached_tasks.discard(task)
            self._parent_observed_tasks.discard(task)

    async def run(self, operation: Callable[[], Any]) -> Any:
        task = asyncio.create_task(
            asyncio.to_thread(operation),
            name="community.worker.blocking_operation",
        )
        self._tasks.add(task)
        task.add_done_callback(self._consume_completed)
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # A native embedded-graph call may still own a process/durable
            # writer fence.  Never let parent cancellation detach it: callers
            # such as GlobalOutboxProcessor release their lease immediately
            # after this coroutine returns.  Drain through repeated
            # cancellation requests, observe the terminal native outcome, and
            # only then propagate the parent's cancellation.
            self._parent_observed_tasks.add(task)
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise
        except Exception:
            self._parent_observed_tasks.add(task)
            raise
        else:
            self._parent_observed_tasks.add(task)
            return result

    @property
    def pending_count(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    async def join(self, timeout: float) -> int:
        """Drain until the owned task set is stably empty or the deadline wins.

        A single snapshot is insufficient during shutdown: a cancellation-safe
        producer can finish one relational step and submit its final native
        durability call while an earlier batch is being joined. Two empty loop
        turns prove that all completion callbacks and immediately-following
        submissions had a chance to run.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        stable_empty_turns = 0
        while True:
            pending = {task for task in self._tasks if not task.done()}
            if not pending:
                stable_empty_turns += 1
                if stable_empty_turns >= 2:
                    return 0
                await asyncio.sleep(0)
                continue

            stable_empty_turns = 0
            remaining = deadline - loop.time()
            if remaining <= 0:
                return len(pending)
            await asyncio.wait(pending, timeout=remaining)


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
        blocking_execution: BlockingExecutionPort | None = None,
        final_iteration_guard: Callable[[], bool] | None = None,
    ) -> None:
        self.processor = processor
        self.name = name
        self.interval_seconds = interval_seconds
        self.operation_name = operation_name
        self._recover = recover
        self._final_iteration = final_iteration
        self._blocking_execution = blocking_execution
        self._final_iteration_guard = final_iteration_guard
        self._task: asyncio.Task[None] | None = None
        self._wake_event: asyncio.Event | None = None
        self._notify_loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def wake_event(self) -> asyncio.Event | None:
        return self._wake_event

    def notify(self) -> None:
        loop = self._notify_loop
        wake_event = self._wake_event
        if loop is None or wake_event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake_event.set)
        except RuntimeError:
            # The loop may close between the snapshot and scheduling.
            return

    async def start(self) -> "PollingRunner":
        if self.is_running:
            return self
        if self._recover is not None:
            await self._recover()
        self._notify_loop = asyncio.get_running_loop()
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
            final_iteration_allowed = (
                self._final_iteration_guard is None or self._final_iteration_guard()
            )
            if self._final_iteration and final_iteration_allowed:
                try:
                    await self.process_once()
                except Exception:
                    logger.exception(
                        "worker final iteration failed family=%s", self.name
                    )
            elif self._final_iteration:
                logger.critical(
                    "worker final iteration skipped family=%s "
                    "reason=upstream_native_drain_incomplete",
                    self.name,
                    extra={
                        "event": "community.worker.final_iteration_skipped",
                        "family": self.name,
                        "reason": "upstream_native_drain_incomplete",
                    },
                )
            raise

    async def stop(self, timeout: float = 10.0) -> None:
        self._running = False
        self.notify()
        loop = asyncio.get_running_loop()
        effective_timeout = max(0.0, float(timeout))
        deadline = loop.time() + effective_timeout
        task = self._task
        pending_tasks: set[asyncio.Task[None]] = set()
        task_error: Exception | None = None
        if task is not None and not task.done():
            task.cancel()
            _done, pending_tasks = await asyncio.wait(
                {task},
                timeout=max(0.0, deadline - loop.time()),
            )
        if task is not None and task.done():
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - re-raised after native drain
                task_error = exc

        blocking_pending = 0
        if self._blocking_execution is not None:
            blocking_pending = await self._blocking_execution.join(
                max(0.0, deadline - loop.time())
            )
        pending_tasks = {task for task in pending_tasks if not task.done()}
        if pending_tasks or blocking_pending:
            logger.critical(
                "worker native drain incomplete family=%s pending_tasks=%d "
                "pending_operations=%d timeout_s=%s",
                self.name,
                len(pending_tasks),
                blocking_pending,
                effective_timeout,
                extra={
                    "event": "community.worker.native_drain_incomplete",
                    "family": self.name,
                    "phase": "final_iteration_drain",
                    "pending_tasks": len(pending_tasks),
                    "pending_operations": blocking_pending,
                    "timeout_seconds": effective_timeout,
                },
            )
            raise WorkerDrainIncomplete(
                family=self.name,
                phase="final_iteration_drain",
                pending_tasks=len(pending_tasks),
                pending_operations=blocking_pending,
                timeout_seconds=effective_timeout,
            )
        self._task = None
        self._wake_event = None
        self._notify_loop = None
        if task_error is not None:
            raise task_error

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
        self._notify_loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._processing_task: asyncio.Task[None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._shutdown_drained = False

    @property
    def is_running(self) -> bool:
        # Runtime callers use this signal to decide whether notify() is enough
        # or they must execute one batch directly. A surviving recovery timer
        # cannot drain pending work, so it must never mask a dead processing
        # task as a healthy worker family.
        task = self._processing_task
        return self._running and task is not None and not task.done()

    @property
    def shutdown_drained(self) -> bool:
        """Whether the latest stop proved task and native quiescence."""

        return self._shutdown_drained

    @property
    def wake_event(self) -> asyncio.Event | None:
        return self._wake_event

    def notify(self) -> None:
        loop = self._notify_loop
        wake_event = self._wake_event
        if loop is None or wake_event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake_event.set)
        except RuntimeError:
            # The loop may close between the snapshot and scheduling.
            return

    async def start(self) -> "ConsolidationRunner":
        if self.is_running:
            return self
        dangling = {task for task in self._tasks if not task.done()}
        if dangling:
            self._running = False
            for task in dangling:
                task.cancel()
            await asyncio.gather(*dangling, return_exceptions=True)
        self._tasks.clear()
        self._shutdown_drained = False
        await self.processor.recover_stale_claims()
        self._notify_loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()
        self._running = True
        self._processing_task = asyncio.create_task(
            self.run_forever(),
            name="community.kg.consolidation_runner",
        )
        self._recovery_task = asyncio.create_task(
            self.run_recovery_forever(),
            name="community.kg.consolidation_recovery_runner",
        )
        self._tasks = {self._processing_task, self._recovery_task}
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
                    attempted = int(
                        getattr(self.processor, "last_attempted_count", processed)
                    )
                    if processed > 0 or attempted > 0:
                        # One bounded follow-up lets a same-board backlog move
                        # after the previous lease was ACKed/re-pended. If the
                        # only row is now in backoff, that follow-up claims zero
                        # and the runner sleeps normally (no busy cycle).
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
                try:
                    await self.processor.recover_stale_claims()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transient SQLite lock must not permanently kill stale
                    # claim recovery while the processing task remains alive.
                    logger.exception("consolidation stale-claim recovery failed")
        except asyncio.CancelledError:
            raise

    async def stop(self, timeout: float | None = None) -> None:
        self._shutdown_drained = False
        self._running = False
        self.notify()
        loop = asyncio.get_running_loop()
        effective_timeout = self.join_timeout if timeout is None else timeout
        effective_timeout = max(0.0, float(effective_timeout))
        deadline = loop.time() + effective_timeout
        tasks = {task for task in self._tasks if not task.done()}
        for task in tasks:
            task.cancel()
        pending: set[asyncio.Task[None]] = set()
        task_error: Exception | None = None
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=max(0.0, deadline - loop.time()),
            )
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 - re-raised after drain
                    task_error = task_error or exc
            if pending:
                logger.warning(
                    "consolidation runner join timed out pending=%d timeout_s=%s",
                    len(pending),
                    effective_timeout,
                )
        # Also observe tasks that reached a terminal state before this stop
        # attempt; otherwise a failed worker could become an unobserved task
        # when a later successful retry clears the retained handle set.
        for completed in {task for task in self._tasks if task.done()}:
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - re-raised after drain
                task_error = task_error or exc

        blocking_pending = await self._blocking_execution.join(
            max(0.0, deadline - loop.time())
        )
        pending = {task for task in pending if not task.done()}
        if blocking_pending:
            logger.warning(
                "consolidation blocking join timed out pending=%d timeout_s=%s",
                blocking_pending,
                effective_timeout,
            )
        if pending or blocking_pending:
            logger.critical(
                "community.worker.native_drain_incomplete family=%s "
                "pending_tasks=%d pending_operations=%d timeout_s=%s",
                "community.kg.consolidation_runner",
                len(pending),
                blocking_pending,
                effective_timeout,
                extra={
                    "event": "community.worker.native_drain_incomplete",
                    "family": "community.kg.consolidation_runner",
                    "phase": "consolidation_drain",
                    "pending_tasks": len(pending),
                    "pending_operations": blocking_pending,
                    "timeout_seconds": effective_timeout,
                },
            )
            # Preserve every task and handle for a bounded retry/diagnostic.
            # Clearing them here would let graph/DB teardown race a live native
            # durability operation.
            raise WorkerDrainIncomplete(
                family="community.kg.consolidation_runner",
                phase="consolidation_drain",
                pending_tasks=len(pending),
                pending_operations=blocking_pending,
                timeout_seconds=effective_timeout,
            )
        self._tasks.clear()
        self._processing_task = None
        self._recovery_task = None
        self._wake_event = None
        self._notify_loop = None
        self._shutdown_drained = True
        if task_error is not None:
            raise task_error

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
