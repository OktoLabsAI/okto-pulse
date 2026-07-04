"""R08 — Community owns the concrete SchedulerControl adapter."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.scheduler import SingletonSchedulerControl
from okto_pulse.core.ports.scheduler import (
    KG_DAILY_TICK_JOB_ID,
    JobSpec,
    SchedulerControl,
)


class _FakeScheduler:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, object]] = []
        self.reschedule_calls: list[tuple[str, object]] = []
        self.shutdown_calls: list[bool] = []
        self.start_calls = 0
        self.running = False
        self.next_run_time = datetime(2026, 6, 30, tzinfo=timezone.utc)
        self.jobs: dict[str, SimpleNamespace] = {}

    def add_job(self, handler, **kwargs):
        self.add_calls.append({"handler": handler, **kwargs})
        job = SimpleNamespace(next_run_time=self.next_run_time)
        self.jobs[str(kwargs["id"])] = job
        return job

    def start(self) -> None:
        self.start_calls += 1
        self.running = True

    def reschedule_job(self, job_id: str, *, trigger: object):
        self.reschedule_calls.append((job_id, trigger))
        job = SimpleNamespace(next_run_time=self.next_run_time)
        self.jobs[job_id] = job
        return job

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def shutdown(self, *, wait: bool = False) -> None:
        self.shutdown_calls.append(wait)

def test_community_scheduler_adapter_satisfies_core_port() -> None:
    assert isinstance(SingletonSchedulerControl(), SchedulerControl)


def test_core_no_longer_exposes_scheduler_control_concrete_module() -> None:
    assert importlib.util.find_spec("okto_pulse.core.services.scheduler_control_adapter") is None


@pytest.mark.asyncio
async def test_scheduler_adapter_skips_when_scheduler_absent() -> None:
    control = SingletonSchedulerControl()

    assert control.is_available() is False
    result = await control.reschedule_job(KG_DAILY_TICK_JOB_ID, {"minutes": 5})
    await control.shutdown(wait=True)

    assert result.scheduled is False
    assert result.message == "no_scheduler"
    assert result.audit_status == "skipped"


@pytest.mark.asyncio
async def test_scheduler_adapter_registers_jobspec_and_preserves_reschedule() -> None:
    scheduler = _FakeScheduler()
    control = SingletonSchedulerControl(scheduler_factory=lambda: scheduler)
    next_run = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    job_spec = JobSpec(
        job_id=KG_DAILY_TICK_JOB_ID,
        interval_minutes=7,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=next_run,
    )

    assert control.is_available() is False
    register_result = await control.register_job(job_spec, lambda: None)
    assert register_result.scheduled is True
    assert control.is_available() is True
    assert scheduler.start_calls == 1
    add_call = scheduler.add_calls[0]
    assert add_call["id"] == KG_DAILY_TICK_JOB_ID
    assert add_call["replace_existing"] is True
    assert add_call["max_instances"] == 1
    assert add_call["coalesce"] is True
    assert add_call["next_run_time"] == next_run
    trigger = add_call["trigger"]
    assert trigger.__class__.__name__ == "IntervalTrigger"
    assert trigger.interval.total_seconds() == 420
    snapshot = await control.get_job_snapshot(KG_DAILY_TICK_JOB_ID)
    assert snapshot.exists is True
    assert snapshot.next_run_time == scheduler.next_run_time

    result = await control.reschedule_job(KG_DAILY_TICK_JOB_ID, {"minutes": 7})
    await control.shutdown(wait=True)

    assert result.scheduled is True
    assert result.next_run_time == scheduler.next_run_time
    assert result.audit_status == "rescheduled"
    assert scheduler.reschedule_calls[0][0] == KG_DAILY_TICK_JOB_ID
    reschedule_trigger = scheduler.reschedule_calls[0][1]
    assert reschedule_trigger.__class__.__name__ == "IntervalTrigger"
    assert reschedule_trigger.interval.total_seconds() == 420
    assert scheduler.shutdown_calls == [True]
    assert control.is_available() is False
