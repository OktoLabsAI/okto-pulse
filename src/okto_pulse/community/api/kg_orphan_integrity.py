"""REST surfaces for KG orphan integrity reports and explicit backfill."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from okto_pulse.community.api.deps import get_unit_of_work, scheduler_control_from_request
from okto_pulse.community.api.kg_health_probe import get_kg_health
from okto_pulse.core.application.use_cases.operational_rest import (
    BoardNotFoundError,
    GetOrphanIntegrityReportUseCase,
    OrphanBackfillCommand,
    OrphanIntegrityReportCommand,
    RunOrphanBackfillUseCase,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.kg.orphan_integrity import (
    DEFAULT_ORPHAN_SAMPLE_LIMIT,
    MAX_ORPHAN_SAMPLE_LIMIT,
    OrphanBackfillReconciler,
    OrphanNodeScanner,
)
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


class OrphanBackfillRequest(BaseModel):
    board_id: str
    generation_id: str | None = None
    dry_run: bool = True
    node_ids: list[str] | None = None
    limit: int = Field(default=DEFAULT_ORPHAN_SAMPLE_LIMIT, ge=0)


def _graph_unavailable_error(board_id: str, exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "kg_orphan_graph_unavailable",
            "board_id": board_id,
            "error_type": type(exc).__name__,
            "operator_action": "inspect_kg_health",
        },
    )


async def _report_payload(
    *,
    board_id: str,
    generation_id: str | None,
    limit: int,
    actor: str,
    uow: PulseUnitOfWork,
) -> dict[str, Any]:
    result = await GetOrphanIntegrityReportUseCase(
        scanner_factory=OrphanNodeScanner,
    ).execute(
        OrphanIntegrityReportCommand(board_id, generation_id, limit),
        actor=RESTAdapterContract.actor(actor, board_id=board_id),
        uow=uow,
    )
    report = result.data
    payload = report.to_safe_dict()
    payload["backfill_summary"] = {
        "status": "not_run",
        "dry_run": None,
        "detected": None,
        "connected": None,
        "noop": None,
        "unresolved": None,
        "ambiguous": None,
        "semantic_pending": None,
    }
    return payload


@router.get("/kg/orphan-integrity/report")
async def get_kg_orphan_integrity_report(
    board_id: str = Query(..., description="Board ID"),
    generation_id: str | None = Query(None, description="Optional KG generation ID"),
    limit: int = Query(
        DEFAULT_ORPHAN_SAMPLE_LIMIT,
        ge=0,
        le=MAX_ORPHAN_SAMPLE_LIMIT,
        description="Maximum number of safe orphan samples to return",
    ),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, Any]:
    """Return a bounded, safe orphan-node report for a board."""

    try:
        return await _report_payload(
            board_id=board_id,
            generation_id=generation_id,
            limit=limit,
            actor=user_id,
            uow=db,
        )
    except BoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except Exception as exc:
        raise _graph_unavailable_error(board_id, exc) from exc


@router.post("/kg/orphan-integrity/backfill")
async def post_kg_orphan_integrity_backfill(
    body: OrphanBackfillRequest,
    request: Request,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, Any]:
    """Run explicit orphan backfill. Defaults to dry-run for safe review."""

    try:
        result = await RunOrphanBackfillUseCase(
            health_reader=get_kg_health,
            reconciler_factory=OrphanBackfillReconciler,
        ).execute(
            OrphanBackfillCommand(
                body.board_id,
                body.generation_id,
                body.dry_run,
                body.node_ids,
                body.limit,
                scheduler_control_from_request(request),
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=body.board_id),
            uow=db,
        )
    except BoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except Exception as exc:
        raise _graph_unavailable_error(body.board_id, exc) from exc
    if isinstance(result.data, dict) and "refused_by_health" in result.data:
        raise HTTPException(status_code=409, detail=result.data["refused_by_health"])

    return result.data
