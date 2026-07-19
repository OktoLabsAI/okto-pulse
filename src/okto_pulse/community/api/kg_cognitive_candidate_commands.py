"""Command endpoint for candidate decision lifecycle (KG-03A.5).

Single POST surface to transition a candidate decision through one of
four explicit, bounded actions:

    POST /api/v1/kg/cognitive-pending/candidate-decisions/{candidate_id}/command
      Body:
        {
          "board_id": "...",
          "action": "promote_to_spec_decision" | "link_existing_decision"
                    | "dismiss" | "no_action_required",
          # action=promote_to_spec_decision:
          "spec_id": "...",
          "title": "...?",        # defaults to candidate.title
          "rationale": "...?",    # defaults to candidate.rationale
          "context": "...?",
          "alternatives_considered": "...?",
          "supersedes_decision_id": "...?",
          "linked_requirements": "...?",
          "notes": "...?",
          # action=link_existing_decision:
          "spec_id": "...",
          "formal_decision_id": "dec_...",
          # action=dismiss | no_action_required:
          "reason_code": "...",
        }

Invariants:

* ``promote_to_spec_decision`` and ``link_existing_decision`` MUST write
  through the formal spec decision flow on the Board DB (``SpecService``
  + ``spec.decisions`` JSON list). They MAY NEVER short-circuit the
  candidate status without producing/linking a formal decision.
* ``dismiss`` and ``no_action_required`` MUST NOT create a formal
  decision and MUST NOT mutate ``spec.decisions``.
* Every accepted command writes ``audit_ref = "audit_cmd_<uuid4hex>"``
  onto the candidate record and emits one bounded counter sample
  through the existing ``kg_candidate_decision_event_total`` series.
* The endpoint is the ONLY mutating REST surface for candidate
  decisions. ``GET /candidate-decisions`` (KG-03A.4) remains read-only.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.board_access import load_accessible_board
from okto_pulse.core.kg.candidate_decision_store import (
    CandidateDecisionAction,
    CandidateDecisionError,
    CandidateDecisionRecord,
    CandidateDecisionStatus,
    CandidateDecisionStore,
)
from okto_pulse.core.kg.rebuild_audit import (
    CognitiveConsolidationItemStore,
    CognitiveItemStatus,
    CognitivePendingOutcomeType,
    is_raw_token_shape,
    require_rebuild_audit_artifact_store,
)
from okto_pulse.core.models.schemas import SpecUpdate
from okto_pulse.core.repositories import PulseUnitOfWork


# Override unsafe-payload bounds — same shape used by the candidate
# store's record() guard. Overrides write directly into ``spec.decisions``,
# so they MUST be validated before SpecService.update_spec.
_MAX_OVERRIDE_TEXT_BYTES = 4000
_MAX_OVERRIDE_LIST_LEN = 50
_MAX_OVERRIDE_LIST_ENTRY_BYTES = 200


# Pending item lookup config — keep it cheap. Worst case is a single
# generation with a few thousand items; we cap the scan.
_PENDING_ITEM_SCAN_LIMIT = 500


router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


CommandActionLiteral = Literal[
    "promote_to_spec_decision",
    "link_existing_decision",
    "dismiss",
    "no_action_required",
]


class CandidateDecisionCommandRequest(BaseModel):
    board_id: str = Field(..., min_length=1)
    action: CommandActionLiteral

    # promote_to_spec_decision
    spec_id: str | None = None
    title: str | None = None
    rationale: str | None = None
    context: str | None = None
    alternatives_considered: list[str] | None = None
    supersedes_decision_id: str | None = None
    linked_requirements: list[int] | None = None
    notes: str | None = None

    # link_existing_decision
    formal_decision_id: str | None = None

    # dismiss | no_action_required
    reason_code: str | None = None


class CandidateDecisionCommandResponse(BaseModel):
    candidate_id: str
    board_id: str
    action: str
    status: str
    formal_decision_ref: str | None = None
    formal_decision: dict[str, Any] | None = None
    dismissed_reason_code: str | None = None
    audit_ref: str
    updated_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_audit_ref() -> str:
    return f"audit_cmd_{uuid.uuid4().hex}"


def _generate_decision_id() -> str:
    return f"dec_{uuid.uuid4().hex[:8]}"


def _project(record: CandidateDecisionRecord) -> dict[str, Any]:
    return {
        "candidate_id": record.candidate_id,
        "board_id": record.board_id,
        "status": record.status,
        "formal_decision_ref": record.formal_decision_ref,
        "dismissed_reason_code": record.dismissed_reason_code,
        "audit_ref": record.audit_ref or "",
        "updated_at": record.updated_at,
    }


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": code, "message": message},
    )


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": code, "message": message},
    )


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


def _candidate_error_to_http(exc: CandidateDecisionError) -> HTTPException:
    detail = {
        "code": exc.outcome,
        "message": exc.message,
        "reason_code": exc.reason_code,
    }
    if exc.unsafe_field is not None:
        detail["unsafe_field"] = exc.unsafe_field
    if exc.outcome == "not_found":
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=detail
        )
    if exc.outcome in ("validation_error", "unsafe_payload"):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=detail
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
    )


def _is_terminal(status_value: str) -> bool:
    """proposed is the only non-terminal status — every other value is
    a one-shot decision the operator already made."""

    return status_value != CandidateDecisionStatus.PROPOSED.value


def _validate_override_text(
    field_name: str, value: str | None, *, required: bool = False,
) -> None:
    """Reject token-shape / oversized / non-string overrides BEFORE the
    formal write. Reuses the same primitives as
    ``_detect_unsafe_candidate_payload``."""

    if value is None:
        if required:
            raise _bad_request(
                "override_field_required",
                f"override field {field_name!r} is required",
            )
        return
    if not isinstance(value, str):
        raise _bad_request(
            "unsafe_override_payload",
            f"override field {field_name!r} must be a string",
        )
    if required and not value.strip():
        raise _bad_request(
            "override_field_required",
            f"override field {field_name!r} cannot be blank",
        )
    if len(value.encode("utf-8")) > _MAX_OVERRIDE_TEXT_BYTES:
        raise _bad_request(
            "unsafe_override_payload",
            f"override field {field_name!r} exceeds {_MAX_OVERRIDE_TEXT_BYTES} bytes",
        )
    if is_raw_token_shape(value):
        raise _bad_request(
            "unsafe_override_payload",
            f"override field {field_name!r} looks like a raw token",
        )


def _validate_override_list(
    field_name: str, values: list[str] | None,
) -> None:
    if values is None:
        return
    if isinstance(values, (str, bytes)) or not isinstance(values, list):
        raise _bad_request(
            "unsafe_override_payload",
            f"override field {field_name!r} must be a list of strings",
        )
    if len(values) > _MAX_OVERRIDE_LIST_LEN:
        raise _bad_request(
            "unsafe_override_payload",
            (
                f"override field {field_name!r} cannot exceed "
                f"{_MAX_OVERRIDE_LIST_LEN} entries"
            ),
        )
    for entry in values:
        if not isinstance(entry, str):
            raise _bad_request(
                "unsafe_override_payload",
                f"override field {field_name!r} entries must be strings",
            )
        stripped = entry.strip()
        if not stripped:
            raise _bad_request(
                "unsafe_override_payload",
                f"override field {field_name!r} entries cannot be blank",
            )
        if len(stripped.encode("utf-8")) > _MAX_OVERRIDE_LIST_ENTRY_BYTES:
            raise _bad_request(
                "unsafe_override_payload",
                (
                    f"override field {field_name!r} entry exceeds "
                    f"{_MAX_OVERRIDE_LIST_ENTRY_BYTES} bytes"
                ),
            )
        if is_raw_token_shape(stripped):
            raise _bad_request(
                "unsafe_override_payload",
                f"override field {field_name!r} entry looks like a raw token",
            )


def _validate_promote_overrides(
    body: "CandidateDecisionCommandRequest",
) -> None:
    """Run the full override guard before SpecService.update_spec."""

    _validate_override_text("title", body.title)
    _validate_override_text("rationale", body.rationale)
    _validate_override_text("context", body.context)
    _validate_override_text("notes", body.notes)
    _validate_override_list(
        "alternatives_considered", body.alternatives_considered
    )


_OUTCOME_BY_ACTION: dict[str, str] = {
    "promote_to_spec_decision": (
        CognitivePendingOutcomeType.FORMAL_DECISION_PROMOTED.value
    ),
    "link_existing_decision": (
        CognitivePendingOutcomeType.EXISTING_DECISION_LINKED.value
    ),
    "dismiss": (
        CognitivePendingOutcomeType.CONTRADICTION_DISMISSED.value
    ),
    "no_action_required": (
        CognitivePendingOutcomeType.NO_ACTION_REQUIRED.value
    ),
}


def _propagate_pending_item_outcome(
    *,
    board_id: str,
    candidate: CandidateDecisionRecord,
    action_label: str,
    user_id: str,
    formal_decision_ref: str | None,
    dismissed_reason_code: str | None,
) -> None:
    """Best-effort update of the originating cognitive pending item.

    The candidate carries the (source_ref, source_generation_id,
    consolidation_session_id) triple. We use ``source_generation_id`` as
    the ``kg_generation_id`` for the pending ledger lookup and find the
    item with the matching ``source_ref``. If the ledger record does not
    exist (legacy candidate, dev fixture, scratch board, …) we silently
    skip — the candidate transition is the primary outcome.
    """

    outcome_type = _OUTCOME_BY_ACTION.get(action_label)
    if outcome_type is None:
        return

    generation_id = candidate.source_generation_id
    if not generation_id:
        return

    try:
        artifact_store = require_rebuild_audit_artifact_store()
        store = CognitiveConsolidationItemStore(
            artifact_store=artifact_store
        )
    except Exception:
        return

    if not store.record_exists(board_id, generation_id):
        return

    try:
        items = store.list_items(
            board_id, generation_id, limit=_PENDING_ITEM_SCAN_LIMIT,
        )
    except Exception:
        return

    target = next(
        (it for it in items if it.source_ref == candidate.source_ref),
        None,
    )
    if target is None:
        return

    promoted_ids = (
        [formal_decision_ref] if formal_decision_ref else None
    )

    try:
        store.update_item(
            board_id=board_id,
            kg_generation_id=generation_id,
            item_id=target.item_id,
            new_status=CognitiveItemStatus.CONSOLIDATED.value,
            updated_by_agent_id=user_id,
            consolidation_session_id=candidate.consolidation_session_id,
            reason=dismissed_reason_code,
            outcome_type=outcome_type,
            generated_candidate_decision_ids=[candidate.candidate_id],
            promoted_formal_decision_ids=promoted_ids,
        )
    except Exception:
        # Best-effort: failure to update the pending ledger MUST NOT
        # roll back the formal-decision write or candidate transition.
        return


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def _create_formal_decision(
    *,
    service: Any,
    spec_id: str,
    user_id: str,
    candidate: CandidateDecisionRecord,
    body: CandidateDecisionCommandRequest,
) -> dict[str, Any]:
    """Create a NEW formal decision on ``spec.decisions`` via the same
    write path used by ``okto_pulse_add_decision``. Returns the decision
    dict (with generated ``dec_<8-hex>`` id).
    """

    spec = await service.get_spec(spec_id)
    if spec is None:
        raise _not_found(
            "spec_not_found", f"spec {spec_id!r} does not exist"
        )

    if spec.board_id != body.board_id:
        raise _bad_request(
            "target_spec_board_mismatch",
            (
                f"spec {spec_id!r} belongs to board {spec.board_id!r}, "
                f"not the candidate board {body.board_id!r}"
            ),
        )

    decisions = list(spec.decisions or [])

    # Auto-supersede target (mirrors okto_pulse_add_decision).
    sup_id = body.supersedes_decision_id
    if sup_id:
        found = False
        for existing in decisions:
            if existing.get("id") == sup_id:
                existing["status"] = "superseded"
                found = True
                break
        if not found:
            raise _bad_request(
                "supersedes_decision_id_not_found",
                f"supersedes_decision_id {sup_id!r} not in spec.decisions",
            )

    decision = {
        "id": _generate_decision_id(),
        "title": (body.title or candidate.title),
        "rationale": (body.rationale or candidate.rationale),
        "context": body.context,
        "alternatives_considered": (
            list(body.alternatives_considered)
            if body.alternatives_considered
            else None
        ),
        "supersedes_decision_id": sup_id or None,
        "linked_requirements": (
            list(body.linked_requirements)
            if body.linked_requirements
            else []
        ),
        "linked_task_ids": None,
        "status": "active",
        "notes": body.notes,
    }
    decisions.append(decision)

    try:
        await service.update_spec(
            spec_id, user_id, SpecUpdate(decisions=decisions)
        )
    except ValueError as exc:
        raise _bad_request("spec_update_rejected", str(exc)) from exc
    return decision


async def _verify_existing_decision(
    *,
    service: Any,
    spec_id: str,
    formal_decision_id: str,
    expected_board_id: str,
) -> dict[str, Any]:
    spec = await service.get_spec(spec_id)
    if spec is None:
        raise _not_found(
            "spec_not_found", f"spec {spec_id!r} does not exist"
        )
    if spec.board_id != expected_board_id:
        raise _bad_request(
            "target_spec_board_mismatch",
            (
                f"spec {spec_id!r} belongs to board {spec.board_id!r}, "
                f"not the candidate board {expected_board_id!r}"
            ),
        )
    for existing in (spec.decisions or []):
        if existing.get("id") == formal_decision_id:
            return existing
    raise _not_found(
        "formal_decision_not_found",
        (
            f"decision {formal_decision_id!r} not found in spec "
            f"{spec_id!r}"
        ),
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/kg/cognitive-pending/candidate-decisions/{candidate_id}/command",
    response_model=CandidateDecisionCommandResponse,
    tags=["kg-cognitive-pending"],
)
async def submit_candidate_decision_command(
    body: CandidateDecisionCommandRequest,
    candidate_id: str = Path(..., min_length=1),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> CandidateDecisionCommandResponse:
    """Submit a lifecycle command against a candidate decision.

    Routes the request to the matching ``CandidateDecisionStore`` method
    AFTER performing the formal spec decision side-effect (for
    ``promote_to_spec_decision`` and ``link_existing_decision``). Every
    accepted command writes an ``audit_ref`` on the candidate record and
    emits exactly one bounded counter sample.
    """

    actor = RESTAdapterContract.actor(user_id, board_id=body.board_id)
    if await load_accessible_board(
        db,
        body.board_id,
        actor,
        allowed_share_permissions={"editor", "admin"},
    ) is None:
        raise HTTPException(status_code=404, detail="Board not found")

    store = CandidateDecisionStore(
        artifact_store=require_rebuild_audit_artifact_store()
    )

    candidate = store.get(body.board_id, candidate_id)
    if candidate is None:
        raise _not_found(
            "candidate_not_found",
            (
                f"candidate {candidate_id!r} not found on board "
                f"{body.board_id!r}"
            ),
        )
    if _is_terminal(candidate.status):
        raise _conflict(
            "candidate_already_terminal",
            (
                f"candidate {candidate_id!r} is already in terminal status "
                f"{candidate.status!r}"
            ),
        )

    audit_ref = _generate_audit_ref()
    service = db.services.specs
    formal_decision: dict[str, Any] | None = None

    if body.action == "promote_to_spec_decision":
        if not body.spec_id:
            raise _bad_request(
                "spec_id_required",
                "spec_id is required for promote_to_spec_decision",
            )
        _validate_promote_overrides(body)
        formal_decision = await _create_formal_decision(
            service=service,
            spec_id=body.spec_id,
            user_id=user_id,
            candidate=candidate,
            body=body,
        )
        await db.commit()
        try:
            updated = store.promote(
                board_id=body.board_id,
                candidate_id=candidate_id,
                formal_decision_ref=formal_decision["id"],
                audit_ref=audit_ref,
            )
        except CandidateDecisionError as exc:
            raise _candidate_error_to_http(exc) from exc
        action_label = CandidateDecisionAction.PROMOTE.value
        outcome_formal_ref = formal_decision["id"]
        outcome_reason: str | None = None

    elif body.action == "link_existing_decision":
        if not body.spec_id:
            raise _bad_request(
                "spec_id_required",
                "spec_id is required for link_existing_decision",
            )
        if not body.formal_decision_id:
            raise _bad_request(
                "formal_decision_id_required",
                "formal_decision_id is required for link_existing_decision",
            )
        formal_decision = await _verify_existing_decision(
            service=service,
            spec_id=body.spec_id,
            formal_decision_id=body.formal_decision_id,
            expected_board_id=body.board_id,
        )
        try:
            updated = store.link_existing(
                board_id=body.board_id,
                candidate_id=candidate_id,
                formal_decision_ref=body.formal_decision_id,
                audit_ref=audit_ref,
            )
        except CandidateDecisionError as exc:
            raise _candidate_error_to_http(exc) from exc
        action_label = CandidateDecisionAction.LINK_EXISTING.value
        outcome_formal_ref = body.formal_decision_id
        outcome_reason = None

    elif body.action == "dismiss":
        if not body.reason_code or not body.reason_code.strip():
            raise _bad_request(
                "reason_code_required",
                "reason_code is required for dismiss",
            )
        try:
            updated = store.dismiss(
                board_id=body.board_id,
                candidate_id=candidate_id,
                reason_code=body.reason_code,
                audit_ref=audit_ref,
            )
        except CandidateDecisionError as exc:
            raise _candidate_error_to_http(exc) from exc
        action_label = CandidateDecisionAction.DISMISS.value
        outcome_formal_ref = None
        outcome_reason = body.reason_code

    elif body.action == "no_action_required":
        if not body.reason_code or not body.reason_code.strip():
            raise _bad_request(
                "reason_code_required",
                "reason_code is required for no_action_required",
            )
        try:
            updated = store.mark_no_action_required(
                board_id=body.board_id,
                candidate_id=candidate_id,
                reason_code=body.reason_code,
                audit_ref=audit_ref,
            )
        except CandidateDecisionError as exc:
            raise _candidate_error_to_http(exc) from exc
        action_label = CandidateDecisionAction.NO_ACTION_REQUIRED.value
        outcome_formal_ref = None
        outcome_reason = body.reason_code

    else:
        raise _bad_request("invalid_action", f"unknown action {body.action!r}")

    # Best-effort: propagate the lifecycle outcome to the originating
    # cognitive pending item (KG-03A.3 path). Never rolls back the
    # candidate or formal-decision write on failure.
    _propagate_pending_item_outcome(
        board_id=body.board_id,
        candidate=updated,
        action_label=body.action,
        user_id=user_id,
        formal_decision_ref=outcome_formal_ref,
        dismissed_reason_code=outcome_reason,
    )

    projected = _project(updated)
    return CandidateDecisionCommandResponse(
        candidate_id=projected["candidate_id"],
        board_id=projected["board_id"],
        action=action_label,
        status=projected["status"],
        formal_decision_ref=projected["formal_decision_ref"],
        formal_decision=formal_decision,
        dismissed_reason_code=projected["dismissed_reason_code"],
        audit_ref=projected["audit_ref"],
        updated_at=projected["updated_at"],
    )
