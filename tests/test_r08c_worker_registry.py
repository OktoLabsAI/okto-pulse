from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import workers as worker_adapters
from okto_pulse.community.adapters import sqlalchemy_database
from okto_pulse.community.adapters.workers import (
    COMMUNITY_WORKER_BASELINE_FAMILIES,
    build_community_worker_registry,
)
from okto_pulse.core.application.boundary import RuntimeWorkerBoundaryGate
from repo_layout import resolve_core_repo


def test_r08c_community_worker_registry_declares_baseline_families(monkeypatch) -> None:
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    monkeypatch.setattr(sqlalchemy_database, 'get_engine', lambda: engine)
    registry = build_community_worker_registry(object())

    assert registry.families == COMMUNITY_WORKER_BASELINE_FAMILIES
    assert registry.active_families == ()


@pytest.mark.asyncio
async def test_file_backed_readiness_starts_two_workers_and_drains_pool(
    tmp_path, monkeypatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.db'}")
    monkeypatch.setattr(sqlalchemy_database, 'get_engine', lambda: engine)
    events = []

    class FakePool:
        def __init__(self, *, max_workers, mp_context):
            assert max_workers == 2 and mp_context.get_start_method() == 'spawn'

        def shutdown(self, *, wait, cancel_futures):
            assert wait and cancel_futures
            events.append('pool:closed')

    async def start(runner):
        events.append(f'start:{runner.name}')
        return runner

    async def stop(runner, timeout=10.0):
        assert timeout == 45.0
        events.append(f'stop:{runner.name}')

    monkeypatch.setattr(worker_adapters, 'ProcessPoolExecutor', FakePool)
    monkeypatch.setattr(worker_adapters, 'start_runner', start)
    monkeypatch.setattr(worker_adapters.PollingRunner, 'stop', stop)
    registry = build_community_worker_registry(object())
    spec = registry._specs['readiness_projection_worker']
    primary = await spec.start()
    assert primary.processor.maintenance_enabled is True
    assert primary.processor.full_projection_executor is not None
    await spec.stop(primary)
    assert events == [
        'start:community.readiness_projection',
        'start:community.readiness_projection.secondary',
        'stop:community.readiness_projection',
        'stop:community.readiness_projection.secondary',
        'pool:closed',
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_r08c_community_worker_registry_preserves_shutdown_order(
    monkeypatch,
) -> None:
    events: list[str] = []
    consolidation_drained = False
    runners: dict[str, object] = {}
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    monkeypatch.setattr(sqlalchemy_database, 'get_engine', lambda: engine)

    def _family(runner) -> str:
        if isinstance(runner, worker_adapters.ConsolidationRunner):
            return "consolidation_worker"
        return {
            "community.event_dispatcher": "event_dispatcher",
            "community.kg.board_erasure_runner": "board_erasure_worker",
            "community.kg.cleanup_runner": "cleanup_worker",
            "community.kg.outbox_runner": "outbox_worker",
            "community.readiness_projection": "readiness_projection_worker",
        }[runner.name]

    async def _start(runner):
        family = _family(runner)
        events.append(f"start:{family}")
        runner.family = family
        runners[family] = runner
        return runner

    async def _stop(runner) -> None:
        nonlocal consolidation_drained
        if runner.family == "outbox_worker":
            # The real PollingRunner.stop performs the outbox final iteration.
            assert consolidation_drained is True
        events.append(f"stop:{runner.family}")
        if runner.family == "consolidation_worker":
            consolidation_drained = True

    monkeypatch.setattr(worker_adapters, "start_runner", _start)
    monkeypatch.setattr(worker_adapters, "stop_runner", _stop)
    async def _readiness_stop(runner, timeout=10.0):
        assert timeout == 45.0
        await _stop(runner)
    monkeypatch.setattr(worker_adapters.PollingRunner, 'stop', _readiness_stop)

    registry = worker_adapters.build_community_worker_registry(object())

    await registry.start_all()
    failures = await registry.stop_all()

    assert failures == ()
    consolidation_runner = runners["consolidation_worker"]
    outbox_runner = runners["outbox_worker"]
    assert (
        consolidation_runner._blocking_execution
        is not outbox_runner._blocking_execution
    )
    assert (
        outbox_runner._blocking_execution is outbox_runner.processor._blocking_execution
    )
    assert runners['readiness_projection_worker']._blocking_execution is not outbox_runner._blocking_execution
    assert events == [
        "start:readiness_projection_worker",
        "start:event_dispatcher",
        "start:board_erasure_worker",
        "start:cleanup_worker",
        "start:consolidation_worker",
        "start:outbox_worker",
        "stop:event_dispatcher",
        "stop:board_erasure_worker",
        "stop:consolidation_worker",
        "stop:readiness_projection_worker",
        "stop:outbox_worker",
        "stop:cleanup_worker",
    ]
    await engine.dispose()


def test_r08c_worker_boundary_real_core_and_community_trees_pass() -> None:
    community_root = Path(__file__).resolve().parents[1]
    core_root = resolve_core_repo(community_root)

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


@pytest.mark.asyncio(loop_scope="function")
async def test_worker_relational_scope_returns_checkout_after_hard_cancel(
    tmp_path,
) -> None:
    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
    )

    runtime = configure_community_database(
        f"sqlite+aiosqlite:///{tmp_path / 'worker-cancel.db'}"
    )
    scope_factory = worker_adapters._cancel_safe_scope_factory(runtime.session_factory)
    entered = asyncio.Event()

    async def victim() -> None:
        async with scope_factory() as session:
            await session.execute(text("SELECT 1"))
            entered.set()
            await asyncio.sleep(30)

    try:
        task = asyncio.create_task(victim(), name="test.cancelled-worker")
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert runtime.engine.sync_engine.pool.checkedout() == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if runtime.engine.sync_engine.pool.checkedout() == 0:
                break
            await asyncio.sleep(0.05)
        assert runtime.engine.sync_engine.pool.checkedout() == 0
    finally:
        await runtime.close()
