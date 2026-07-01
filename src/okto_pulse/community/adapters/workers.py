"""Community-owned runtime worker registry."""

from __future__ import annotations

from typing import Any

from okto_pulse.core.ports.runtime_workers import (
    RuntimeWorkerRegistry,
    RuntimeWorkerSpec,
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
) -> RuntimeWorkerRegistry:
    """Build the Community runtime worker registry for combined_lifespan."""

    registry = RuntimeWorkerRegistry()
    registry.register(
        RuntimeWorkerSpec(
            family="event_dispatcher",
            start=lambda: _start_event_dispatcher(session_factory),
            stop=_stop_event_dispatcher,
            stop_priority=300,
        )
    )
    if kg_cleanup_enabled:
        registry.register(
            RuntimeWorkerSpec(
                family="cleanup_worker",
                start=_start_cleanup_worker,
                stop=_stop_simple_worker,
                stop_priority=100,
            )
        )
    registry.register(
        RuntimeWorkerSpec(
            family="consolidation_worker",
            start=lambda: _start_consolidation_worker(session_factory),
            stop=_stop_simple_worker,
        )
    )
    registry.register(
        RuntimeWorkerSpec(
            family="outbox_worker",
            start=lambda: _start_outbox_worker(session_factory),
            stop=_stop_simple_worker,
            stop_priority=200,
        )
    )
    return registry


async def _start_event_dispatcher(session_factory: Any) -> Any:
    from okto_pulse.core import events as _events  # noqa: F401
    from okto_pulse.core.events.dispatcher import EventDispatcher, set_dispatcher

    dispatcher = EventDispatcher(session_factory)
    await dispatcher.start()
    set_dispatcher(dispatcher)
    return dispatcher


async def _stop_event_dispatcher(dispatcher: Any) -> None:
    from okto_pulse.core.events.dispatcher import set_dispatcher

    await dispatcher.stop(timeout=5.0)
    set_dispatcher(None)


async def _start_cleanup_worker() -> Any:
    from okto_pulse.core.kg.workers.cleanup import get_cleanup_worker

    worker = get_cleanup_worker()
    await worker.start()
    return worker


async def _start_consolidation_worker(session_factory: Any) -> Any:
    from okto_pulse.core.kg.workers.consolidation import ConsolidationWorker

    worker = ConsolidationWorker(session_factory)
    await worker.start()
    return worker


async def _start_outbox_worker(session_factory: Any) -> Any:
    from okto_pulse.core.kg.global_discovery.outbox_worker import OutboxWorker

    worker = OutboxWorker(session_factory)
    await worker.start()
    return worker


async def _stop_simple_worker(worker: Any) -> None:
    await worker.stop()


__all__ = [
    "COMMUNITY_WORKER_BASELINE_FAMILIES",
    "COMMUNITY_WORKER_CAPABLE_FAMILIES",
    "build_community_worker_registry",
]
