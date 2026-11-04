"""Community composition for application-scoped runtime runners."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from okto_pulse.core.application.processors import (
    BoardErasureProcessor,
    ConsolidationProcessor,
    GlobalOutboxProcessor,
    SessionCleanupProcessor,
)
from okto_pulse.core.application.domain_event_delivery import (
    DomainEventDeliveryProcessor,
)
from okto_pulse.core.ports.runtime_workers import (
    RuntimeWorkerRegistry,
    RuntimeWorkerSpec,
)
from okto_pulse.community.adapters.worker_runners import (
    ConsolidationRunner,
    PollingRunner,
    TrackedBlockingExecution,
    UtcWorkerClock,
    start_runner,
    stop_runner,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventDeliveryStore,
)
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor

COMMUNITY_WORKER_BASELINE_FAMILIES: tuple[str, ...] = (
    "readiness_projection_worker",
    "event_dispatcher",
    "board_erasure_worker",
    "cleanup_worker",
    "consolidation_worker",
    "outbox_worker",
)

COMMUNITY_WORKER_CAPABLE_FAMILIES: tuple[str, ...] = (
    *COMMUNITY_WORKER_BASELINE_FAMILIES,
    "daily_tick",
    "cognitive_closeout_worker",
    "schema_sweep",
)


def _cancel_safe_scope_factory(session_factory: Any):
    def open_scope():
        from okto_pulse.community.adapters.sqlalchemy_database import (
            resolve_community_database_runtime,
        )

        return resolve_community_database_runtime().cancel_safe_session_scope(
            session_factory
        )

    return open_scope


def build_community_worker_registry(
    session_factory: Any,
    *,
    kg_cleanup_enabled: bool = True,
    kg_cleanup_interval_seconds: int = 60,
    kg_queue_heartbeat_seconds: int = 30,
    kg_queue_recovery_scan_interval_seconds: int = 60,
    kg_queue_max_concurrent_workers: int = 1,
    evidence_root: Path | None = None,
) -> RuntimeWorkerRegistry:
    """Build all runners before app construction and register their lifecycle."""

    cancel_safe_relational_scope = _cancel_safe_scope_factory(session_factory)
    clock = UtcWorkerClock()
    # Keep shutdown ownership disjoint. Consolidation must prove its own native
    # operations quiescent before the outbox performs its final iteration; a
    # shared task set could otherwise miss late submissions from either family.
    consolidation_blocking_execution = TrackedBlockingExecution()
    outbox_blocking_execution = TrackedBlockingExecution()
    readiness_blocking_execution = TrackedBlockingExecution()
    from okto_pulse.community.adapters.sqlalchemy_database import get_engine
    readiness_engine = get_engine()
    database = readiness_engine.url.database
    file_backed = bool(database and database != ':memory:'
        and readiness_engine.url.query.get('uri') != 'true')
    # TR03/AC12/AC23 require at most one concurrent calculation.
    readiness_pool = (ProcessPoolExecutor(max_workers=1, mp_context=get_context('spawn'))
        if file_backed else None)
    database_path = str(Path(database).resolve()) if file_backed else None
    readiness_runner = PollingRunner(
        ReadinessProjectionProcessor(readiness_engine, readiness_blocking_execution,
            full_projection_executor=readiness_pool, database_path=database_path,
            evidence_root=evidence_root,
            max_inflight=2 if file_backed else 1),
        name="community.readiness_projection",
        interval_seconds=0.1,
        active_interval_seconds=0.0,
        operation_name="process_once",
        blocking_execution=readiness_blocking_execution,
    )
    readiness_runners = [readiness_runner]

    async def start_readiness():
        started = []
        try:
            for runner in readiness_runners:
                await start_runner(runner)
                started.append(runner)
        except BaseException:
            for runner in reversed(started):
                await runner.stop(timeout=45.0)
            if readiness_pool is not None:
                await asyncio.to_thread(readiness_pool.shutdown, wait=True,
                    cancel_futures=True)
            raise
        return readiness_runner

    async def stop_readiness(_runner):
        outcomes = await asyncio.gather(
            *(runner.stop(timeout=45.0) for runner in readiness_runners),
            return_exceptions=True)
        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        if failures:
            raise failures[0]
        if readiness_pool is not None:
            await asyncio.to_thread(readiness_pool.shutdown, wait=True,
                cancel_futures=True)

    event_processor = DomainEventDeliveryProcessor(
        CommunitySqlAlchemyDomainEventDeliveryStore(
            session_factory,
            session_scope_factory=cancel_safe_relational_scope,
        ),
        clock=clock.now,
    )
    event_runner = PollingRunner(
        event_processor,
        name="community.event_dispatcher",
        interval_seconds=5.0,
        operation_name="process_batch",
        recover=event_processor.recover_orphans,
    )
    cleanup_runner = PollingRunner(
        SessionCleanupProcessor(
            interval_seconds=kg_cleanup_interval_seconds,
            clock=clock,
        ),
        name="community.kg.cleanup_runner",
        interval_seconds=float(kg_cleanup_interval_seconds),
        operation_name="process_once",
        final_iteration=True,
    )
    board_erasure_runner = PollingRunner(
        BoardErasureProcessor(
            cancel_safe_relational_scope,
            clock=clock,
        ),
        name="community.kg.board_erasure_runner",
        interval_seconds=5.0,
        operation_name="process_once",
        final_iteration=True,
    )
    consolidation_processor = ConsolidationProcessor(
        relational_scope_factory=cancel_safe_relational_scope,
        heartbeat_seconds=kg_queue_heartbeat_seconds,
        clock=clock,
        blocking_execution=consolidation_blocking_execution,
        durable_projection_protocol=True,
    )
    consolidation_runner = ConsolidationRunner(
        consolidation_processor,
        blocking_execution=consolidation_blocking_execution,
        heartbeat_seconds=float(kg_queue_heartbeat_seconds),
        recovery_interval_seconds=float(kg_queue_recovery_scan_interval_seconds),
        max_concurrent_workers=kg_queue_max_concurrent_workers,
    )
    outbox_runner = PollingRunner(
        GlobalOutboxProcessor(
            cancel_safe_relational_scope,
            interval_seconds=5,
            clock=clock,
            blocking_execution=outbox_blocking_execution,
        ),
        name="community.kg.outbox_runner",
        interval_seconds=5.0,
        operation_name="process_once",
        final_iteration=True,
        blocking_execution=outbox_blocking_execution,
        final_iteration_guard=lambda: consolidation_runner.shutdown_drained,
    )
    registry = RuntimeWorkerRegistry()
    registry.register(
        RuntimeWorkerSpec(
            family="readiness_projection_worker",
            start=start_readiness,
            stop=stop_readiness,
            stop_priority=225,
        )
    )
    registry.register(
        RuntimeWorkerSpec(
            family="event_dispatcher",
            start=lambda: start_runner(event_runner),
            stop=stop_runner,
            stop_priority=300,
        )
    )
    registry.register(
        RuntimeWorkerSpec(
            family="board_erasure_worker",
            start=lambda: start_runner(board_erasure_runner),
            stop=stop_runner,
            stop_priority=275,
        )
    )
    if kg_cleanup_enabled:
        registry.register(
            RuntimeWorkerSpec(
                family="cleanup_worker",
                start=lambda: start_runner(cleanup_runner),
                stop=stop_runner,
                stop_priority=100,
            )
        )
    registry.register(
        RuntimeWorkerSpec(
            family="consolidation_worker",
            start=lambda: start_runner(consolidation_runner),
            stop=stop_runner,
            # Join every in-flight consolidation/native graph write before
            # the outbox runner performs its shutdown final iteration.
            stop_priority=250,
        )
    )
    registry.register(
        RuntimeWorkerSpec(
            family="outbox_worker",
            start=lambda: start_runner(outbox_runner),
            stop=stop_runner,
            stop_priority=200,
        )
    )
    return registry


__all__ = [
    "COMMUNITY_WORKER_BASELINE_FAMILIES",
    "COMMUNITY_WORKER_CAPABLE_FAMILIES",
    "build_community_worker_registry",
]
