"""Community-owned SchedulerControl adapter.

R08 moves the concrete scheduler bridge out of Core. The remaining process-wide
scheduler singleton is still the registered runtime source until the R08B
lifecycle cleanup; this adapter keeps that access edition-owned and lazy.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any, Mapping

from okto_pulse.core.ports.scheduler import (
    KG_DAILY_TICK_JOB_ID,
    SchedulerResult,
)


class SingletonSchedulerControl:
    """SchedulerControl backed by the registered APScheduler singleton."""

    def is_available(self) -> bool:
        from okto_pulse.core.kg.scheduler_singleton import get_scheduler

        return get_scheduler() is not None

    async def reschedule_job(
        self, job_id: str, trigger: Mapping[str, Any]
    ) -> SchedulerResult:
        from apscheduler.triggers.interval import IntervalTrigger

        from okto_pulse.core.kg.scheduler_singleton import get_scheduler

        scheduler = get_scheduler()
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

    async def shutdown(self, wait: bool = False) -> None:
        from okto_pulse.core.kg.scheduler_singleton import get_scheduler

        scheduler = get_scheduler()
        if scheduler is not None:
            scheduler.shutdown(wait=wait)


__all__ = ["SingletonSchedulerControl", "KG_DAILY_TICK_JOB_ID"]
