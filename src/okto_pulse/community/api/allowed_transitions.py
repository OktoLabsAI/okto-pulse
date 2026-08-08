"""Allowed transition read model API."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.allowed_transitions import (
    ALLOWED_TRANSITIONS_SOURCE,
    ListAllowedTransitionsCommand,
    ListAllowedTransitionsUseCase,
)
from okto_pulse.core.application.use_cases.base import (
    CommandValidationError,
    EntityNotFoundError,
)
from okto_pulse.core.domain.guideline_policy import (
    GuidelineEnforcement,
    PolicyCurrentness,
)
from okto_pulse.core.domain.guideline_semantic_assessment import (
    SemanticAssessmentInadmissibilityCause,
)
from okto_pulse.core.domain.guideline_semantic_currentness import (
    SemanticAssessmentCurrentnessReason,
)
from okto_pulse.core.domain.guideline_semantic_transition import (
    PolicyTransitionDiagnosticCode,
    PolicyTransitionReasonCode,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import get_realm_id, require_user
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


class _ClosedResponseModel(BaseModel):
    """Fail closed when Core adds or renames a public contract field."""

    model_config = ConfigDict(extra="forbid")


class AllowedTransitionBindingDecisionResponse(_ClosedResponseModel):
    binding_id: str
    guideline_id: str
    enforcement: GuidelineEnforcement
    applicable_metric_count: int = Field(ge=0)
    allowed: bool
    assessment_available: bool
    receipt_id: str | None
    currentness: PolicyCurrentness | None
    currentness_reasons: list[
        SemanticAssessmentCurrentnessReason
    ] = Field(default_factory=list)
    inadmissibility_cause: SemanticAssessmentInadmissibilityCause | None
    failed_metric_count: int = Field(ge=0)
    waived_metric_count: int = Field(ge=0)
    blocking_metric_count: int = Field(ge=0)
    advisory_issue_count: int = Field(ge=0)
    skipped: bool
    diagnostic_codes: list[PolicyTransitionDiagnosticCode] = Field(
        default_factory=list
    )


class AllowedTransitionPolicyComplianceDecisionResponse(
    _ClosedResponseModel
):
    state: PolicyTransitionReasonCode
    allowed: bool | None
    policy_compliance_required: bool
    reason_codes: list[PolicyTransitionReasonCode] = Field(
        default_factory=list
    )
    decision_digest: str | None = None
    fence_digest: str | None = None
    receipt_ids: list[str] = Field(default_factory=list)
    currentness: PolicyCurrentness | None = None
    currentness_reasons: list[
        SemanticAssessmentCurrentnessReason
    ] = Field(default_factory=list)
    applicable_metric_count: int | None = Field(default=None, ge=0)
    applicable_blocking_metric_count: int | None = Field(
        default=None,
        ge=0,
    )
    failed_metric_count: int | None = Field(default=None, ge=0)
    blocking_metric_count: int | None = Field(default=None, ge=0)
    waived_metric_count: int | None = Field(default=None, ge=0)
    advisory_issue_count: int | None = Field(default=None, ge=0)
    skipped_binding_count: int | None = Field(default=None, ge=0)
    diagnostic_codes: list[PolicyTransitionDiagnosticCode] = Field(
        default_factory=list
    )
    binding_decisions: list[
        AllowedTransitionBindingDecisionResponse
    ] = Field(default_factory=list)


class AllowedTransitionResponse(_ClosedResponseModel):
    to_status: str
    label: str
    gate: str
    blocked_reason: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    policy_compliance: bool = False
    policy_compliance_decision: (
        AllowedTransitionPolicyComplianceDecisionResponse | None
    ) = None


class AllowedTransitionsResponse(_ClosedResponseModel):
    board_id: str
    entity_type: str
    entity_id: str | None
    current_status: str
    allowed_transitions: list[AllowedTransitionResponse]
    source: Literal["core_sdlc_registry_v1"] = ALLOWED_TRANSITIONS_SOURCE


@router.get(
    "/boards/{board_id}/allowed-transitions",
    response_model=AllowedTransitionsResponse,
)
async def get_allowed_transitions(
    board_id: str,
    entity_type: str = Query(...),
    entity_id: str | None = Query(None),
    current_status: str | None = Query(None),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return lifecycle actions from the canonical Core SDLC registry."""

    try:
        result = await ListAllowedTransitionsUseCase().execute(
            ListAllowedTransitionsCommand(
                board_id,
                entity_type,
                entity_id=entity_id,
                current_status=current_status,
            ),
            actor=RESTAdapterContract.actor(
                user_id,
                board_id=board_id,
                realm_id=realm_id,
            ),
            uow=uow,
        )
    except CommandValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except EntityNotFoundError as exc:
        detail = "Board not found" if exc.entity_type == "board" else f"{exc.entity_type.title()} not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return result.read_model.to_dict()
