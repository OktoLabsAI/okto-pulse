"""Dead Letter Inspector REST endpoint (spec ed17b1fe — Wave 2 NC 1ede3471).

GET /api/v1/kg/queue/dead-letter — listing of DLQ rows for a board.
Pagination via ``limit`` (1-200, default 50) + ``offset`` (>=0, default 0).
Board access is preflighted before the DLQ reader is constructed or called.

DLQ reprocess is available through the MCP tool
``okto_pulse_kg_dead_letter_reprocess`` and the shared service
``reprocess_dead_letter_rows``. This REST module exposes only the inspector
list endpoint.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    ListDeadLetterRowsCommand,
    ListDeadLetterRowsUseCase,
)
from okto_pulse.core.application.use_cases.list_dead_letter_rows import (
    DeadLetterBoardNotFoundError,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


class DeadLetterRow(BaseModel):
    id: str
    board_id: str
    artifact_type: str
    artifact_id: str
    original_queue_id: str | None
    attempts: int
    errors: list[dict[str, Any]]
    dead_lettered_at: str | None


class DeadLetterListResponse(BaseModel):
    rows: list[DeadLetterRow]
    total: int
    limit: int
    offset: int


@router.get(
    "/kg/queue/dead-letter",
    response_model=DeadLetterListResponse,
)
async def get_dead_letter(
    board_id: str = Query(..., description="Board UUID (required)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> DeadLetterListResponse:
    """List dead-lettered consolidation rows for a board.

    Returns ``{rows, total, limit, offset}``. Each row includes the
    full ``errors[]`` history from the TR16 schema (one entry per
    attempt: error_type, message, occurred_at, traceback).

    Spec R01A IMP2: the handler is the REST inbound adapter — it obtains a
    request-scoped ``PulseUnitOfWork`` (bound to the request session via
    ``get_db``, so the dependency override still applies) and calls the
    transport-free use case. No raw ``AsyncSession``/``get_db`` in the handler's
    contract with the use case; payload/permission are unchanged.
    """
    try:
        result = await ListDeadLetterRowsUseCase().execute(
            ListDeadLetterRowsCommand(board_id, limit=limit, offset=offset),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except DeadLetterBoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    return DeadLetterListResponse(**result.data)
