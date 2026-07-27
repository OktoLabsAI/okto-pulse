"""KG decay tick controllability endpoints (spec 54399628 — Wave 2 NC f9732afc).

Manual endpoint `POST /api/v1/kg/tick/run-now` lets an operator or MCP
agent schedule an immediate tick without waiting for the periodic cron.

Pattern compartilha a mesma `LeaseProvider` de `emit_daily_tick` — primeiro
a chegar ganha; segundo
recebe HTTP 409. Resposta 202 + tick_id só é retornada depois que o evento
e suas execuções de handler foram gravados e commitados.

`force_full_rebuild=true` zera `last_recomputed_at` de todos nodes do
escopo (board_id se fornecido, todos boards caso contrário) ANTES do
tick, forçando recompute completo ignorando staleness threshold.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import (
    get_unit_of_work,
    scheduler_control_from_request,
)
from okto_pulse.community.api.kg_health_probe import get_kg_health
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.kg_tick import (
    KGTickAdmissionDeferred,
    refuse_tick_if_degraded as _core_refuse_tick_if_degraded,
)
from okto_pulse.core.application.use_cases.board_access import load_accessible_board
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.ports.coordination import (
    CoordinationProviderMissing,
    get_lease_provider,
)
from okto_pulse.core.ports.scheduler import SchedulerControl
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.repositories import PulseUnitOfWork

import logging

logger = logging.getLogger("okto_pulse.api.kg_tick")
router = APIRouter()

_GLOBAL_TICK_PERMISSIONS = (
    "kg.tick.run.global",
    "kg.admin.historical_consolidation",
    "board.read.all",
    "boards.read.all",
)


def _permission_enabled(permissions: Any, required: str) -> bool:
    if isinstance(permissions, Mapping):
        if permissions.get("*") is True or permissions.get(required) is True:
            return True
        cursor: Any = permissions
        for part in required.split("."):
            if not isinstance(cursor, Mapping) or part not in cursor:
                return False
            cursor = cursor[part]
        return cursor is True
    checker = getattr(permissions, "check", None)
    if callable(checker):
        try:
            return checker(required) is None
        except Exception:
            return False
    if isinstance(permissions, (list, tuple, set, frozenset)):
        return required in permissions or "*" in permissions
    return False


def _require_global_tick_access(actor: ActorContext) -> None:
    roles = {str(role).lower() for role in actor.roles}
    if roles.intersection({"admin", "operator"}) or any(
        _permission_enabled(actor.permissions, permission)
        for permission in _GLOBAL_TICK_PERMISSIONS
    ):
        return
    raise HTTPException(
        status_code=403,
        detail="Global tick requires an admin or operator capability",
    )


async def _require_tick_access(
    board_id: str | None,
    principal: Principal,
    uow: PulseUnitOfWork,
) -> ActorContext:
    actor = RESTAdapterContract.actor_from_principal(principal, board_id=board_id)
    if board_id is None:
        _require_global_tick_access(actor)
        return actor
    if (
        await load_accessible_board(
            uow,
            board_id,
            actor,
            allowed_share_permissions={"editor", "admin"},
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="Board not found")
    return actor


class TickRunNowRequest(BaseModel):
    board_id: str | None = None
    force_full_rebuild: bool = False


class TickRunNowResponse(BaseModel):
    # Legacy correlation root retained for compatible clients.
    tick_id: str
    correlation_id: str
    # Concrete durable event ids. A global fan-out has one child per board.
    tick_ids: list[str]
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
    principal: Principal = Depends(require_principal),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> TickRunNowResponse:
    """Trigger the KG decay tick manually (idempotent — concurrent calls
    return 409 until the in-flight tick releases the lease).

    Body:
        - ``board_id`` (optional): scope the tick to a single board. When
          omitted, the tick runs globally (same scope as the cron schedule).
        - ``force_full_rebuild`` (optional, default false): zero out
          ``last_recomputed_at`` for nodes in scope inside the durable tick
          handler before recomputation, forcing even fresh nodes to run
          (ignores staleness threshold).

    Returns 202 after the tick event has been durably scheduled.
    Operator monitors progress via KGHealthView snapshot polling (30s).

    Returns 409 when the tick lease is already held by the cron OR
    another manual trigger.
    """
    await _require_tick_access(payload.board_id, principal, db)
    user = principal.subject

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

    schedule_committed = False
    try:
        # F17 admission gate: refuse a manual tick on a degraded CONCRETE board
        # with a structured 409 — AFTER the lease check (so
        # tick_already_running keeps priority, TR7) and BEFORE any tick_id is
        # allocated (no doomed tick). The global tick (board_id is None) is not
        # health-gated (FR9). Every post-acquire branch is inside this
        # try/finally so a probe refusal or failure cannot strand the lease.
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
            tick_id,
            user,
            payload.board_id,
            payload.force_full_rebuild,
            extra={
                "event": "kg.tick.manual_triggered",
                "tick_id": tick_id,
                "triggered_by_user_id": user,
                "board_id": payload.board_id,
                "force_full_rebuild": payload.force_full_rebuild,
            },
        )

        try:
            dispatched_tick_ids = await db.services.kg.dispatch_manual_tick(
                tick_id=tick_id,
                board_id=payload.board_id,
                force_full_rebuild=payload.force_full_rebuild,
                scheduled_at=scheduled_at,
            )
            tick_ids = [str(value) for value in dispatched_tick_ids]
            if payload.board_id is not None and tick_id not in tick_ids:
                raise RuntimeError(
                    "concrete tick dispatch did not return its durable event id"
                )
            await db.commit()
            schedule_committed = True
        except KGTickAdmissionDeferred as exc:
            await db.rollback()
            logger.info(
                "kg.tick.manual_deferred reason=%s board=%s",
                exc.reason_code,
                payload.board_id,
                extra={
                    "event": "kg.tick.manual_deferred",
                    "reason": exc.reason_code,
                    "board_id": payload.board_id,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": exc.code,
                    "reason": exc.reason_code,
                    "retryable": True,
                    "message": (
                        "KG tick deferred while Global Discovery recovery "
                        "owns the mutation fence"
                    ),
                },
            ) from exc
        except Exception as exc:
            await db.rollback()
            logger.error(
                "kg.tick.manual_schedule_failed tick_id=%s err=%s",
                tick_id,
                exc,
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
        return TickRunNowResponse(
            tick_id=tick_id,
            correlation_id=tick_id,
            tick_ids=tick_ids,
            status="running",
            scheduled_at=scheduled_at,
        )
    finally:
        try:
            await lease_provider.release(lease)
        except Exception as exc:  # noqa: BLE001 - never mask the primary result
            # In particular, a release failure after the event commit must not
            # turn a durable 202 into a 500 that invites a duplicate retry.
            # The lease has a finite TTL; surface the coordination fault to
            # operators while preserving the already-committed outcome.
            logger.error(
                "kg.tick.manual_lease_release_failed board=%s "
                "schedule_committed=%s err=%s",
                payload.board_id,
                schedule_committed,
                exc,
                exc_info=True,
                extra={
                    "event": "kg.tick.manual_lease_release_failed",
                    "board_id": payload.board_id,
                    "schedule_committed": schedule_committed,
                    "error": str(exc),
                },
            )
