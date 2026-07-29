"""REST surface for governed SK-A quality assessments."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory
from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
    CommunitySqlAlchemyQualityAssessmentPreflightReader,
)
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work_factory
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.inbound.quality_assessment_error import (
    project_quality_assessment_error,
)
from okto_pulse.core.application.use_cases.quality_assessment import (
    GetCurrentQualityAssessmentCommand,
    GetQualityAssessmentReceiptCommand,
    ListQualityAssessmentsCommand,
    ListQualityFindingsCommand,
    ListQualityReceiptFindingsCommand,
    QualityAssessmentReadUseCases,
    RecordAmbiguityAssessmentCommand,
    RecordAmbiguityAssessmentUseCase,
)
from okto_pulse.core.domain.quality_assessment import (
    AssessmentKind,
    AssessmentReceiptState,
    AssessmentStaleReason,
    AssessmentSubjectType,
    FindingAnchorType,
    FindingLifecycle,
    FindingSeverity,
    MAX_PROPOSED_QUESTIONS_PER_ASSESSMENT_V1,
    QualityAssessmentContractError,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.quality_assessment import (
    QualityFindingInput,
    QualityProposedQuestionInput,
    project_current_quality_assessment,
    project_quality_finding,
    project_quality_receipt_currentness,
    project_quality_receipt_view,
)
from okto_pulse.core.models.schemas import PageEnvelope
from okto_pulse.core.ports.quality_assessment import (
    AssessmentSubjectNotFound,
    QualityAssessmentPersistenceError,
)
from okto_pulse.core.services.quality_assessment import (
    QualityAssessmentError,
    QualityAssessmentErrorCategory,
    QualityAssessmentValidationError,
)

router = APIRouter()

_REST_PAGE_LIMITS = frozenset({25, 50, 100})


class RecordAmbiguityAssessmentRequest(BaseModel):
    """Caller-owned assessment data; subject identities are path/server-owned."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1)
    expected_subject_version: int = Field(ge=1)
    expected_head_revision: int = Field(ge=0)
    score: float = Field(ge=1, le=5)
    findings: list[QualityFindingInput] = Field(default_factory=list)
    proposed_questions: list[QualityProposedQuestionInput] = Field(
        default_factory=list,
        json_schema_extra={
            "maxItems": MAX_PROPOSED_QUESTIONS_PER_ASSESSMENT_V1
        },
    )


class RecordAmbiguityAssessmentResponse(BaseModel):
    outcome: Literal["success"]
    replayed: bool
    receipt_id: str
    head_revision: int
    qa_id_map: dict[str, str]


class QualityCurrentnessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current: bool
    state: Literal["current", "stale"]
    stale_reasons: list[AssessmentStaleReason]


class QualityAssessmentListItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt: dict[str, Any]
    is_head: bool
    state: AssessmentReceiptState
    currentness: QualityCurrentnessResponse


class QualityGatePreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applicable: bool
    enabled: bool
    allowed: bool
    reason_code: Literal[
        "not_applicable",
        "ambiguity_gate_disabled",
        "ambiguity_gate_skipped",
        "ambiguity_assessment_stale",
        "ambiguity_score_exceeds_threshold",
        "ambiguity_gate_ready",
    ]
    threshold: int | float | None
    score: int | float
    skipped: bool


class CurrentQualityAssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt: dict[str, Any]
    head_revision: int
    currentness: Literal["current", "stale"]
    stale_reasons: list[AssessmentStaleReason]
    gate_preview: QualityGatePreviewResponse


class QualityAssessmentReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt: dict[str, Any]
    currentness: Literal["current", "stale"]
    stale_reasons: list[AssessmentStaleReason]


class QualityEvidenceRefResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_id: str
    source_version: int
    content_hash: str


class QualityFindingAnchorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_id: str
    subject_type: AssessmentSubjectType
    subject_id: str
    subject_version: int
    input_digest: str
    anchor_type: FindingAnchorType
    anchor_ref: str | None
    excerpt_hash: str | None


class QualityFindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    receipt_id: str
    assessment_kind: AssessmentKind
    finding_key: str
    category_code: str
    taxonomy_version: str
    severity: FindingSeverity
    confidence: float
    deterministic: bool
    blocking_eligible: bool
    title: str
    detail: str
    anchor: QualityFindingAnchorResponse
    evidence_refs: list[QualityEvidenceRefResponse]
    lifecycle: FindingLifecycle
    created_at: str
    remediation: str | None
    rule_code: str | None


def _preflight_reader() -> CommunitySqlAlchemyQualityAssessmentPreflightReader:
    return CommunitySqlAlchemyQualityAssessmentPreflightReader(
        get_session_factory()
    )


def _quality_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, QualityAssessmentError):
        status_code = {
            QualityAssessmentErrorCategory.INVALID_ARGUMENT: (
                status.HTTP_400_BAD_REQUEST
            ),
            QualityAssessmentErrorCategory.UNPROCESSABLE: (
                status.HTTP_422_UNPROCESSABLE_CONTENT
            ),
            QualityAssessmentErrorCategory.FORBIDDEN: status.HTTP_403_FORBIDDEN,
            QualityAssessmentErrorCategory.NOT_FOUND: status.HTTP_404_NOT_FOUND,
            QualityAssessmentErrorCategory.CONFLICT: status.HTTP_409_CONFLICT,
            QualityAssessmentErrorCategory.INTERNAL: (
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
        }[exc.category]
        return HTTPException(
            status_code=status_code,
            detail=project_quality_assessment_error(exc),
        )
    if isinstance(exc, AssessmentSubjectNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=project_quality_assessment_error(exc),
        )
    if isinstance(exc, QualityAssessmentContractError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=project_quality_assessment_error(exc),
        )
    if isinstance(exc, QualityAssessmentPersistenceError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=project_quality_assessment_error(exc),
        )
    if isinstance(exc, (TypeError, ValueError)):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=project_quality_assessment_error(exc),
        )
    raise exc


def _validate_rest_page(offset: int, limit: int) -> None:
    if offset < 0 or limit not in _REST_PAGE_LIMITS:
        raise _quality_http_error(
            QualityAssessmentValidationError(
                "invalid_pagination",
                "offset must be non-negative and limit must be 25, 50, or 100",
                details={"allowed_limits": sorted(_REST_PAGE_LIMITS)},
                category=QualityAssessmentErrorCategory.INVALID_ARGUMENT,
            )
        )


def _validate_rest_page_query(request: Request) -> None:
    """Reject malformed pagination before FastAPI emits an untyped 422."""

    values = {"offset": 0, "limit": 25}
    for field in values:
        raw = request.query_params.get(field)
        if raw is None:
            continue
        try:
            values[field] = int(raw)
        except (TypeError, ValueError) as exc:
            raise _quality_http_error(
                QualityAssessmentValidationError(
                    "invalid_pagination",
                    f"quality_assessment_page_{field}_invalid",
                    details={
                        "allowed_limits": sorted(_REST_PAGE_LIMITS),
                    },
                    category=QualityAssessmentErrorCategory.INVALID_ARGUMENT,
                )
            ) from exc
    _validate_rest_page(values["offset"], values["limit"])


def _validate_question_budget(
    data: RecordAmbiguityAssessmentRequest,
) -> None:
    actual = len(data.proposed_questions)
    if actual > MAX_PROPOSED_QUESTIONS_PER_ASSESSMENT_V1:
        raise _quality_http_error(
            QualityAssessmentValidationError(
                "question_budget_exceeded",
                "an assessment may propose at most five questions",
                details={
                    "maximum": MAX_PROPOSED_QUESTIONS_PER_ASSESSMENT_V1,
                    "actual": actual,
                },
            )
        )


def _subject_kind_supported(
    subject_type: AssessmentSubjectType,
    assessment_kind: AssessmentKind,
) -> bool:
    if subject_type in {
        AssessmentSubjectType.IDEATION,
        AssessmentSubjectType.REFINEMENT,
    }:
        return assessment_kind is AssessmentKind.AMBIGUITY
    return assessment_kind in {
        AssessmentKind.SPEC_VALIDATION,
        AssessmentKind.REQUIREMENT_LINT,
    }


def _assessment_kind_query_guard(
    subject_type: AssessmentSubjectType,
    *,
    required: bool,
) -> Callable[[Request], None]:
    """Build a raw-query guard so FastAPI cannot emit an untyped 422."""

    def validate(request: Request) -> None:
        raw_kind = request.query_params.get("assessment_kind")
        if raw_kind is None and not required:
            return
        try:
            assessment_kind = AssessmentKind(raw_kind)
        except (TypeError, ValueError) as exc:
            raise _quality_http_error(
                QualityAssessmentValidationError(
                    "assessment_subject_kind_unsupported",
                    "assessment_kind must match the supported subject matrix",
                    category=QualityAssessmentErrorCategory.INVALID_ARGUMENT,
                )
            ) from exc
        if not _subject_kind_supported(subject_type, assessment_kind):
            raise _quality_http_error(
                QualityAssessmentValidationError(
                    "assessment_subject_kind_unsupported",
                    "assessment_kind must match the supported subject matrix",
                    category=QualityAssessmentErrorCategory.INVALID_ARGUMENT,
                )
            )

    mode = "required" if required else "optional"
    validate.__name__ = (
        f"validate_{subject_type.value}_{mode}_quality_kind"
    )
    return validate


async def _subject_board_id(
    *,
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader,
    subject_type: AssessmentSubjectType,
    subject_id: str,
) -> str:
    try:
        return await reader.resolve_subject_board_id(
            subject_type=subject_type,
            subject_id=subject_id,
            realm_scope=RealmScope.local(),
        )
    except Exception as exc:
        raise _quality_http_error(exc) from exc


async def _record_ambiguity(
    *,
    request: Request,
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader,
    user_id: str,
    subject_type: AssessmentSubjectType,
    subject_id: str,
    data: RecordAmbiguityAssessmentRequest,
) -> RecordAmbiguityAssessmentResponse:
    _validate_question_budget(data)
    board_id = await _subject_board_id(
        reader=reader,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    try:
        result = await RecordAmbiguityAssessmentUseCase(
            preflight_reader=reader,
            uow_factory=get_unit_of_work_factory(request),
        ).execute(
            RecordAmbiguityAssessmentCommand(
                board_id=board_id,
                subject_type=subject_type,
                subject_id=subject_id,
                idempotency_key=data.idempotency_key,
                expected_subject_version=data.expected_subject_version,
                expected_head_revision=data.expected_head_revision,
                score=data.score,
                findings=tuple(item.to_domain() for item in data.findings),
                proposed_questions=tuple(
                    item.to_domain() for item in data.proposed_questions
                ),
            ),
            actor=actor,
        )
    except Exception as exc:
        raise _quality_http_error(exc) from exc
    return RecordAmbiguityAssessmentResponse(
        outcome="success",
        replayed=result.replayed,
        receipt_id=result.receipt_id,
        head_revision=result.head_revision,
        qa_id_map=dict(result.qa_id_map),
    )


@router.post(
    "/ideations/{ideation_id}/quality-assessments",
    response_model=RecordAmbiguityAssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_ideation_ambiguity_assessment(
    ideation_id: str,
    data: RecordAmbiguityAssessmentRequest,
    request: Request,
    user_id: str = Depends(require_user),
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader = Depends(
        _preflight_reader
    ),
) -> RecordAmbiguityAssessmentResponse:
    return await _record_ambiguity(
        request=request,
        reader=reader,
        user_id=user_id,
        subject_type=AssessmentSubjectType.IDEATION,
        subject_id=ideation_id,
        data=data,
    )


@router.post(
    "/refinements/{refinement_id}/quality-assessments",
    response_model=RecordAmbiguityAssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_refinement_ambiguity_assessment(
    refinement_id: str,
    data: RecordAmbiguityAssessmentRequest,
    request: Request,
    user_id: str = Depends(require_user),
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader = Depends(
        _preflight_reader
    ),
) -> RecordAmbiguityAssessmentResponse:
    return await _record_ambiguity(
        request=request,
        reader=reader,
        user_id=user_id,
        subject_type=AssessmentSubjectType.REFINEMENT,
        subject_id=refinement_id,
        data=data,
    )


async def _read_runtime(
    *,
    request: Request,
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader,
) -> QualityAssessmentReadUseCases:
    return QualityAssessmentReadUseCases(
        preflight_reader=reader,
        uow_factory=get_unit_of_work_factory(request),
    )


async def _list_assessments(
    *,
    request: Request,
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader,
    user_id: str,
    subject_type: AssessmentSubjectType,
    subject_id: str,
    offset: int,
    limit: int,
    assessment_kind: AssessmentKind | None,
    state_filter: AssessmentReceiptState | None,
) -> PageEnvelope[QualityAssessmentListItemResponse]:
    _validate_rest_page(offset, limit)
    board_id = await _subject_board_id(
        reader=reader,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    try:
        page = await (
            await _read_runtime(request=request, reader=reader)
        ).list_assessments(
            ListQualityAssessmentsCommand(
                board_id=board_id,
                subject_type=subject_type,
                subject_id=subject_id,
                offset=offset,
                limit=limit,
                assessment_kind=assessment_kind,
                state=state_filter,
            ),
            actor=actor,
        )
    except Exception as exc:
        raise _quality_http_error(exc) from exc
    return PageEnvelope[QualityAssessmentListItemResponse](
        items=[project_quality_receipt_view(item) for item in page.items],
        total_filtered=page.total_filtered,
        total_overall=page.total_overall,
        offset=page.offset,
        limit=page.limit,
    )


async def _list_findings(
    *,
    request: Request,
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader,
    user_id: str,
    subject_type: AssessmentSubjectType,
    subject_id: str,
    offset: int,
    limit: int,
    receipt_id: str | None,
    assessment_kind: AssessmentKind | None,
    category_code: str | None,
    severity: FindingSeverity | None,
) -> PageEnvelope[QualityFindingResponse]:
    _validate_rest_page(offset, limit)
    board_id = await _subject_board_id(
        reader=reader,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    try:
        page = await (
            await _read_runtime(request=request, reader=reader)
        ).list_findings(
            ListQualityFindingsCommand(
                board_id=board_id,
                subject_type=subject_type,
                subject_id=subject_id,
                offset=offset,
                limit=limit,
                receipt_id=receipt_id,
                assessment_kind=assessment_kind,
                category_code=category_code,
                severity=severity,
            ),
            actor=actor,
        )
    except Exception as exc:
        raise _quality_http_error(exc) from exc
    return PageEnvelope[QualityFindingResponse](
        items=[project_quality_finding(item) for item in page.items],
        total_filtered=page.total_filtered,
        total_overall=page.total_overall,
        offset=page.offset,
        limit=page.limit,
    )


def _subject_routes(subject_type: AssessmentSubjectType) -> APIRouter:
    """Build identical closed read contracts for the three subject literals."""

    subject_router = APIRouter()
    plural = f"{subject_type.value}s"
    subject_id_name = f"{subject_type.value}_id"
    validate_optional_kind = _assessment_kind_query_guard(
        subject_type,
        required=False,
    )
    validate_current_kind = _assessment_kind_query_guard(
        subject_type,
        required=True,
    )

    async def list_assessments(
        request: Request,
        subject_id: str = Path(..., alias=subject_id_name),
        offset: int = Query(0, ge=0),
        limit: int = Query(25),
        assessment_kind: AssessmentKind | None = Query(None),
        state_filter: AssessmentReceiptState | None = Query(None, alias="state"),
        user_id: str = Depends(require_user),
        reader: CommunitySqlAlchemyQualityAssessmentPreflightReader = Depends(
            _preflight_reader
        ),
    ) -> PageEnvelope[QualityAssessmentListItemResponse]:
        return await _list_assessments(
            request=request,
            reader=reader,
            user_id=user_id,
            subject_type=subject_type,
            subject_id=subject_id,
            offset=offset,
            limit=limit,
            assessment_kind=assessment_kind,
            state_filter=state_filter,
        )

    async def list_findings(
        request: Request,
        subject_id: str = Path(..., alias=subject_id_name),
        offset: int = Query(0, ge=0),
        limit: int = Query(25),
        receipt_id: str | None = Query(None),
        assessment_kind: AssessmentKind | None = Query(None),
        category_code: str | None = Query(None),
        severity: FindingSeverity | None = Query(None),
        user_id: str = Depends(require_user),
        reader: CommunitySqlAlchemyQualityAssessmentPreflightReader = Depends(
            _preflight_reader
        ),
    ) -> PageEnvelope[QualityFindingResponse]:
        return await _list_findings(
            request=request,
            reader=reader,
            user_id=user_id,
            subject_type=subject_type,
            subject_id=subject_id,
            offset=offset,
            limit=limit,
            receipt_id=receipt_id,
            assessment_kind=assessment_kind,
            category_code=category_code,
            severity=severity,
        )

    async def get_current(
        request: Request,
        subject_id: str = Path(..., alias=subject_id_name),
        assessment_kind: AssessmentKind = Query(...),
        user_id: str = Depends(require_user),
        reader: CommunitySqlAlchemyQualityAssessmentPreflightReader = Depends(
            _preflight_reader
        ),
    ) -> CurrentQualityAssessmentResponse:
        board_id = await _subject_board_id(
            reader=reader,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        actor = RESTAdapterContract.actor(user_id, board_id=board_id)
        try:
            current = await (
                await _read_runtime(request=request, reader=reader)
            ).get_current(
                GetCurrentQualityAssessmentCommand(
                    board_id=board_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    assessment_kind=assessment_kind,
                ),
                actor=actor,
            )
        except Exception as exc:
            raise _quality_http_error(exc) from exc
        return project_current_quality_assessment(current)

    list_assessments.__name__ = f"list_{subject_type.value}_quality_assessments"
    list_findings.__name__ = f"list_{subject_type.value}_quality_findings"
    get_current.__name__ = f"get_current_{subject_type.value}_quality_assessment"
    subject_router.add_api_route(
        f"/{plural}/{{{subject_id_name}}}/quality-assessments",
        list_assessments,
        methods=["GET"],
        response_model=PageEnvelope[QualityAssessmentListItemResponse],
        dependencies=[
            Depends(_validate_rest_page_query),
            Depends(validate_optional_kind),
        ],
    )
    subject_router.add_api_route(
        f"/{plural}/{{{subject_id_name}}}/quality-findings",
        list_findings,
        methods=["GET"],
        response_model=PageEnvelope[QualityFindingResponse],
        dependencies=[
            Depends(_validate_rest_page_query),
            Depends(validate_optional_kind),
        ],
    )
    subject_router.add_api_route(
        f"/{plural}/{{{subject_id_name}}}/quality-assessments/current",
        get_current,
        methods=["GET"],
        response_model=CurrentQualityAssessmentResponse,
        dependencies=[Depends(validate_current_kind)],
    )
    return subject_router


for _subject_type in AssessmentSubjectType:
    router.include_router(_subject_routes(_subject_type))


@router.get(
    "/quality-assessment-receipts/{receipt_id}",
    response_model=QualityAssessmentReceiptResponse,
)
async def get_quality_assessment_receipt(
    receipt_id: str,
    request: Request,
    user_id: str = Depends(require_user),
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader = Depends(
        _preflight_reader
    ),
) -> QualityAssessmentReceiptResponse:
    actor = RESTAdapterContract.actor(user_id)
    try:
        result = await (
            await _read_runtime(request=request, reader=reader)
        ).get_receipt(
            GetQualityAssessmentReceiptCommand(receipt_id=receipt_id),
            actor=actor,
        )
    except Exception as exc:
        raise _quality_http_error(exc) from exc
    return project_quality_receipt_currentness(
        result.receipt,
        result.currentness,
    )


@router.get(
    "/quality-assessment-receipts/{receipt_id}/findings",
    response_model=PageEnvelope[QualityFindingResponse],
    dependencies=[Depends(_validate_rest_page_query)],
)
async def list_quality_assessment_receipt_findings(
    receipt_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(25),
    category_code: str | None = Query(None),
    severity: FindingSeverity | None = Query(None),
    user_id: str = Depends(require_user),
    reader: CommunitySqlAlchemyQualityAssessmentPreflightReader = Depends(
        _preflight_reader
    ),
) -> PageEnvelope[QualityFindingResponse]:
    _validate_rest_page(offset, limit)
    actor = RESTAdapterContract.actor(user_id)
    try:
        page = await (
            await _read_runtime(request=request, reader=reader)
        ).list_receipt_findings(
            ListQualityReceiptFindingsCommand(
                offset=offset,
                limit=limit,
                receipt_id=receipt_id,
                category_code=category_code,
                severity=severity,
            ),
            actor=actor,
        )
    except Exception as exc:
        raise _quality_http_error(exc) from exc
    return PageEnvelope[QualityFindingResponse](
        items=[project_quality_finding(item) for item in page.items],
        total_filtered=page.total_filtered,
        total_overall=page.total_overall,
        offset=page.offset,
        limit=page.limit,
    )


__all__ = [
    "RecordAmbiguityAssessmentRequest",
    "RecordAmbiguityAssessmentResponse",
    "router",
]
