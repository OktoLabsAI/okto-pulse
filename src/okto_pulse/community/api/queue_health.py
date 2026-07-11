"""REST endpoint for the consolidation queue health snapshot.

Spec bdcda842 (FR9): GET /api/v1/kg/queue/health returns 13 live metrics
the EventQueueTab in the frontend polls every 2s. The endpoint is read-only
and never touches Kùzu — only the SQLite app DB + in-process counters.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from typing import Any

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    GetQueueDrilldownCommand,
    GetQueueDrilldownUseCase,
    GetQueueHealthCommand,
    GetQueueHealthUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


class QueueHealthResponse(BaseModel):
    """Live consolidation queue health snapshot."""

    queue_depth: int
    oldest_pending_age_s: float
    claimed_count: int
    claimed_boards: list[str]
    dead_letter_count: int
    claims_per_min_1m: int
    claims_per_min_5m: int
    alert_threshold: int
    alert_active: bool
    alert_fired_total: int
    workers_active: int
    workers_idle: int
    workers_draining_count: int
    kuzu_lock_retries_5m: int


@router.get("/kg/queue/health", response_model=QueueHealthResponse)
async def get_kg_queue_health(
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> QueueHealthResponse:
    """Return the live snapshot of consolidation queue health.

    Polled by the frontend EventQueueTab at 2s intervals (TR10). Safe to
    invoke at any frequency — counts are SQL aggregates and in-process
    sliding windows; no Kùzu I/O.

    Spec R01A IMP4: routes through the transport-free use case via
    ``get_unit_of_work`` — no raw ``AsyncSession``/``get_db`` in the handler.
    """
    result = await GetQueueHealthUseCase().execute(
        GetQueueHealthCommand(),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )
    return QueueHealthResponse(**result.data)


@router.get("/kg/queue/drilldown")
async def get_kg_queue_drilldown(
    board_id: str | None = None,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, Any]:
    """R6-IMP2 read-only twin of the MCP `okto_pulse_kg_queue_drilldown` tool.

    Drill-down of the ACTIVE operational queue depth (ConsolidationQueue
    pending/claimed + global_update_outbox retry-window rows), split by source,
    with worker_mode + transient|stuck|backpressure|idle classification. Global
    when `board_id` is omitted; board-scoped otherwise. DLQ / canonical debt are
    NOT counted here. SQL aggregates only — no new store, no Kùzu I/O.

    Spec R01A IMP4: routes through the transport-free use case via
    ``get_unit_of_work`` — no raw ``AsyncSession``/``get_db`` in the handler."""
    result = await GetQueueDrilldownUseCase().execute(
        GetQueueDrilldownCommand(board_id),
        actor=RESTAdapterContract.actor(user_id, board_id=board_id),
        uow=uow,
    )
    return result.data
