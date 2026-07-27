"""REST endpoint for candidate decisions (KG-03A.4 / api_2d0d274d).

Read-only adapter over ``CandidateDecisionStore`` used by the cognitive
consolidation review UI to list per-board candidate decisions awaiting
operator triage (promote / link / dismiss).

Contract:

    GET /api/v1/kg/cognitive-pending/candidate-decisions
      ?board_id=<uuid>
      &status=<bounded-enum>?
      &source_ref=<str>?
      &limit=<1..100, default 50>
      &offset=<0..>=default 0>

    Response:
      {
        "board_id": "...",
        "readonly": true,
        "counts": {
          proposed, promoted, linked, dismissed,
          no_action_required, total,
        },
        "items": [<safe-projection candidates>],
      }

Errors:
    invalid_status — 400
    candidate_decisions_unavailable — 503

This endpoint is the ONLY KG-03A.4 read surface for the UI. There is NO
mutating endpoint here — promotion / dismiss / link belong to KG-03A.5.

The endpoint NEVER mutates ``spec.decisions`` on the Board DB.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.board_access import load_accessible_board
from okto_pulse.core.kg.candidate_decision_store import (
    CandidateDecisionRecord,
    CandidateDecisionStatus,
    CandidateDecisionStore,
)
from okto_pulse.core.kg.rebuild_audit import require_rebuild_audit_artifact_store
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX


router = APIRouter()


_VALID_STATUS_VALUES: frozenset[str] = frozenset(
    s.value for s in CandidateDecisionStatus
)


class CandidateDecisionItem(BaseModel):
    """Read-only projection of a candidate decision record."""

    candidate_id: str
    board_id: str
    source_ref: str
    source_generation_id: str
    consolidation_session_id: str
    title: str
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)
    status: str
    created_by_agent_id: str
    created_at: str
    updated_at: str
    formal_decision_ref: str | None = None
    dismissed_reason_code: str | None = None
    audit_ref: str | None = None


class CandidateDecisionCounts(BaseModel):
    proposed: int = 0
    promoted: int = 0
    linked: int = 0
    dismissed: int = 0
    no_action_required: int = 0
    total: int = 0


class CandidateDecisionListResponse(BaseModel):
    board_id: str
    readonly: bool = True
    counts: CandidateDecisionCounts = Field(
        default_factory=CandidateDecisionCounts
    )
    items: list[CandidateDecisionItem] = Field(default_factory=list)


def _project_candidate(record: CandidateDecisionRecord) -> CandidateDecisionItem:
    return CandidateDecisionItem(
        candidate_id=record.candidate_id,
        board_id=record.board_id,
        source_ref=record.source_ref,
        source_generation_id=record.source_generation_id,
        consolidation_session_id=record.consolidation_session_id,
        title=record.title,
        rationale=record.rationale,
        evidence_refs=list(record.evidence_refs),
        status=record.status,
        created_by_agent_id=record.created_by_agent_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        formal_decision_ref=record.formal_decision_ref,
        dismissed_reason_code=record.dismissed_reason_code,
        audit_ref=record.audit_ref,
    )


def _compute_status_counts(
    records: list[CandidateDecisionRecord],
) -> CandidateDecisionCounts:
    counts = CandidateDecisionCounts()
    for record in records:
        if record.status == CandidateDecisionStatus.PROPOSED.value:
            counts.proposed += 1
        elif record.status == CandidateDecisionStatus.PROMOTED.value:
            counts.promoted += 1
        elif record.status == CandidateDecisionStatus.LINKED.value:
            counts.linked += 1
        elif record.status == CandidateDecisionStatus.DISMISSED.value:
            counts.dismissed += 1
        elif record.status == CandidateDecisionStatus.NO_ACTION_REQUIRED.value:
            counts.no_action_required += 1
        counts.total += 1
    return counts


@router.get(
    "/kg/cognitive-pending/candidate-decisions",
    response_model=CandidateDecisionListResponse,
    tags=["kg-cognitive-pending"],
)
async def list_candidate_decisions(
    board_id: str = Query(..., min_length=1),
    status: str | None = Query(default=None),
    source_ref: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> CandidateDecisionListResponse:
    """List candidate decisions awaiting operator triage.

    Returns the contract-aligned read-only payload. NEVER mutates the
    Board DB or the candidate store.
    """

    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    if await load_accessible_board(uow, board_id, actor) is None:
        raise HTTPException(status_code=404, detail="Board not found")

    if status is not None and status not in _VALID_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_status",
                "message": (
                    "status must be one of "
                    f"{sorted(_VALID_STATUS_VALUES)}"
                ),
            },
        )

    try:
        store = CandidateDecisionStore(
            artifact_store=require_rebuild_audit_artifact_store()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "candidate_decisions_unavailable",
                "message": "candidate decision store could not be opened",
                "reason": type(exc).__name__,
            },
        ) from exc

    try:
        page_items = store.list(
            board_id,
            status_filter=status,
            source_ref_filter=source_ref,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "candidate_decisions_unavailable",
                "message": "candidate decision store could not be read",
                "reason": type(exc).__name__,
            },
        ) from exc

    counts = _compute_status_counts(page_items)
    items = [_project_candidate(record) for record in page_items]

    return CandidateDecisionListResponse(
        board_id=board_id,
        readonly=True,
        counts=counts,
        items=items,
    )
