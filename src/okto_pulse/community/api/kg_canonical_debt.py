"""REST endpoints for CanonicalDebt ledger."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel

from okto_pulse.community.api.deps import get_unit_of_work, scheduler_control_from_request
from okto_pulse.core.application.use_cases.operational_rest import (
    BoardNotFoundError,
    CanonicalDebtListCommand,
    CanonicalDebtRetryCommand,
    ListCanonicalDebtUseCase,
    RetryCanonicalDebtUseCase,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter(prefix="/kg/canonical-debt")


class CanonicalDebtListResponse(BaseModel):
    board_id: str
    items: list[dict[str, Any]]
    counts: dict[str, Any]
    total: int
    limit: int
    offset: int


class CanonicalDebtRetryResponse(BaseModel):
    ok: bool
    attempt_consumed: bool = False
    kg_health_state: str | None = None
    debt: dict[str, Any] | None = None
    error: str | None = None


@router.get("", response_model=CanonicalDebtListResponse)
async def get_canonical_debt(
    board_id: str = Query(..., min_length=1),
    artifact_type: str | None = Query(None),
    state: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> CanonicalDebtListResponse:
    try:
        use_case_result = await ListCanonicalDebtUseCase().execute(
            CanonicalDebtListCommand(board_id, artifact_type, state, limit, offset),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
    except BoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc
    result = use_case_result.data
    return CanonicalDebtListResponse(
        board_id=board_id,
        items=result.items,
        counts=result.counts,
        total=result.total,
        limit=limit,
        offset=offset,
    )


@router.post("/{debt_id}/retry", response_model=CanonicalDebtRetryResponse)
async def retry_canonical_debt(
    request: Request,
    debt_id: str = Path(..., min_length=1),
    board_id: str = Query(..., min_length=1),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> CanonicalDebtRetryResponse:
    try:
        use_case_result = await RetryCanonicalDebtUseCase().execute(
            CanonicalDebtRetryCommand(
                board_id,
                debt_id,
                scheduler_control_from_request(request),
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
    except BoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc
    result = use_case_result.data
    if not result.get("ok") and result.get("error") == "canonical_debt_not_found":
        raise HTTPException(status_code=404, detail="canonical debt item not found")
    return CanonicalDebtRetryResponse(**result)


__all__ = ["router"]
