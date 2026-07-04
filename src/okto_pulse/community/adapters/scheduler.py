"""Community-owned SchedulerControl adapter."""

from __future__ import annotations

from datetime import timezone
from typing import Any, Callable, Mapping

from okto_pulse.core.ports.scheduler import (
    KG_DAILY_TICK_JOB_ID,
    JobSpec,
    SchedulerJobHandler,
    SchedulerJobSnapshot,
    SchedulerResult,
)


class SingletonSchedulerControl:
    """SchedulerControl backed by a Community-owned APScheduler instance."""

    def __init__(self, scheduler_factory: Callable[[], Any] | None = None) -> None:
        self._scheduler_factory = scheduler_factory
        self._scheduler: Any | None = None

    def _build_scheduler(self) -> Any:
        if self._scheduler_factory is not None:
            return self._scheduler_factory()

        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        return AsyncIOScheduler(timezone=timezone.utc)

    def _ensure_scheduler(self) -> Any:
        if self._scheduler is None:
            self._scheduler = self._build_scheduler()
        return self._scheduler

    def is_available(self) -> bool:
        return self._scheduler is not None

    async def register_job(
        self,
        job_spec: JobSpec,
        handler: SchedulerJobHandler,
    ) -> SchedulerResult:
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = self._ensure_scheduler()
        job_kwargs: dict[str, Any] = {}
        if job_spec.next_run_time is not None:
            job_kwargs["next_run_time"] = job_spec.next_run_time
        job = scheduler.add_job(
            handler,
            trigger=IntervalTrigger(
                minutes=job_spec.interval_minutes,
                timezone=timezone.utc,
            ),
            id=job_spec.job_id,
            replace_existing=job_spec.replace_existing,
            max_instances=job_spec.max_instances,
            coalesce=job_spec.coalesce,
            **job_kwargs,
        )
        if not getattr(scheduler, "running", False):
            scheduler.start()
        return SchedulerResult(
            job_id=job_spec.job_id,
            scheduled=True,
            next_run_time=getattr(job, "next_run_time", None),
            audit_status="rescheduled",
        )

    async def reschedule_job(
        self, job_id: str, trigger: Mapping[str, Any]
    ) -> SchedulerResult:
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = self._scheduler
        if scheduler is None:
            return SchedulerResult(
                job_id=job_id,
                scheduled=False,
                message="no_scheduler",
                audit_status="skipped",
            )
        minutes = int(trigger["minutes"])
        job = scheduler.reschedule_job(
            job_id,
            trigger=IntervalTrigger(minutes=minutes, timezone=timezone.utc),
        )
        return SchedulerResult(
            job_id=job_id,
            scheduled=True,
            next_run_time=getattr(job, "next_run_time", None),
            audit_status="rescheduled",
        )

    async def get_job_snapshot(self, job_id: str) -> SchedulerJobSnapshot:
        scheduler = self._scheduler
        if scheduler is None:
            return SchedulerJobSnapshot(
                job_id=job_id,
                exists=False,
                message="scheduler_unavailable",
            )
        job = scheduler.get_job(job_id)
        if job is None:
            return SchedulerJobSnapshot(
                job_id=job_id,
                exists=False,
                message="scheduler_job_unavailable",
            )
        return SchedulerJobSnapshot(
            job_id=job_id,
            exists=True,
            next_run_time=getattr(job, "next_run_time", None),
        )

    async def shutdown(self, wait: bool = False) -> None:
        scheduler = self._scheduler
        if scheduler is not None:
            scheduler.shutdown(wait=wait)
            self._scheduler = None


__all__ = ["SingletonSchedulerControl", "KG_DAILY_TICK_JOB_ID"]
