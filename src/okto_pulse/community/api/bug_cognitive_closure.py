"""REST endpoint for bug cognitive-closure evidence evaluation
(S2 / card 13b43f3d / api_8c29ce5d).

POST /api/v1/bugs/{bug_id}/cognitive-closure/evaluate — classifies bug evidence
and returns the readiness verdict OBTAINED from CognitiveReadinessService
(readiness_effect / blocking / precedence_explanation mirrored, NEVER recomputed
— tr_28465cc7). Any resulting skip/no_action goes through the central write-path.
The MCP twin ``okto_pulse_kg_evaluate_bug_cognitive_closure`` shares this exact
core (``bug_cognitive_closure.evaluate_bug_cognitive_closure``) so REST and MCP
never diverge (br_4f1fedd9 / dec_7b75ce29).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.operational_rest import (
    BugNotFoundError,
    EvaluateBugCognitiveClosureByBugIdCommand,
    EvaluateBugCognitiveClosureByBugIdUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.kg.cognitive_readiness import CognitiveReadinessError
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.application.use_cases.learning_capture import (
    CreateLearningCaptureUseCase, GetLearningCaptureSourceUseCase,
)
from okto_pulse.core.application.use_cases.base import EntityNotFoundError, PermissionDeniedError
from okto_pulse.core.models.learning_capture import LearningCaptureCreateRequest
from okto_pulse.core.ports.kg_cognitive_source import CognitiveSourceError, CognitiveSourceConflict
from okto_pulse.community.api.permission_errors import permission_denied_http_error

router = APIRouter()


def _capture_error(exc):
    if isinstance(exc, PermissionDeniedError):
        return permission_denied_http_error(exc)
    if isinstance(exc, EntityNotFoundError):
        return HTTPException(status_code=404, detail={'code': 'bug_not_found'})
    if isinstance(exc, CognitiveSourceConflict):
        return HTTPException(status_code=409, detail={'code': exc.failure_reason})
    code = str(exc)
    unavailable = code in {'learning_capture_transaction_capability_unavailable',
        'bug_semantic_source_serialization_unsupported', 'bug_semantic_source_serialization_failed'}
    conflict = code in {'learning_capture_source_changed_or_unavailable', 'learning_capture_idempotency_conflict'}
    if not unavailable and not conflict and code not in {
        'learning_capture_request_invalid', 'learning_capture_payload_invalid', 'learning_capture_payload_limit',
        'learning_capture_evidence_ambiguous', 'learning_capture_evidence_not_authenticated',
    }:
        return HTTPException(status_code=503, detail={'code': 'learning_capture_unavailable'})
    return HTTPException(status_code=503 if unavailable else 409 if conflict else 422, detail={'code': code})


@router.get('/bugs/{bug_id}/learning-capture-context', tags=['bug-learning'])
async def get_learning_capture_context(
    bug_id: str, board_id: str = Query(min_length=1, max_length=4096),
    uow: PulseUnitOfWork = Depends(get_unit_of_work), actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        return await GetLearningCaptureSourceUseCase().execute(board_id=board_id, bug_id=bug_id,
            actor=RESTAdapterContract.actor(actor), uow=uow)
    except (EntityNotFoundError, PermissionDeniedError, ValueError, CognitiveSourceError, RuntimeError) as exc:
        raise _capture_error(exc) from exc


@router.post('/bugs/{bug_id}/learning-captures', tags=['bug-learning'])
async def create_learning_capture(
    bug_id: str, payload: LearningCaptureCreateRequest,
    uow: PulseUnitOfWork = Depends(get_unit_of_work), actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        record = await CreateLearningCaptureUseCase().execute(payload.command(bug_id),
            actor=RESTAdapterContract.actor(actor), uow=uow)
        return {'capture_id': record.payload['capture_id'], 'learning_id': record.node_id,
            'fingerprint': record.record_fingerprint, 'status': 'captured_pending_materialization'}
    except (EntityNotFoundError, PermissionDeniedError, ValueError, CognitiveSourceError, RuntimeError) as exc:
        raise _capture_error(exc) from exc


class BugCognitiveClosureEvaluateRequest(BaseModel):
    """api_8c29ce5d request: evidence, requested_action, optional revisit_at."""

    evidence: dict[str, Any] = Field(default_factory=dict)
    requested_action: str = "evaluate"
    reason_code: str | None = None
    justification: str | None = None
    evidence_refs: list[str] | None = None
    revisit_at: str | None = None


@router.post(
    "/bugs/{bug_id}/cognitive-closure/evaluate",
    tags=["bug-cognitive-closure"],
)
async def evaluate_bug_cognitive_closure_endpoint(
    bug_id: str,
    payload: BugCognitiveClosureEvaluateRequest,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        result = await EvaluateBugCognitiveClosureByBugIdUseCase().execute(
            EvaluateBugCognitiveClosureByBugIdCommand(
                bug_id,
                payload.evidence,
                payload.requested_action,
                payload.reason_code,
                payload.justification,
                payload.evidence_refs,
                payload.revisit_at,
            ),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result.data
    except BugNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "bug_not_found", "message": f"Bug {bug_id!r} not found."},
        ) from exc
    except CognitiveReadinessError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc
