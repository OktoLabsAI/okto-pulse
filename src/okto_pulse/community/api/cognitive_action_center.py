"""REST endpoint for the Cognitive Action Center read-model
(S3 / card 38a014f7 / api_8ee8f41b).

GET /api/v1/kg/{board_id}/cognitive-readiness/items — a READ-ONLY projection of
the board's readiness signals (cognitive items + canonical debt + technical DLQ),
reconciled by normalized artifact_id, with each artifact's readiness verdict
OBTAINED from ``CognitiveReadinessService`` (readiness_effect / blocking /
precedence_explanation surfaced verbatim, NEVER recomputed — tr_6bfe98e7 /
tr_0aab9bea / br_ee939fc7). The Action Center owns no store/table/queue/ledger
(fr_7695a7a7 / dec_272906c3).

Errors echo the bounded code verbatim:
  * 400 ``invalid_filter`` — unknown ``signal`` value;
  * 503 ``readiness_source_unavailable`` — a readiness source could not be read.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.board_access import load_accessible_board
from okto_pulse.core.application.use_cases.cognitive_readiness import (
    ListCognitiveReadinessItemsCommand,
    ListCognitiveReadinessItemsUseCase,
)
from okto_pulse.core.application.use_cases.operational_rest import (
    BoardNotFoundError,
    ClearCognitiveSkipUseCase,
    CognitiveClearCommand,
    CognitiveReadinessMetricsCommand,
    CognitiveSkipCommand,
    GetCognitiveReadinessMetricsUseCase,
    RecordCognitiveSkipUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.kg.cognitive_action_center import (
    build_clear_response,
    build_skip_response,
)
from okto_pulse.core.kg.cognitive_readiness import (
    GATE_BLOCKING_TIERS,
    CognitiveReadinessError,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX

router = APIRouter()


def _board_not_found(exc: BoardNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail="Board not found")


def build_default_readiness_service():
    """Compatibility injection point for REST tests and thin wrapper wiring."""
    from okto_pulse.core.services.application_kg import (
        build_cognitive_readiness_service,
    )

    return build_cognitive_readiness_service()


def _would_block_done(verdict, enforcement_active: bool) -> bool:
    return bool(enforcement_active and verdict.tier in GATE_BLOCKING_TIERS)


class CognitiveSkipRequest(BaseModel):
    """Direct cognitive skip / no_action on an artifact (fr_2007b14a)."""

    source_ref: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    justification: str | None = None
    evidence_refs: list[str] | None = None
    revisit_at: str | None = None
    kg_generation_id: str | None = None


class CognitiveClearRequest(BaseModel):
    """Clear / reopen a cognitive skip via the central path."""

    source_ref: str = Field(min_length=1)
    kg_generation_id: str | None = None


@router.get(
    "/kg/{board_id}/cognitive-readiness/items",
    tags=["kg-cognitive-action-center"],
)
async def list_cognitive_readiness_items(
    board_id: str,
    signal: str = Query("all"),
    artifact_id: str | None = Query(None),
    source_ref: str | None = Query(None),
    reason_code: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    kg_generation_id: str | None = Query(None),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        actor_context = RESTAdapterContract.actor(actor, board_id=board_id)
        if await load_accessible_board(db, board_id, actor_context) is None:
            raise BoardNotFoundError(board_id)
        uc_result = await ListCognitiveReadinessItemsUseCase(
            readiness_service_factory=build_default_readiness_service
        ).execute(
            ListCognitiveReadinessItemsCommand(
                board_id,
                signal=signal,
                artifact_id=artifact_id,
                source_ref=source_ref,
                reason_code=reason_code,
                status=status,
                search=search,
                limit=limit,
                offset=offset,
                kg_generation_id=kg_generation_id,
            ),
            actor=actor_context,
            uow=db,
        )
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except CognitiveReadinessError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc

    result = uc_result.result
    # Enforcement-aware annotation so the UI never says "blocks done" purely from
    # blocking=True (S3.1 carry-forward): would_block_done = enforcement_active
    # AND tier in GATE_BLOCKING_TIERS, delegated to the central helper.
    for item in result["items"]:
        tier = (item.get("precedence_explanation") or {}).get("tier")
        item["would_block_done"] = bool(
            uc_result.enforcement_active and tier in GATE_BLOCKING_TIERS
        )
    result["summary"]["enforcement_active"] = uc_result.enforcement_active
    return result


@router.post(
    "/kg/{board_id}/cognitive-readiness/skip",
    tags=["kg-cognitive-action-center"],
)
async def record_cognitive_skip_endpoint(
    board_id: str,
    payload: CognitiveSkipRequest,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    """Thin REST over the CENTRAL skip write-path (same service as the MCP twin).
    400 invalid_reason_code / 400 revisit_at_required / 409
    technical_debt_cannot_be_skipped — a DLQ/open-debt blocker is NEVER masked."""
    try:
        result = await RecordCognitiveSkipUseCase(
            readiness_service_factory=build_default_readiness_service
        ).execute(
            CognitiveSkipCommand(
                board_id,
                payload.source_ref,
                payload.reason_code,
                payload.justification,
                payload.evidence_refs,
                payload.revisit_at,
                payload.kg_generation_id,
            ),
            actor=RESTAdapterContract.actor(actor, board_id=board_id),
            uow=db,
        )
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except CognitiveReadinessError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc
    data = result.data
    return build_skip_response(
        data["item"], data["verdict"], actor=actor,
        would_block_done=_would_block_done(
            data["verdict"], data["enforcement_active"]
        ),
    )


@router.post(
    "/kg/{board_id}/cognitive-readiness/clear",
    tags=["kg-cognitive-action-center"],
)
async def clear_cognitive_skip_endpoint(
    board_id: str,
    payload: CognitiveClearRequest,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    """Clear/reopen a cognitive skip via the CENTRAL path (audit preserved).
    404 generation/item_not_found; 409 cognitive_item_not_skipped."""
    try:
        result = await ClearCognitiveSkipUseCase(
            readiness_service_factory=build_default_readiness_service
        ).execute(
            CognitiveClearCommand(
                board_id,
                payload.source_ref,
                payload.kg_generation_id,
            ),
            actor=RESTAdapterContract.actor(actor, board_id=board_id),
            uow=db,
        )
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except CognitiveReadinessError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc
    data = result.data
    return build_clear_response(
        data["item"], data["verdict"], actor=actor,
        would_block_done=_would_block_done(
            data["verdict"], data["enforcement_active"]
        ),
    )


@router.get(
    "/kg/{board_id}/cognitive-readiness/metrics",
    tags=["kg-cognitive-action-center"],
)
async def get_cognitive_readiness_metrics(
    board_id: str,
    kg_generation_id: str | None = Query(None),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    """Bounded readiness metrics derived from the read-model (fr_9ae21310).
    Labels are bounded; free-text is never a label (br_3a55b609)."""
    try:
        result = await GetCognitiveReadinessMetricsUseCase(
            readiness_service_factory=build_default_readiness_service
        ).execute(
            CognitiveReadinessMetricsCommand(board_id, kg_generation_id),
            actor=RESTAdapterContract.actor(actor, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except CognitiveReadinessError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc
