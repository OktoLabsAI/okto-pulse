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

    class _Worker:
        def __init__(self, family: str) -> None:
            self.family = family

    async def _start(family: str) -> _Worker:
        events.append(f"start:{family}")
        return _Worker(family)

    async def _stop(worker: _Worker) -> None:
        events.append(f"stop:{worker.family}")

    monkeypatch.setattr(
        worker_adapters,
        "_start_event_dispatcher",
        lambda _session_factory: _start("event_dispatcher"),
    )
    monkeypatch.setattr(
        worker_adapters,
        "_start_cleanup_worker",
        lambda: _start("cleanup_worker"),
    )
    monkeypatch.setattr(
        worker_adapters,
        "_start_consolidation_worker",
        lambda _session_factory: _start("consolidation_worker"),
    )
    monkeypatch.setattr(
        worker_adapters,
        "_start_outbox_worker",
        lambda _session_factory: _start("outbox_worker"),
    )
    monkeypatch.setattr(worker_adapters, "_stop_event_dispatcher", _stop)
    monkeypatch.setattr(worker_adapters, "_stop_simple_worker", _stop)

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
        "okto_pulse/core/app.py",
        "okto_pulse/community/main.py",
    } <= set(report.evidence["scanned_files"])
    assert report.evidence["offenders"] == []
