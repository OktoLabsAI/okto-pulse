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

from fastapi import APIRouter, Depends, HTTPException
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

router = APIRouter()


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
