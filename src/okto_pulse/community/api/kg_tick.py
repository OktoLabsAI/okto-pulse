"""KG decay tick controllability endpoints (spec 54399628 — Wave 2 NC f9732afc).

Manual endpoint `POST /api/v1/kg/tick/run-now` lets an operator or MCP
agent schedule an immediate tick without waiting for the periodic cron.

Pattern compartilha a mesma `LeaseProvider` do `_emit_daily_tick` — primeiro
a chegar ganha; segundo
recebe HTTP 409. Resposta 202 + tick_id só é retornada depois que o evento
e suas execuções de handler foram gravados e commitados.

`force_full_rebuild=true` zera `last_recomputed_at` de todos nodes do
escopo (board_id se fornecido, todos boards caso contrário) ANTES do
tick, forçando recompute completo ignorando staleness threshold.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work, scheduler_control_from_request
from okto_pulse.community.api.kg_health_probe import get_kg_health
from okto_pulse.core.application.kg_tick import (
    refuse_tick_if_degraded as _core_refuse_tick_if_degraded,
)
from okto_pulse.core.ports.coordination import (
    CoordinationProviderMissing,
    get_lease_provider,
)
from okto_pulse.core.ports.scheduler import SchedulerControl
from okto_pulse.core.repositories import PulseUnitOfWork

import logging

logger = logging.getLogger("okto_pulse.api.kg_tick")
router = APIRouter()


class TickRunNowRequest(BaseModel):
    board_id: str | None = None
    force_full_rebuild: bool = False


class TickRunNowResponse(BaseModel):
    tick_id: str
    status: str  # "running"
    scheduled_at: str  # ISO


async def _refuse_tick_if_degraded(
    board_id: str | None,
    uow: PulseUnitOfWork,
    *,
    scheduler_control: SchedulerControl | None = None,
) -> dict[str, object] | None:
    """Inject the Community health reader into the Core admission policy."""

    return await _core_refuse_tick_if_degraded(
        board_id,
        uow,
        scheduler_control=scheduler_control,
        health_probe=get_kg_health,
    )


@router.post(
    "/kg/tick/run-now",
    response_model=TickRunNowResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_tick_now(
    payload: TickRunNowRequest,
    request: Request,
    user: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> TickRunNowResponse:
    """Trigger the KG decay tick manually (idempotent — concurrent calls
    return 409 until the in-flight tick releases the lease).

    Body:
        - ``board_id`` (optional): scope the tick to a single board. When
          omitted, the tick runs globally (same scope as the cron schedule).
        - ``force_full_rebuild`` (optional, default false): zero out
          ``last_recomputed_at`` for nodes in scope BEFORE the tick, forcing
          recompute even of fresh nodes (ignores staleness threshold).

    Returns 202 after the tick event has been durably scheduled.
    Operator monitors progress via KGHealthView snapshot polling (30s).

    Returns 409 when the tick lease is already held by the cron OR
    another manual trigger.
    """
    try:
        lease_provider = get_lease_provider()
    except CoordinationProviderMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": exc.code,
                "provider": exc.provider_key,
                "message": "Tick lease provider is not configured",
            },
        ) from exc

    lease = await lease_provider.try_acquire("kg_daily_tick", ttl_seconds=300)
    if lease is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "tick_already_running",
                "message": "Tick already running, retry shortly",
            },
        )

    # F17 admission gate: refuse a manual tick on a degraded CONCRETE board with
    # a structured 409 — AFTER the lease check (so tick_already_running keeps
    # priority, TR7) and BEFORE any tick_id is allocated (no doomed tick). The
    # global tick (board_id is None) is not health-gated (FR9).
    refusal = await _refuse_tick_if_degraded(
        payload.board_id,
        db,
        scheduler_control=scheduler_control_from_request(request),
    )
    if refusal is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=refusal,
        )

    tick_id = str(uuid.uuid4())
    scheduled_at = datetime.now(timezone.utc).isoformat()

    # Audit log — emit BEFORE the background task starts so the trigger
    # is recorded even if the task crashes immediately.
    logger.info(
        "kg.tick.manual_triggered tick_id=%s user=%s board=%s force=%s",
        tick_id, user, payload.board_id, payload.force_full_rebuild,
        extra={
            "event": "kg.tick.manual_triggered",
            "tick_id": tick_id,
            "triggered_by_user_id": user,
            "board_id": payload.board_id,
            "force_full_rebuild": payload.force_full_rebuild,
        },
    )

    try:
        try:
            await db.services.kg.dispatch_manual_tick(
                tick_id=tick_id,
                board_id=payload.board_id,
                force_full_rebuild=payload.force_full_rebuild,
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.error(
                "kg.tick.manual_schedule_failed tick_id=%s err=%s",
                tick_id, exc,
                extra={
                    "event": "kg.tick.manual_schedule_failed",
                    "tick_id": tick_id,
                    "board_id": payload.board_id,
                    "force_full_rebuild": payload.force_full_rebuild,
                    "error": str(exc),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "tick_schedule_failed",
                    "message": (
                        "Failed to persist the KG tick event. "
                        "No background tick was scheduled."
                    ),
                    "detail": str(exc),
                },
            ) from exc
    finally:
        await lease_provider.release(lease)

    return TickRunNowResponse(
        tick_id=tick_id,
        status="running",
        scheduled_at=scheduled_at,
    )


