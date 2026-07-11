"""Community composition for application-scoped runtime runners."""

from __future__ import annotations

from typing import Any

from okto_pulse.core.application.processors import (
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

COMMUNITY_WORKER_BASELINE_FAMILIES: tuple[str, ...] = (
    "event_dispatcher",
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


def build_community_worker_registry(
    session_factory: Any,
    *,
    kg_cleanup_enabled: bool = True,
    kg_cleanup_interval_seconds: int = 60,
    kg_queue_heartbeat_seconds: int = 30,
    kg_queue_recovery_scan_interval_seconds: int = 60,
    kg_queue_max_concurrent_workers: int = 1,
) -> RuntimeWorkerRegistry:
    """Build all runners before app construction and register their lifecycle."""

    clock = UtcWorkerClock()
    blocking_execution = TrackedBlockingExecution()
    event_processor = DomainEventDeliveryProcessor(
        CommunitySqlAlchemyDomainEventDeliveryStore(session_factory),
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
    consolidation_processor = ConsolidationProcessor(
        session_factory=session_factory,
        heartbeat_seconds=kg_queue_heartbeat_seconds,
        clock=clock,
        blocking_execution=blocking_execution,
    )
    consolidation_runner = ConsolidationRunner(
        consolidation_processor,
        blocking_execution=blocking_execution,
        heartbeat_seconds=float(kg_queue_heartbeat_seconds),
        recovery_interval_seconds=float(kg_queue_recovery_scan_interval_seconds),
        max_concurrent_workers=kg_queue_max_concurrent_workers,
    )
    outbox_runner = PollingRunner(
        GlobalOutboxProcessor(session_factory, interval_seconds=5, clock=clock),
        name="community.kg.outbox_runner",
        interval_seconds=5.0,
        operation_name="process_once",
        final_iteration=True,
    )
    registry = RuntimeWorkerRegistry()
    registry.register(
        RuntimeWorkerSpec(
            family="event_dispatcher",
            start=lambda: start_runner(event_runner),
            stop=stop_runner,
            stop_priority=300,
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
