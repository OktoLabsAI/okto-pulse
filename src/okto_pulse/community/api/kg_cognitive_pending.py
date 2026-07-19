"""REST endpoint for cognitive pending feedback (KG-03.4 / api_cce40fa6).

Read-only adapter over ``CognitiveConsolidationItemStore`` used by the
KGHealthView UI to show per-item cognitive consolidation progress.

Contract:

    GET /api/v1/kg/cognitive-pending
      ?board_id=<uuid>
      &kg_generation_id=<uuid>?
      &status=<bounded-enum>?
      &limit=<1..100, default 50>
      &offset=<0..n, default 0>

    Response:
      {
        "board_id": "...",
        "selected_kg_generation_id": "..." | null,
        "readonly": true,
        "legacy_mode": <bool>,
        "counts": {pending, in_progress, consolidated, skipped, failed, total},
        "items": [<safe-projection items>],
      }

``counts`` always describes the full selected KG generation. ``items`` is
the only paginated field. This lets dashboards request a tiny page for a
summary without under-reporting the generation state.

Errors:
    invalid_status — 400
    generation_not_found — 404
    cognitive_pending_unavailable — 503

This endpoint is the ONLY KG-03 read surface for the UI. There is NO
mutating endpoint (br_2065f80b + ir_0b66c7af). The router exposes
exactly ONE GET path.

Metrics:
    kg_cognitive_item_list_total (or_c8fff4f5) — emitted with
        ``surface="rest"`` so MCP and REST share the same counter with
        bounded surface labels.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.board_access import load_accessible_board
from okto_pulse.core.kg.rebuild_audit import (
    CognitiveConsolidationItemStore,
    CognitiveItemListOutcome,
    CognitiveItemListReasonCode,
    CognitiveItemListSurface,
    CognitiveItemStatus,
    emit_list_sample,
    compute_status_counts,
    empty_status_counts,
    project_item_for_api,
    require_rebuild_audit_artifact_store,
)
from okto_pulse.core.repositories import PulseUnitOfWork


router = APIRouter()


_VALID_STATUS_VALUES: frozenset[str] = frozenset(
    s.value for s in CognitiveItemStatus
)


class CognitivePendingItem(BaseModel):
    """Read-only item surface for the KG Health panel.

    Mirrors ``project_item_for_api`` but kept as a Pydantic model so
    OpenAPI generation reflects the contract field set exactly.
    """

    item_id: str
    source_ref: str
    artifact_type: str
    status: str
    recorded_at: str
    updated_at: str | None = None
    updated_by_agent_id: str | None = None
    consolidation_session_id: str | None = None
    reason_code: str | None = None


class CognitivePendingCounts(BaseModel):
    pending: int = 0
    in_progress: int = 0
    consolidated: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0


class CognitivePendingResponse(BaseModel):
    board_id: str
    selected_kg_generation_id: str | None = None
    readonly: bool = True
    legacy_mode: bool = False
    counts: CognitivePendingCounts = Field(default_factory=CognitivePendingCounts)
    items: list[CognitivePendingItem] = Field(default_factory=list)


@router.get(
    "/kg/cognitive-pending",
    response_model=CognitivePendingResponse,
    tags=["kg-cognitive-pending"],
)
async def get_cognitive_pending(
    board_id: str = Query(..., min_length=1),
    kg_generation_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> CognitivePendingResponse:
    """List cognitive pending items for the KG Health UI panel.

    Returns the contract-aligned read-only payload. NEVER mutates
    storage and NEVER exposes raw artifact bodies or free-text reasons.
    """

    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    if await load_accessible_board(uow, board_id, actor) is None:
        raise HTTPException(status_code=404, detail="Board not found")

    status_present = status is not None

    if status_present and status not in _VALID_STATUS_VALUES:
        emit_list_sample(
            surface=CognitiveItemListSurface.REST.value,
            board_id=board_id,
            outcome=CognitiveItemListOutcome.VALIDATION_ERROR.value,
            status_filter_present=True,
            reason_code=CognitiveItemListReasonCode.INVALID_STATUS.value,
            item_count=0,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_status",
                "message": (
                    "status must be one of "
                    f"{sorted(_VALID_STATUS_VALUES)}"
                ),
                "reason_code": CognitiveItemListReasonCode.INVALID_STATUS.value,
            },
        )

    try:
        store = CognitiveConsolidationItemStore(
            artifact_store=require_rebuild_audit_artifact_store()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "cognitive_pending_unavailable",
                "message": "item store could not be read",
                "reason": type(exc).__name__,
            },
        ) from exc

    explicit_generation = bool(kg_generation_id)
    resolved_generation = (
        kg_generation_id
        if explicit_generation
        else store.latest_generation(board_id)
    )

    if explicit_generation and not store.record_exists(
        board_id, resolved_generation
    ):
        emit_list_sample(
            surface=CognitiveItemListSurface.REST.value,
            board_id=board_id,
            outcome=CognitiveItemListOutcome.NOT_FOUND.value,
            status_filter_present=status_present,
            reason_code=CognitiveItemListReasonCode.NO_GENERATION_FOUND.value,
            item_count=0,
        )
        raise HTTPException(
            status_code=404,
            detail={
                "code": "generation_not_found",
                "message": (
                    "no cognitive pending record exists for the requested generation"
                ),
                "reason_code": CognitiveItemListReasonCode.NO_GENERATION_FOUND.value,
            },
        )

    if resolved_generation is None:
        emit_list_sample(
            surface=CognitiveItemListSurface.REST.value,
            board_id=board_id,
            outcome=CognitiveItemListOutcome.NOT_FOUND.value,
            status_filter_present=status_present,
            reason_code=CognitiveItemListReasonCode.NO_GENERATION_FOUND.value,
            item_count=0,
        )
        return CognitivePendingResponse(
            board_id=board_id,
            selected_kg_generation_id=None,
            readonly=True,
            legacy_mode=False,
            counts=CognitivePendingCounts(**empty_status_counts()),
            items=[],
        )

    try:
        legacy_mode = store.is_legacy_record(board_id, resolved_generation)
        all_items = store.list_items(
            board_id,
            resolved_generation,
        )
        page_items = store.list_items(
            board_id,
            resolved_generation,
            status_filter=status,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "cognitive_pending_unavailable",
                "message": "item store could not be read",
                "reason": type(exc).__name__,
            },
        ) from exc

    counts = compute_status_counts(all_items)

    emit_list_sample(
        surface=CognitiveItemListSurface.REST.value,
        board_id=board_id,
        outcome=CognitiveItemListOutcome.SUCCESS.value,
        status_filter_present=status_present,
        reason_code=CognitiveItemListReasonCode.NONE.value,
        item_count=len(page_items),
    )

    safe_items: list[dict[str, Any]] = [
        project_item_for_api(item) for item in page_items
    ]

    return CognitivePendingResponse(
        board_id=board_id,
        selected_kg_generation_id=resolved_generation,
        readonly=True,
        legacy_mode=legacy_mode,
        counts=CognitivePendingCounts(**counts),
        items=[CognitivePendingItem(**item) for item in safe_items],
    )
