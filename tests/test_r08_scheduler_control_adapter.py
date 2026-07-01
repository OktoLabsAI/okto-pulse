"""R08 — Community owns the concrete SchedulerControl adapter."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.scheduler import SingletonSchedulerControl
from okto_pulse.core.kg.scheduler_singleton import (
    clear_scheduler_for_tests,
    set_scheduler,
)
from okto_pulse.core.ports.scheduler import KG_DAILY_TICK_JOB_ID, SchedulerControl


class _FakeScheduler:
    def __init__(self) -> None:
        self.reschedule_calls: list[tuple[str, object]] = []
        self.shutdown_calls: list[bool] = []
        self.next_run_time = datetime(2026, 6, 30, tzinfo=timezone.utc)

    def reschedule_job(self, job_id: str, *, trigger: object):
        self.reschedule_calls.append((job_id, trigger))
        return SimpleNamespace(next_run_time=self.next_run_time)

    def shutdown(self, *, wait: bool = False) -> None:
        self.shutdown_calls.append(wait)


def teardown_function() -> None:
    clear_scheduler_for_tests()


def test_community_scheduler_adapter_satisfies_core_port() -> None:
    assert isinstance(SingletonSchedulerControl(), SchedulerControl)


def test_core_no_longer_exposes_scheduler_control_concrete_module() -> None:
    assert importlib.util.find_spec("okto_pulse.core.services.scheduler_control_adapter") is None


@pytest.mark.asyncio
async def test_scheduler_adapter_skips_when_scheduler_absent() -> None:
    clear_scheduler_for_tests()
    control = SingletonSchedulerControl()

    assert control.is_available() is False
    result = await control.reschedule_job(KG_DAILY_TICK_JOB_ID, {"minutes": 5})
    await control.shutdown(wait=True)

    assert result.scheduled is False
    assert result.message == "no_scheduler"
    assert result.audit_status == "skipped"


@pytest.mark.asyncio
async def test_scheduler_adapter_preserves_reschedule_and_shutdown_behavior() -> None:
    scheduler = _FakeScheduler()
    set_scheduler(scheduler)
    control = SingletonSchedulerControl()

    assert control.is_available() is True
    result = await control.reschedule_job(KG_DAILY_TICK_JOB_ID, {"minutes": 7})
    await control.shutdown(wait=True)

    assert result.scheduled is True
    assert result.next_run_time == scheduler.next_run_time
    assert result.audit_status == "rescheduled"
    assert scheduler.reschedule_calls[0][0] == KG_DAILY_TICK_JOB_ID
    trigger = scheduler.reschedule_calls[0][1]
    assert trigger.__class__.__name__ == "IntervalTrigger"
    assert trigger.interval.total_seconds() == 420
    assert scheduler.shutdown_calls == [True]
