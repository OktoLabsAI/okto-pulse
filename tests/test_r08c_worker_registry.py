from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters import workers as worker_adapters
from okto_pulse.community.adapters.workers import (
    COMMUNITY_WORKER_BASELINE_FAMILIES,
    build_community_worker_registry,
)
from okto_pulse.core.application.boundary import RuntimeWorkerBoundaryGate


def test_r08c_community_worker_registry_declares_baseline_families() -> None:
    registry = build_community_worker_registry(object())

    assert registry.families == COMMUNITY_WORKER_BASELINE_FAMILIES
    assert registry.active_families == ()


@pytest.mark.asyncio
async def test_r08c_community_worker_registry_preserves_shutdown_order(
    monkeypatch,
) -> None:
    events: list[str] = []

    def _family(runner) -> str:
        if isinstance(runner, worker_adapters.ConsolidationRunner):
            return "consolidation_worker"
        return {
            "community.event_dispatcher": "event_dispatcher",
            "community.kg.cleanup_runner": "cleanup_worker",
            "community.kg.outbox_runner": "outbox_worker",
        }[runner.name]

    async def _start(runner):
        family = _family(runner)
        events.append(f"start:{family}")
        runner.family = family
        return runner

    async def _stop(runner) -> None:
        events.append(f"stop:{runner.family}")

    monkeypatch.setattr(worker_adapters, "start_runner", _start)
    monkeypatch.setattr(worker_adapters, "stop_runner", _stop)

    registry = worker_adapters.build_community_worker_registry(object())

    await registry.start_all()
    failures = await registry.stop_all()

    assert failures == ()
    assert events == [
        "start:event_dispatcher",
        "start:cleanup_worker",
        "start:consolidation_worker",
        "start:outbox_worker",
        "stop:event_dispatcher",
        "stop:outbox_worker",
        "stop:cleanup_worker",
        "stop:consolidation_worker",
    ]


def test_r08c_worker_boundary_real_core_and_community_trees_pass() -> None:
    community_root = Path(__file__).resolve().parents[1]
    core_root = community_root.parent / "okto_labs_pulse_core"

    report = RuntimeWorkerBoundaryGate().run(
        source_root=core_root,
        community_source_root=community_root,
    )

    assert report.status == "passed", report.as_dict()
    assert {
        "okto_pulse/core/application/processors/consolidation.py",
        "okto_pulse/community/main.py",
    } <= set(report.evidence["scanned_files"])
    assert report.evidence["offenders"] == []
