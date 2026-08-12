"""REST endpoints for bounded, human-readable validation cycles."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory
from okto_pulse.community.adapters.sqlalchemy_validation_cycle import (
    CommunitySqlAlchemyValidationCycleReader,
)
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.validation_cycle import (
    GetValidationCycleCommand,
    GetValidationTechnicalAuditCommand,
    ValidationCycleReadUseCases,
)
from okto_pulse.core.domain.quality_assessment import AssessmentSubjectType
from okto_pulse.core.domain.validation_cycle import (
    ValidationCycleResultType,
    ValidationCycleState,
    ValidationEditionExceptionType,
)
from okto_pulse.core.models.validation_cycle import (
    project_validation_cycle,
    project_validation_technical_audit,
)
from okto_pulse.core.ports.validation_cycle import (
    ValidationCycleReadAccessDenied,
    ValidationCycleResultNotFound,
    ValidationCycleSubjectNotFound,
)


router = APIRouter()


class _ClosedResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationCycleResultResponse(_ClosedResponseModel):
    result_id: str
    result_type: ValidationCycleResultType
    subject_edition: int | None = Field(ge=1)
    status: str
    summary: dict[str, Any]


class ValidationSubmissionFenceResponse(_ClosedResponseModel):
    expected_validation_edition: int = Field(ge=1)
    expected_subject_version: int = Field(ge=1)
    expected_head_revision: int = Field(ge=0)


class ValidationCycleCheckResponse(_ClosedResponseModel):
    result_type: ValidationCycleResultType
    status: str
    summary: str


class SpecValidationCycleResponse(_ClosedResponseModel):
    subject_type: Literal["spec"]
    subject_id: str
    edition: int = Field(ge=1)
    subject_status: str
    visible_sections: list[ValidationCycleResultType]
    cycle_state: ValidationCycleState | None = None
    current_result: ValidationCycleResultResponse | None = None
    previous_result_count: int | None = Field(default=None, ge=0)
    previous_results: list[ValidationCycleResultResponse] | None = None
    submission_fence: ValidationSubmissionFenceResponse | None = None
    checks: list[ValidationCycleCheckResponse]
    remaining_actions: list[str]


class QualityValidationCycleResponse(_ClosedResponseModel):
    subject_type: Literal["ideation", "refinement"]
    subject_id: str
    edition: int = Field(ge=1)
    subject_status: str
    visible_sections: list[ValidationCycleResultType]
    cycle_state: ValidationCycleState
    current_result: ValidationCycleResultResponse | None
    previous_result_count: int = Field(ge=0)
    previous_results: list[ValidationCycleResultResponse]
    submission_fence: ValidationSubmissionFenceResponse


class ValidationEditionExceptionResponse(_ClosedResponseModel):
    exception_id: str
    exception_type: ValidationEditionExceptionType
    subject_edition: int = Field(ge=1)
    status: str
    reason: str
    actor_id: str
    recorded_at: str


class ValidationTechnicalAuditDetailsResponse(_ClosedResponseModel):
    receipt_id: str
    subject_version: int = Field(ge=1)
    head_revision: int = Field(ge=1)
    digests: dict[str, str]
    visible_exception_types: list[ValidationEditionExceptionType]
    exceptions: list[ValidationEditionExceptionResponse]


class ValidationTechnicalAuditResponse(_ClosedResponseModel):
    subject_type: AssessmentSubjectType
    subject_id: str
    result_id: str
    result_type: ValidationCycleResultType
    subject_edition: int | None = Field(ge=1)
    technical_audit: ValidationTechnicalAuditDetailsResponse


def _reader() -> CommunitySqlAlchemyValidationCycleReader:
    return CommunitySqlAlchemyValidationCycleReader(get_session_factory())


def _http_error(
    exc: Exception,
    *,
    subject_type: AssessmentSubjectType,
) -> HTTPException:
    if isinstance(exc, ValidationCycleReadAccessDenied):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "permission_denied", "retryable": False},
        )
    if isinstance(exc, ValidationCycleSubjectNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": f"{subject_type.value}_not_found",
                "retryable": False,
            },
        )
    if isinstance(exc, ValidationCycleResultNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": exc.code, "retryable": False},
        )
    if isinstance(exc, (TypeError, ValueError)):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": str(exc), "retryable": False},
        )
    raise exc


async def _cycle(
    *,
    subject_type: AssessmentSubjectType,
    subject_id: str,
    include_previous: bool,
    offset: int,
    limit: int,
    user_id: str,
    reader: CommunitySqlAlchemyValidationCycleReader,
) -> dict[str, Any]:
    try:
        result = await ValidationCycleReadUseCases(reader=reader).get_cycle(
            GetValidationCycleCommand(
                subject_type=subject_type,
                subject_id=subject_id,
                include_previous=include_previous,
                offset=offset,
                limit=limit,
            ),
            actor=RESTAdapterContract.actor(user_id),
        )
    except Exception as exc:
        raise _http_error(exc, subject_type=subject_type) from exc
    return project_validation_cycle(result)


async def _audit(
    *,
    subject_type: AssessmentSubjectType,
    subject_id: str,
    result_id: str,
    result_type: ValidationCycleResultType,
    user_id: str,
    reader: CommunitySqlAlchemyValidationCycleReader,
) -> dict[str, Any]:
    try:
        result = await ValidationCycleReadUseCases(reader=reader).get_technical_audit(
            GetValidationTechnicalAuditCommand(
                subject_type=subject_type,
                subject_id=subject_id,
                result_id=result_id,
                result_type=result_type,
            ),
            actor=RESTAdapterContract.actor(user_id),
        )
    except Exception as exc:
        raise _http_error(exc, subject_type=subject_type) from exc
    return project_validation_technical_audit(result)


def _register_subject_routes(subject_type: AssessmentSubjectType) -> None:
    plural = f"{subject_type.value}s"

    async def get_cycle(
        subject_id: str,
        include_previous: bool = Query(False),
        offset: int = Query(0, ge=0),
        limit: int = Query(25, ge=1, le=100),
        user_id: str = Depends(require_user),
        reader: CommunitySqlAlchemyValidationCycleReader = Depends(_reader),
    ) -> dict[str, Any]:
        return await _cycle(
            subject_type=subject_type,
            subject_id=subject_id,
            include_previous=include_previous,
            offset=offset,
            limit=limit,
            user_id=user_id,
            reader=reader,
        )

    async def get_audit(
        subject_id: str,
        result_id: str,
        result_type: ValidationCycleResultType = Query(...),
        user_id: str = Depends(require_user),
        reader: CommunitySqlAlchemyValidationCycleReader = Depends(_reader),
    ) -> dict[str, Any]:
        return await _audit(
            subject_type=subject_type,
            subject_id=subject_id,
            result_id=result_id,
            result_type=result_type,
            user_id=user_id,
            reader=reader,
        )

    get_cycle.__name__ = f"get_{subject_type.value}_validation_cycle"
    get_audit.__name__ = f"get_{subject_type.value}_validation_technical_audit"
    router.add_api_route(
        f"/{plural}/{{subject_id}}/validation-cycle",
        get_cycle,
        methods=["GET"],
        response_model=(
            SpecValidationCycleResponse
            if subject_type is AssessmentSubjectType.SPEC
            else QualityValidationCycleResponse
        ),
        response_model_exclude_unset=True,
    )
    router.add_api_route(
        f"/{plural}/{{subject_id}}/validation-cycle/results/{{result_id}}/technical-audit",
        get_audit,
        methods=["GET"],
        response_model=ValidationTechnicalAuditResponse,
    )


for _subject_type in AssessmentSubjectType:
    _register_subject_routes(_subject_type)


__all__ = ["router"]
