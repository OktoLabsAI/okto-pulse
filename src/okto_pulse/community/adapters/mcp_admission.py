"""Bounded, fair admission control for Community MCP tool calls.

The API/UI and MCP listeners intentionally share one asyncio loop so embedded
SQLite and Ladybug ownership stays in one process.  This controller bounds only
MCP tool execution.  Session transport, streaming, resources, prompts and REST
requests never enter this queue.

Admission bounds competing calls; it cannot preempt synchronous CPU inside one
Core handler.  Community therefore also offloads its known response projection
and trace serialization hotspots, while Core owns handler-level yielding.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import time
from typing import Any


class McpWorkClass(str, Enum):
    """Execution lanes understood by the Community admission controller."""

    READER = "reader"
    WRITER = "writer"


def classify_mcp_tool(
    tool_name: object,
    admission_classes: Mapping[str, object] | None = None,
) -> McpWorkClass:
    """Consume Core catalog metadata, failing closed for missing/invalid values."""

    raw_class = (
        admission_classes.get(str(tool_name or ""))
        if admission_classes is not None
        else None
    )
    raw_value = getattr(raw_class, "value", raw_class)
    try:
        return McpWorkClass(raw_value)
    except (TypeError, ValueError):
        return McpWorkClass.WRITER


@dataclass(frozen=True, slots=True)
class McpAdmissionPolicy:
    """Closed QoS limits for one materialized MCP host."""

    max_active: int = 4
    max_active_per_session: int = 2
    max_active_writers: int = 1
    max_queued: int = 16
    max_queued_per_session: int = 4
    wait_timeout_ms: int = 250
    retry_after_ms: int = 500

    def __post_init__(self) -> None:
        positive = {
            "max_active": self.max_active,
            "max_active_per_session": self.max_active_per_session,
            "max_active_writers": self.max_active_writers,
            "retry_after_ms": self.retry_after_ms,
        }
        for name, value in positive.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        non_negative = {
            "max_queued": self.max_queued,
            "max_queued_per_session": self.max_queued_per_session,
            "wait_timeout_ms": self.wait_timeout_ms,
        }
        for name, value in non_negative.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_active_per_session > self.max_active:
            raise ValueError("max_active_per_session cannot exceed max_active")
        if self.max_active_writers != 1:
            raise ValueError("max_active_writers must remain exactly 1")
        if self.max_queued_per_session > self.max_queued:
            raise ValueError("max_queued_per_session cannot exceed max_queued")

    @classmethod
    def from_settings(cls, settings: Any) -> "McpAdmissionPolicy":
        return cls(
            max_active=int(settings.mcp_admission_max_active),
            max_active_per_session=int(
                settings.mcp_admission_max_active_per_session
            ),
            max_active_writers=int(
                getattr(settings, "mcp_admission_max_active_writers", 1)
            ),
            max_queued=int(settings.mcp_admission_max_queued),
            max_queued_per_session=int(
                settings.mcp_admission_max_queued_per_session
            ),
            wait_timeout_ms=int(settings.mcp_admission_wait_timeout_ms),
            retry_after_ms=int(settings.mcp_admission_retry_after_ms),
        )


class McpAdmissionRejected(RuntimeError):
    """Retryable overload result carrying only bounded operational metadata."""

    def __init__(
        self,
        reason: str,
        *,
        retry_after_ms: int,
        snapshot: dict[str, Any],
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_ms = retry_after_ms
        self.snapshot = snapshot


@dataclass(slots=True)
class _Waiter:
    session_id: str
    work_class: McpWorkClass
    enqueued_at: float
    future: asyncio.Future[McpAdmissionLease]
    granted: bool = False


class McpAdmissionLease:
    """One active slot; release is asynchronous and idempotent."""

    __slots__ = (
        "_controller",
        "_handler_started",
        "_released",
        "session_id",
        "waited_ms",
        "work_class",
    )

    def __init__(
        self,
        controller: "McpAdmissionController",
        *,
        session_id: str,
        work_class: McpWorkClass,
        waited_ms: float,
    ) -> None:
        self._controller = controller
        self._handler_started = False
        self._released = False
        self.session_id = session_id
        self.work_class = work_class
        self.waited_ms = waited_ms

    async def release(self, *, cancelled: bool = False) -> None:
        await self._controller.release(self, cancelled=cancelled)

    def mark_handler_started(self) -> None:
        """Record the distinct boundary where middleware enters Core."""

        self._controller.mark_handler_started(self)

    async def release_cancel_safe(self, *, cancelled: bool = False) -> None:
        """Finish accounting even if the request receives another cancel."""

        release_task = asyncio.create_task(
            self.release(cancelled=cancelled),
            name="community.mcp.admission.release",
        )
        postponed_cancellation: asyncio.CancelledError | None = None
        while not release_task.done():
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError as exc:
                # Shield keeps the release alive.  Delay propagation only until
                # its bounded lock section restores controller invariants.
                postponed_cancellation = exc
        release_task.result()
        if postponed_cancellation is not None:
            raise postponed_cancellation


class McpAdmissionController:
    """FIFO admission with bounded per-session shares and round-robin release."""

    def __init__(self, policy: McpAdmissionPolicy) -> None:
        self.policy = policy
        self._lock = asyncio.Lock()
        self._waiters: deque[_Waiter] = deque()
        self._active = 0
        self._active_writers = 0
        self._queued_writers = 0
        self._active_by_session: defaultdict[str, int] = defaultdict(int)
        self._queued_by_session: defaultdict[str, int] = defaultdict(int)
        self._last_granted_session: str | None = None
        self._granted_total = 0
        self._admitted_to_handler_total = 0
        self._completed_total = 0
        self._cancelled_total = 0
        self._queue_cancelled_total = 0
        self._waited_total = 0
        self._wait_ms_total = 0.0
        self._wait_ms_max = 0.0
        self._rejections: Counter[str] = Counter()

    @staticmethod
    def _session_id(value: object) -> str:
        resolved = str(value or "anon").strip()
        return resolved[:256] or "anon"

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "active_writers": self._active_writers,
            "queued": len(self._waiters),
            "queued_writers": self._queued_writers,
            "max_active": self.policy.max_active,
            "max_active_writers": self.policy.max_active_writers,
            "max_queued": self.policy.max_queued,
            # ``accepted_total`` remains the compatibility name for a granted
            # lease.  The two explicit gauges avoid conflating a grant that is
            # cancelled/times out before Core with handler execution.
            "accepted_total": self._granted_total,
            "granted_total": self._granted_total,
            "admitted_to_handler_total": self._admitted_to_handler_total,
            "completed_total": self._completed_total,
            "cancelled_total": self._cancelled_total,
            "queue_cancelled_total": self._queue_cancelled_total,
            "waited_total": self._waited_total,
            "wait_ms_total": round(self._wait_ms_total, 3),
            "wait_ms_max": round(self._wait_ms_max, 3),
            "rejected_total": sum(self._rejections.values()),
            "rejections_by_reason": dict(sorted(self._rejections.items())),
            "accounting_consistent": self._granted_total
            == self._completed_total + self._cancelled_total + self._active,
        }

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return self._snapshot_locked()

    def _can_admit_session_locked(
        self,
        session_id: str,
        work_class: McpWorkClass,
    ) -> bool:
        # The writer lane is checked before global capacity.  A writer waiting
        # for serialization therefore never reserves one of the total slots.
        if (
            work_class is McpWorkClass.WRITER
            and self._active_writers >= self.policy.max_active_writers
        ):
            return False
        if self._active >= self.policy.max_active:
            return False
        return (
            self._active_by_session[session_id]
            < self.policy.max_active_per_session
        )

    def _new_lease_locked(
        self,
        session_id: str,
        *,
        work_class: McpWorkClass,
        enqueued_at: float | None,
    ) -> McpAdmissionLease:
        waited_ms = (
            0.0
            if enqueued_at is None
            else max(0.0, (time.perf_counter() - enqueued_at) * 1000)
        )
        # Reserve the exclusive writer lane first and global capacity second.
        # No await occurs between them, so the pair is one atomic grant under
        # ``_lock`` while preserving the no-global-slot-while-waiting invariant.
        if work_class is McpWorkClass.WRITER:
            self._active_writers += 1
        self._active += 1
        self._active_by_session[session_id] += 1
        self._last_granted_session = session_id
        self._granted_total += 1
        if enqueued_at is not None:
            self._waited_total += 1
            self._wait_ms_total += waited_ms
            self._wait_ms_max = max(self._wait_ms_max, waited_ms)
        return McpAdmissionLease(
            self,
            session_id=session_id,
            work_class=work_class,
            waited_ms=waited_ms,
        )

    def _remove_waiter_locked(self, waiter: _Waiter) -> bool:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return False
        self._queued_by_session[waiter.session_id] -= 1
        if self._queued_by_session[waiter.session_id] <= 0:
            self._queued_by_session.pop(waiter.session_id, None)
        if waiter.work_class is McpWorkClass.WRITER:
            self._queued_writers -= 1
        return True

    def _next_eligible_waiter_locked(self) -> _Waiter | None:
        eligible = [
            waiter
            for waiter in self._waiters
            if self._can_admit_session_locked(
                waiter.session_id,
                waiter.work_class,
            )
        ]
        if not eligible:
            return None
        return next(
            (
                waiter
                for waiter in eligible
                if waiter.session_id != self._last_granted_session
            ),
            eligible[0],
        )

    def _dispatch_locked(self) -> None:
        while self._active < self.policy.max_active:
            waiter = self._next_eligible_waiter_locked()
            if waiter is None:
                return
            self._remove_waiter_locked(waiter)
            waiter.granted = True
            lease = self._new_lease_locked(
                waiter.session_id,
                work_class=waiter.work_class,
                enqueued_at=waiter.enqueued_at,
            )
            if not waiter.future.done():
                waiter.future.set_result(lease)

    def _release_locked(
        self,
        lease: McpAdmissionLease,
        *,
        cancelled: bool,
    ) -> bool:
        if lease._released:
            return False
        lease._released = True
        if lease.work_class is McpWorkClass.WRITER:
            self._active_writers -= 1
        self._active -= 1
        self._active_by_session[lease.session_id] -= 1
        if self._active_by_session[lease.session_id] <= 0:
            self._active_by_session.pop(lease.session_id, None)
        if cancelled:
            self._cancelled_total += 1
        else:
            self._completed_total += 1
        return True

    def _rejection_locked(self, reason: str) -> McpAdmissionRejected:
        self._rejections[reason] += 1
        return McpAdmissionRejected(
            reason,
            retry_after_ms=self.policy.retry_after_ms,
            snapshot=self._snapshot_locked(),
        )

    def mark_handler_started(self, lease: McpAdmissionLease) -> None:
        """Count handler entry once; called synchronously on the owner loop."""

        if lease._controller is not self:
            raise ValueError("admission lease belongs to another controller")
        if lease._released or lease._handler_started:
            return
        lease._handler_started = True
        self._admitted_to_handler_total += 1

    async def _cancel_waiter(self, waiter: _Waiter) -> None:
        async with self._lock:
            if waiter.granted and waiter.future.done():
                self._release_locked(
                    waiter.future.result(),
                    cancelled=True,
                )
            elif self._remove_waiter_locked(waiter):
                self._queue_cancelled_total += 1
            self._dispatch_locked()

    async def _timeout_waiter(
        self,
        waiter: _Waiter,
    ) -> McpAdmissionRejected:
        async with self._lock:
            if waiter.granted and waiter.future.done():
                self._release_locked(
                    waiter.future.result(),
                    cancelled=True,
                )
            else:
                self._remove_waiter_locked(waiter)
            rejection = self._rejection_locked("wait_timeout")
            self._dispatch_locked()
            return rejection

    @staticmethod
    async def _finish_cleanup_despite_cancellation(
        cleanup_task: asyncio.Task[Any],
    ) -> Any:
        postponed_cancellation: asyncio.CancelledError | None = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                postponed_cancellation = exc
        result = cleanup_task.result()
        if postponed_cancellation is not None:
            raise postponed_cancellation
        return result

    async def acquire(
        self,
        session_id: object,
        *,
        work_class: McpWorkClass = McpWorkClass.WRITER,
    ) -> McpAdmissionLease:
        resolved_session = self._session_id(session_id)
        try:
            resolved_work_class = McpWorkClass(work_class)
        except ValueError as exc:
            raise ValueError(f"unsupported MCP work class: {work_class!r}") from exc
        waiter: _Waiter | None = None
        async with self._lock:
            if not self._waiters and self._can_admit_session_locked(
                resolved_session,
                resolved_work_class,
            ):
                return self._new_lease_locked(
                    resolved_session,
                    work_class=resolved_work_class,
                    enqueued_at=None,
                )
            if len(self._waiters) >= self.policy.max_queued:
                raise self._rejection_locked("queue_full")
            if (
                self._queued_by_session[resolved_session]
                >= self.policy.max_queued_per_session
            ):
                raise self._rejection_locked("session_queue_full")
            if self.policy.wait_timeout_ms == 0:
                raise self._rejection_locked("wait_timeout")

            waiter = _Waiter(
                session_id=resolved_session,
                work_class=resolved_work_class,
                enqueued_at=time.perf_counter(),
                future=asyncio.get_running_loop().create_future(),
            )
            self._waiters.append(waiter)
            self._queued_by_session[resolved_session] += 1
            if resolved_work_class is McpWorkClass.WRITER:
                self._queued_writers += 1
            self._dispatch_locked()

        try:
            return await asyncio.wait_for(
                asyncio.shield(waiter.future),
                timeout=self.policy.wait_timeout_ms / 1000,
            )
        except TimeoutError:
            cleanup_task = asyncio.create_task(
                self._timeout_waiter(waiter),
                name="community.mcp.admission.timeout_waiter",
            )
            rejection = await self._finish_cleanup_despite_cancellation(
                cleanup_task
            )
            raise rejection from None
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(
                self._cancel_waiter(waiter),
                name="community.mcp.admission.cancel_waiter",
            )
            await self._finish_cleanup_despite_cancellation(cleanup_task)
            raise

    async def release(
        self,
        lease: McpAdmissionLease,
        *,
        cancelled: bool = False,
    ) -> None:
        async with self._lock:
            if self._release_locked(lease, cancelled=cancelled):
                self._dispatch_locked()


__all__ = [
    "McpAdmissionController",
    "McpAdmissionLease",
    "McpAdmissionPolicy",
    "McpAdmissionRejected",
    "McpWorkClass",
    "classify_mcp_tool",
]
