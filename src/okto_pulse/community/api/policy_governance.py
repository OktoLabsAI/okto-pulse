"""Board-scoped REST surface for versioned guideline policy governance.

The adapter is intentionally thin:

* request models are closed and reject unknown fields;
* authentication/capability-bearing actor construction precedes UoW access;
* commands and use cases remain Core-owned;
* keyset cursors and projections are passed without offset fallbacks; and
* errors use a temporary structured projection local to this adapter until the
  shared B13 inbound projector is installed.

Literal ``import``/``export`` and action routes in this module must be included
before the historical ``/guidelines/{guideline_id}`` router.  Starlette matches
routes in registration order.
"""

from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from enum import Enum
from functools import reduce
from importlib import import_module
import operator
import types
from typing import (
    Annotated,
    Any,
    Literal,
    Protocol,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.physical_identity import (
    CommunityBoardId,
    validate_community_board_id,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.domain.guideline_compliance import (
    GuidelineRevisionListItem as CoreGuidelineRevisionListItem,
    PolicyCursorCodec,
)
from okto_pulse.core.domain.guideline_import_export import (
    guideline_export_payload,
)
from okto_pulse.core.domain.guideline_lifecycle import GuidelineVersionBump
from okto_pulse.core.domain.guideline_semantic_projection import (
    SemanticAssessmentDetail as CoreSemanticAssessmentDetail,
    SemanticAssessmentFull as CoreSemanticAssessmentFull,
    SemanticAssessmentSummary as CoreSemanticAssessmentSummary,
    SemanticFindingDetail as CoreSemanticFindingDetail,
    SemanticFindingFull as CoreSemanticFindingFull,
    SemanticFindingSummary as CoreSemanticFindingSummary,
    SemanticMetricResultDetail as CoreSemanticMetricResultDetail,
    SemanticMetricResultFull as CoreSemanticMetricResultFull,
    SemanticSkipDetail as CoreSemanticSkipDetail,
    SemanticSkipFull as CoreSemanticSkipFull,
    SemanticSkipSummary as CoreSemanticSkipSummary,
    SemanticWaiverDetail as CoreSemanticWaiverDetail,
    SemanticWaiverFull as CoreSemanticWaiverFull,
    SemanticWaiverSummary as CoreSemanticWaiverSummary,
)
from okto_pulse.core.domain.guideline_semantic_exceptions import (
    SemanticMetricWaiverEvent as CoreSemanticMetricWaiverEvent,
)
from okto_pulse.core.domain.guideline_semantic_v2 import (
    SEMANTIC_PINPOINT_DETAIL_MAX_LENGTH,
    SEMANTIC_PINPOINT_KEY_MAX_LENGTH,
    SEMANTIC_PINPOINT_REMEDIATION_MAX_LENGTH,
    SEMANTIC_PINPOINT_TITLE_MAX_LENGTH,
)
from okto_pulse.core.domain.guideline_policy import (
    GUIDELINE_BINDING_ID_MAX_LENGTH,
    GUIDELINE_ID_MAX_LENGTH,
    GUIDELINE_RETIREMENT_ID_MAX_LENGTH,
    GUIDELINE_REVISION_ID_MAX_LENGTH,
    GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    GUIDELINE_TITLE_MAX_LENGTH,
    POLICY_ACTOR_ID_MAX_LENGTH,
    POLICY_FINDING_ID_MAX_LENGTH,
    POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    POLICY_IMPACT_RECEIPT_ID_MAX_LENGTH,
    POLICY_METRIC_CODE_MAX_LENGTH,
    POLICY_METRIC_ID_MAX_LENGTH,
    POLICY_RECEIPT_ID_MAX_LENGTH,
    POLICY_SQL_INTEGER_MAX,
    POLICY_SUBJECT_ID_MAX_LENGTH,
    POLICY_WAIVER_ID_MAX_LENGTH,
    BoardGuidelineBinding as CoreBoardGuidelineBinding,
    Guideline as CoreGuideline,
    GuidelineHead as CoreGuidelineHead,
    GuidelineImpactItem as CoreGuidelineImpactItem,
    GuidelineImpactReceipt as CoreGuidelineImpactReceipt,
    GuidelineRetirement as CoreGuidelineRetirement,
    GuidelineRevision as CoreGuidelineRevision,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.repositories import PulseUnitOfWork


class PolicyGovernanceRoute(APIRoute):
    """Scope FastAPI validation projection to the B13 governance surface."""

    def get_route_handler(self):
        original = super().get_route_handler()
        preflight_import_envelope = (
            self.path == "/boards/{board_id}/guidelines/import"
        )

        async def governed_route_handler(request: Request):
            try:
                board_id = request.path_params.get("board_id")
                if board_id is not None:
                    validate_community_board_id(board_id)
                if preflight_import_envelope:
                    GuidelineExportV3Request.model_validate_json(
                        await request.body()
                    )
                return await original(request)
            except (RequestValidationError, ValidationError):
                from okto_pulse.core.application.use_cases.base import (
                    CommandValidationError,
                )
                from okto_pulse.core.inbound.guideline_policy_error import (
                    project_guideline_policy_error,
                )

                detail = project_guideline_policy_error(
                    CommandValidationError("request_validation_failed")
                )
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": detail},
                )
            except HTTPException as exc:
                detail = _governed_http_error_detail(exc)
                if detail is None:
                    raise
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": detail.model_dump(mode="json")},
                    headers=exc.headers,
                )

        return governed_route_handler


class PolicyErrorDetail(BaseModel):
    """Canonical bounded B13 error projected identically by REST and MCP."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["error"]
    error: Literal[
        "validation_failed",
        "under_bump",
        "permission_denied",
        "not_found",
        "conflict",
        "service_unavailable",
        "invalid_cursor",
        "semantic_anchor_missing",
        "semantic_anchor_forbidden",
        "semantic_assessment_contract_invalid",
        "unsupported_contract_version",
        "v2_writer_not_ready",
    ]
    code: str
    error_code: str
    message: str
    category: Literal[
        "invalid_argument",
        "permission_denied",
        "not_found",
        "conflict",
        "unprocessable_entity",
        "service_unavailable",
    ]
    status_category: str
    http_status: Literal[400, 401, 403, 404, 409, 422, 503]
    retryable: bool
    next_action: str
    details: dict[str, str]


class PolicyErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: PolicyErrorDetail


def _governed_http_error_detail(
    exc: HTTPException,
) -> PolicyErrorDetail | None:
    """Close dependency failures without rewriting canonical domain errors."""

    try:
        canonical = PolicyErrorDetail.model_validate(exc.detail)
    except ValidationError:
        canonical = None
    if canonical is not None and canonical.http_status == exc.status_code:
        return canonical

    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return PolicyErrorDetail(
            outcome="error",
            error="permission_denied",
            code="authentication_required",
            error_code="authentication_required",
            message="Authentication is required for this guideline policy operation.",
            category="permission_denied",
            status_category="permission_denied",
            http_status=401,
            retryable=False,
            next_action="provide_credentials",
            details={},
        )
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return PolicyErrorDetail(
            outcome="error",
            error="permission_denied",
            code="permission_denied",
            error_code="permission_denied",
            message="Permission denied for this guideline policy operation.",
            category="permission_denied",
            status_category="permission_denied",
            http_status=403,
            retryable=False,
            next_action="request_authority",
            details={},
        )
    if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        legacy_code = (
            exc.detail.get("code") if isinstance(exc.detail, dict) else None
        )
        code = (
            legacy_code
            if isinstance(legacy_code, str)
            and legacy_code
            in {
                "persistence_provider_not_configured",
                "realm_provider_not_configured",
            }
            else "service_unavailable"
        )
        return PolicyErrorDetail(
            outcome="error",
            error="service_unavailable",
            code=code,
            error_code=code,
            message="Guideline policy service is unavailable.",
            category="service_unavailable",
            status_category="service_unavailable",
            http_status=503,
            retryable=True,
            next_action="retry_or_report",
            details={},
        )
    return None


_POLICY_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    code: {
        "model": PolicyErrorEnvelope,
        "description": "Canonical guideline-policy error.",
    }
    for code in (400, 401, 403, 404, 409, 503, "4XX")
}
_SEMANTIC_V2_UNPROCESSABLE_RESPONSE = {
    422: {
        "model": PolicyErrorEnvelope,
        "description": "The authorized semantic anchor is missing.",
    }
}


router = APIRouter(
    route_class=PolicyGovernanceRoute,
    responses=_POLICY_ERROR_RESPONSES,
)

POLICY_PAGE_LIMIT_DEFAULT = 50
POLICY_PAGE_LIMIT_MAX = 200

BoardId = CommunityBoardId
GuidelineId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=GUIDELINE_ID_MAX_LENGTH),
]
RevisionId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=GUIDELINE_REVISION_ID_MAX_LENGTH),
]
ImpactReceiptId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=POLICY_IMPACT_RECEIPT_ID_MAX_LENGTH),
]
ComplianceReceiptId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=POLICY_RECEIPT_ID_MAX_LENGTH),
]
WaiverId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=POLICY_WAIVER_ID_MAX_LENGTH),
]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


_CLOSED_DATACLASS_MODELS: dict[type, type[_ClosedModel]] = {}


def _closed_response_annotation(annotation: object) -> object:
    """Map Core dataclasses to recursively closed transport DTO annotations."""

    if isinstance(annotation, type) and is_dataclass(annotation):
        return _closed_dataclass_response_model(annotation)
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    arguments = get_args(annotation)
    if origin in (Union, types.UnionType):
        return reduce(
            operator.or_,
            (_closed_response_annotation(item) for item in arguments),
        )
    converted = tuple(
        item
        if item is Ellipsis
        else _closed_response_annotation(item)
        for item in arguments
    )
    try:
        return origin[converted[0] if len(converted) == 1 else converted]
    except TypeError:  # pragma: no cover - defensive for unusual typing aliases
        return annotation


def _closed_dataclass_response_model(domain_type: type) -> type[_ClosedModel]:
    """Create a closed Pydantic projection without changing the Core model."""

    cached = _CLOSED_DATACLASS_MODELS.get(domain_type)
    if cached is not None:
        return cached
    definitions: dict[str, tuple[object, object]] = {}
    annotations = get_type_hints(domain_type)
    semantic_profile = {
        "Summary": "summary",
        "Detail": "detail",
        "Full": "full",
    }
    for dataclass_field in fields(domain_type):
        annotation = _closed_response_annotation(
            annotations.get(dataclass_field.name, dataclass_field.type)
        )
        if (
            dataclass_field.name == "projection"
            and domain_type.__module__.endswith(
                "guideline_semantic_projection"
            )
        ):
            for suffix, profile in semantic_profile.items():
                if domain_type.__name__.endswith(suffix):
                    annotation = Literal[profile]
                    break
        if dataclass_field.name == "metric_results":
            if domain_type is CoreSemanticAssessmentDetail:
                annotation = tuple[
                    _closed_dataclass_response_model(
                        CoreSemanticMetricResultDetail
                    ),
                    ...,
                ]
            elif domain_type is CoreSemanticAssessmentFull:
                annotation = tuple[
                    _closed_dataclass_response_model(
                        CoreSemanticMetricResultFull
                    ),
                    ...,
                ]
        if dataclass_field.default is not MISSING:
            default: object = dataclass_field.default
        elif dataclass_field.default_factory is not MISSING:
            default = Field(default_factory=dataclass_field.default_factory)
        else:
            default = ...
        definitions[dataclass_field.name] = (annotation, default)
    model = create_model(
        f"Closed{domain_type.__name__}",
        __base__=_ClosedModel,
        **definitions,
    )
    _CLOSED_DATACLASS_MODELS[domain_type] = model
    return model


class PolicyProjection(str, Enum):
    SUMMARY = "summary"
    DETAIL = "detail"


class SemanticPolicyProjection(str, Enum):
    SUMMARY = "summary"
    DETAIL = "detail"
    FULL = "full"


class PolicyEntityType(str, Enum):
    IDEATION = "ideation"
    REFINEMENT = "refinement"
    SPEC = "spec"
    SPRINT = "sprint"
    CARD = "card"
    TEST_SCENARIO = "test_scenario"


class GuidelineEnforcement(str, Enum):
    ADVISORY = "advisory"
    BLOCKING = "blocking"


class GuidelineScope(str, Enum):
    GLOBAL = "global"
    INLINE = "inline"


class GuidelineContextScope(str, Enum):
    ALL = "all"


class GuidelineBindingState(str, Enum):
    ACTIVE = "active"
    UNLINKED = "unlinked"


class GuidelineBindingProvenance(str, Enum):
    NATIVE = "native"
    DEFAULT_MATERIALIZATION = "default_materialization"


class GuidelineBindingMaterialization(str, Enum):
    LIVE = "live"
    CANDIDATE = "candidate"


class GuidelineHistoryStatus(str, Enum):
    COMPLETE = "complete"
    BASELINE_ONLY = "baseline_only"


class GuidelineImportTransactionStatus(str, Enum):
    PLANNED = "planned"
    DRY_RUN = "dry_run"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class GuidelineRevisionMutationStatus(str, Enum):
    APPLIED = "applied"
    NOOP = "noop"


class GuidelineLifecycleStatus(str, Enum):
    RETIRED = "retired"
    SUPERSEDED = "superseded"


class GuidelineImpactItemKind(str, Enum):
    BINDING = "binding"
    TARGET = "target"
    ARTIFACT = "artifact"
    WAIVER = "waiver"


class PolicyCurrentness(str, Enum):
    CURRENT = "current"
    STALE = "stale"


class SemanticAssessmentOutcome(str, Enum):
    PASSED = "passed"
    METRIC_THRESHOLD_FAILED = "metric_threshold_failed"


class SemanticMetricOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class SemanticThresholdSource(str, Enum):
    DEFAULT = "default"
    OVERRIDE = "override"


class SemanticWaiverStatus(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"


class SemanticWaiverReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class SemanticWaiverRevalidationReasonCode(str, Enum):
    CURRENT = "current"
    SCHEDULED_EXPIRY = "scheduled_expiry"
    ANCHOR_MISSING = "anchor_missing"
    SUBJECT_SCOPE_CHANGED = "subject_scope_changed"
    GUIDELINE_REVISION_CHANGED = "guideline_revision_changed"
    BINDING_CONFIGURATION_CHANGED = "binding_configuration_changed"
    METRIC_RESULT_CHANGED = "metric_result_changed"
    REVOKED = "revoked"


PolicyScalar = str | int | float | bool | None
_METRIC_CODE_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]*$"


class GuidelineMetricDirection(str, Enum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"


class GuidelineMetricRequest(_ClosedModel):
    metric_id: str = Field(
        min_length=1,
        max_length=POLICY_METRIC_ID_MAX_LENGTH,
    )
    code: str = Field(
        min_length=1,
        max_length=POLICY_METRIC_CODE_MAX_LENGTH,
        pattern=_METRIC_CODE_PATTERN,
    )
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1)
    evaluation_rubric: str = Field(min_length=1)
    target_entity_types: list[PolicyEntityType] = Field(min_length=1)
    direction: GuidelineMetricDirection
    default_threshold: int = Field(ge=0, le=100)


class GuidelineRevisionPatchRequest(_ClosedModel):
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=GUIDELINE_TITLE_MAX_LENGTH,
    )
    content: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = None
    metrics: list[GuidelineMetricRequest] | None = None

    @model_validator(mode="after")
    def require_change(self) -> GuidelineRevisionPatchRequest:
        if all(
            value is None
            for value in (self.title, self.content, self.tags, self.metrics)
        ):
            raise ValueError("guideline_revision_patch_empty")
        return self


class CreateGuidelineRevisionRequest(_ClosedModel):
    next_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=GUIDELINE_REVISION_ID_MAX_LENGTH,
    )
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    patch: GuidelineRevisionPatchRequest
    declared_semantic_version: str | None = Field(
        default=None,
        min_length=5,
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    occurred_at: datetime | None = None


class RetireGuidelineRequest(_ClosedModel):
    retirement_id: str = Field(
        min_length=1,
        max_length=GUIDELINE_RETIREMENT_ID_MAX_LENGTH,
    )
    status: GuidelineLifecycleStatus = GuidelineLifecycleStatus.RETIRED
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    superseded_by_guideline_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=GUIDELINE_ID_MAX_LENGTH,
    )
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def require_successor_for_supersedence(self) -> RetireGuidelineRequest:
        if (
            self.status is GuidelineLifecycleStatus.SUPERSEDED
            and self.superseded_by_guideline_id is None
        ):
            raise ValueError("guideline_retirement_successor_required")
        if (
            self.status is GuidelineLifecycleStatus.RETIRED
            and self.superseded_by_guideline_id is not None
        ):
            raise ValueError("guideline_retirement_successor_unexpected")
        return self


class PreviewGuidelineImpactRequest(_ClosedModel):
    proposed_priority: int = Field(ge=0, le=POLICY_SQL_INTEGER_MAX)
    proposed_enforcement: GuidelineEnforcement
    proposed_minimum_confidence: int = Field(ge=0, le=100)
    proposed_metric_threshold_overrides: dict[
        Annotated[
            str,
            StringConstraints(
                min_length=1,
                max_length=POLICY_METRIC_CODE_MAX_LENGTH,
                pattern=_METRIC_CODE_PATTERN,
            ),
        ],
        Annotated[int, Field(ge=0, le=100)],
    ] = Field(default_factory=dict)
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    to_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=GUIDELINE_REVISION_ID_MAX_LENGTH,
    )
    requested_at: datetime | None = None


class AdoptGuidelineRevisionRequest(_ClosedModel):
    impact_receipt_id: str = Field(
        min_length=1,
        max_length=POLICY_IMPACT_RECEIPT_ID_MAX_LENGTH,
    )
    impact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    occurred_at: datetime | None = None


class SemanticEvidenceRefRequest(_ClosedModel):
    source_type: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_version: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SemanticPinpointRequest(_ClosedModel):
    anchor_type: Literal[
        "whole_artifact",
        "field",
        "structured_child",
        "qa",
    ]
    anchor_ref: str | None = None
    excerpt_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class SemanticMetricAssessmentRequest(_ClosedModel):
    metric_id: str = Field(
        min_length=1,
        max_length=POLICY_METRIC_ID_MAX_LENGTH,
    )
    score: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1)
    evidence_refs: list[SemanticEvidenceRefRequest] = Field(min_length=1)
    pinpoints: list[SemanticPinpointRequest] = Field(min_length=1)


class SemanticAnchorV2Request(_ClosedModel):
    anchor_type: Literal[
        "whole_artifact",
        "field",
        "structured_child",
        "qa",
    ]
    anchor_ref: str | None = Field(default=None, min_length=1, max_length=500)
    excerpt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_anchor_shape(self) -> "SemanticAnchorV2Request":
        if self.anchor_type == "whole_artifact" and self.anchor_ref is not None:
            raise ValueError("finding_whole_artifact_ref_forbidden")
        if self.anchor_type != "whole_artifact" and self.anchor_ref is None:
            raise ValueError("finding_anchor_ref_required")
        return self


class SemanticPinpointV2Request(_ClosedModel):
    contract_version: Literal["v2"]
    pinpoint_key: str = Field(min_length=1, max_length=SEMANTIC_PINPOINT_KEY_MAX_LENGTH)
    kind: Literal["evidence", "issue"]
    title: str = Field(min_length=1, max_length=SEMANTIC_PINPOINT_TITLE_MAX_LENGTH)
    detail: str = Field(min_length=1, max_length=SEMANTIC_PINPOINT_DETAIL_MAX_LENGTH)
    severity: Literal["low", "medium", "high", "critical"] | None = None
    remediation: str | None = Field(
        default=None,
        min_length=1,
        max_length=SEMANTIC_PINPOINT_REMEDIATION_MAX_LENGTH,
    )
    anchor: SemanticAnchorV2Request

    @model_validator(mode="after")
    def require_issue_severity(self) -> "SemanticPinpointV2Request":
        if self.kind == "issue" and self.severity is None:
            raise ValueError("semantic_pinpoint_v2_issue_severity_required")
        return self


class SemanticMetricAssessmentV2Request(_ClosedModel):
    contract_version: Literal["v2"]
    metric_id: str = Field(min_length=1, max_length=POLICY_METRIC_ID_MAX_LENGTH)
    score: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=20_000)
    evidence_refs: list[SemanticEvidenceRefRequest] = Field(
        min_length=1,
        max_length=200,
    )
    pinpoints: list[SemanticPinpointV2Request] = Field(
        min_length=1,
        max_length=200,
    )


class RecordSemanticGuidelineAssessmentV2Request(_ClosedModel):
    contract_version: Literal["v2"]
    subject_type: PolicyEntityType
    subject_id: str = Field(min_length=1, max_length=POLICY_SUBJECT_ID_MAX_LENGTH)
    expected_subject_version: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    binding_id: str = Field(min_length=1, max_length=GUIDELINE_BINDING_ID_MAX_LENGTH)
    expected_binding_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    guideline_revision_id: str = Field(
        min_length=1,
        max_length=GUIDELINE_REVISION_ID_MAX_LENGTH,
    )
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    confidence: int = Field(ge=0, le=100)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    metric_results: list[SemanticMetricAssessmentV2Request] = Field(
        min_length=1,
        max_length=200,
    )


class SemanticAssessmentAssessorRequest(_ClosedModel):
    agent_id: str = Field(
        min_length=1,
        max_length=POLICY_ACTOR_ID_MAX_LENGTH,
    )
    model_id: str | None = Field(min_length=1)


class RecordSemanticGuidelineAssessmentRequest(_ClosedModel):
    subject_type: PolicyEntityType
    subject_id: str = Field(
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    )
    expected_subject_version: int = Field(
        ge=1,
        le=POLICY_SQL_INTEGER_MAX,
    )
    binding_id: str = Field(
        min_length=1,
        max_length=GUIDELINE_BINDING_ID_MAX_LENGTH,
    )
    expected_binding_revision: int = Field(
        ge=1,
        le=POLICY_SQL_INTEGER_MAX,
    )
    guideline_revision_id: str = Field(
        min_length=1,
        max_length=GUIDELINE_REVISION_ID_MAX_LENGTH,
    )
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    confidence: int = Field(ge=0, le=100)
    assessor: SemanticAssessmentAssessorRequest
    metric_results: list[SemanticMetricAssessmentRequest] = Field(
        min_length=1
    )


class RequestSemanticWaiverRequest(_ClosedModel):
    metric_result_id: str = Field(
        min_length=1,
        max_length=POLICY_RECEIPT_ID_MAX_LENGTH,
    )
    finding_id: str = Field(
        min_length=1,
        max_length=POLICY_FINDING_ID_MAX_LENGTH,
    )
    receipt_id: str = Field(
        min_length=1,
        max_length=POLICY_RECEIPT_ID_MAX_LENGTH,
    )
    justification: str = Field(min_length=1)
    evidence_refs: list[SemanticEvidenceRefRequest] = Field(min_length=1)
    expires_at: datetime | None
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )


class ReviewSemanticWaiverRequest(_ClosedModel):
    decision: SemanticWaiverReviewDecision
    reason: str = Field(min_length=1)
    evidence_refs: list[SemanticEvidenceRefRequest] = Field(min_length=1)
    expected_waiver_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )


class RevokeSemanticWaiverRequest(_ClosedModel):
    reason: str = Field(min_length=1)
    evidence_refs: list[SemanticEvidenceRefRequest] = Field(min_length=1)
    expected_waiver_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )


class RevalidateSemanticWaiverRequest(_ClosedModel):
    expected_waiver_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    evaluated_at: datetime
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )


class CreateSemanticPolicySkipRequest(_ClosedModel):
    subject_type: PolicyEntityType
    subject_id: str = Field(
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    )
    expected_subject_version: int = Field(
        ge=1,
        le=POLICY_SQL_INTEGER_MAX,
    )
    binding_id: str = Field(
        min_length=1,
        max_length=GUIDELINE_BINDING_ID_MAX_LENGTH,
    )
    reason: str = Field(min_length=1)


class RevokeSemanticPolicySkipRequest(_ClosedModel):
    expected_skip_revision: int = Field(
        ge=1,
        le=POLICY_SQL_INTEGER_MAX,
    )
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )


class GuidelineExportMetricV3(_ClosedModel):
    metric_id: str = Field(max_length=POLICY_METRIC_ID_MAX_LENGTH)
    code: str = Field(
        min_length=1,
        max_length=POLICY_METRIC_CODE_MAX_LENGTH,
        pattern=_METRIC_CODE_PATTERN,
    )
    title: str
    description: str
    evaluation_rubric: str
    target_entity_types: list[PolicyEntityType]
    direction: GuidelineMetricDirection
    default_threshold: int = Field(ge=0, le=100)


class GuidelineExportRevisionV3(_ClosedModel):
    revision_id: RevisionId
    guideline_id: GuidelineId
    revision_number: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    semantic_version: str = Field(
        min_length=1,
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    title: str = Field(min_length=1, max_length=GUIDELINE_TITLE_MAX_LENGTH)
    content: str
    revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    metrics: list[GuidelineExportMetricV3]
    created_by: str = Field(min_length=1, max_length=POLICY_ACTOR_ID_MAX_LENGTH)
    created_at: datetime
    parent_revision_id: RevisionId | None
    tags: list[str]
    published_head_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    published_head_updated_at: datetime
    legacy_version: str | None = Field(
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH
    )
    legacy_version_unresolvable: bool
    legacy_tags: list[str] | None


class GuidelineExportIdentityV3(_ClosedModel):
    guideline_id: GuidelineId
    owner_id: str = Field(min_length=1, max_length=POLICY_ACTOR_ID_MAX_LENGTH)
    scope: GuidelineScope
    board_id: BoardId | None
    context_scope: GuidelineContextScope
    created_at: datetime


class GuidelineExportHeadV3(_ClosedModel):
    guideline_id: GuidelineId
    revision_id: RevisionId
    revision_number: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    semantic_version: str = Field(
        min_length=1,
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    head_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    updated_at: datetime


class GuidelineExportRetirementV3(_ClosedModel):
    retirement_id: str = Field(
        min_length=1,
        max_length=GUIDELINE_RETIREMENT_ID_MAX_LENGTH,
    )
    guideline_id: GuidelineId
    status: GuidelineLifecycleStatus
    retired_revision_id: RevisionId
    retired_revision_number: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    retired_semantic_version: str = Field(
        min_length=1,
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    retired_revision_digest: str
    retired_head_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    reason: str
    retired_by: str = Field(min_length=1, max_length=POLICY_ACTOR_ID_MAX_LENGTH)
    retired_at: datetime
    superseded_by_guideline_id: GuidelineId | None


class GuidelineExportLogicalBindingV3(_ClosedModel):
    binding_id: str = Field(
        min_length=1,
        max_length=GUIDELINE_BINDING_ID_MAX_LENGTH,
    )
    board_id: BoardId
    guideline_id: GuidelineId
    revision_id: RevisionId
    semantic_version: str = Field(
        min_length=1,
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    revision_digest: str
    priority: int = Field(ge=0, le=POLICY_SQL_INTEGER_MAX)
    binding_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    adopted_by: str = Field(min_length=1, max_length=POLICY_ACTOR_ID_MAX_LENGTH)
    adopted_at: datetime
    enforcement: GuidelineEnforcement
    minimum_confidence: int = Field(ge=0, le=100)
    metric_threshold_overrides: dict[str, int]
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: GuidelineBindingState
    source_kind: GuidelineBindingProvenance


class GuidelineExportBindingV3(_ClosedModel):
    binding: GuidelineExportLogicalBindingV3
    physical_source_kind: str = Field(min_length=1, max_length=40)
    binding_origin: str = Field(min_length=1, max_length=32)
    materialization: GuidelineBindingMaterialization
    legacy_source_id: str | None = Field(max_length=GUIDELINE_ID_MAX_LENGTH)
    legacy_guideline_version: str | None = Field(
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    legacy_template_id: str | None = Field(
        max_length=GUIDELINE_ID_MAX_LENGTH,
    )
    legacy_template_version: str | None = Field(
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    legacy_version_unresolvable: bool
    evidence_refs: list[tuple[str, str]]
    binding_digest: str


class GuidelineExportAggregateV3(_ClosedModel):
    identity: GuidelineExportIdentityV3
    revisions: list[GuidelineExportRevisionV3]
    head: GuidelineExportHeadV3
    retirement: GuidelineExportRetirementV3 | None
    bindings: list[GuidelineExportBindingV3]
    history_status: GuidelineHistoryStatus
    migration_notes: list[str]


class GuidelineExportV3Request(_ClosedModel):
    contract_version: Literal["guideline-export/v3"]
    schema_version: Literal["3"]
    kind: Literal["guidelines"]
    exported_at: datetime
    source_board_id: BoardId | None
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    guidelines: list[GuidelineExportAggregateV3]


class GuidelineImportResultResponse(_ClosedModel):
    transaction_status: GuidelineImportTransactionStatus
    created_count: int
    skip_identical_count: int
    conflict_count: int
    overwritten_row_count: int
    dry_run: bool
    error_code: str | None = None


ClosedGuidelineRevisionListItem = _closed_dataclass_response_model(
    CoreGuidelineRevisionListItem
)
ClosedGuidelineImpactItem = _closed_dataclass_response_model(
    CoreGuidelineImpactItem
)
ClosedGuideline = _closed_dataclass_response_model(CoreGuideline)
ClosedGuidelineRevision = _closed_dataclass_response_model(CoreGuidelineRevision)
ClosedGuidelineHead = _closed_dataclass_response_model(CoreGuidelineHead)
ClosedGuidelineRetirement = _closed_dataclass_response_model(
    CoreGuidelineRetirement
)
ClosedGuidelineImpactReceipt = _closed_dataclass_response_model(
    CoreGuidelineImpactReceipt
)
ClosedBoardGuidelineBinding = _closed_dataclass_response_model(
    CoreBoardGuidelineBinding
)
ClosedSemanticAssessmentSummary = _closed_dataclass_response_model(
    CoreSemanticAssessmentSummary
)
ClosedSemanticAssessmentDetail = _closed_dataclass_response_model(
    CoreSemanticAssessmentDetail
)
ClosedSemanticAssessmentFull = _closed_dataclass_response_model(
    CoreSemanticAssessmentFull
)
ClosedSemanticFindingSummary = _closed_dataclass_response_model(
    CoreSemanticFindingSummary
)
ClosedSemanticFindingDetail = _closed_dataclass_response_model(
    CoreSemanticFindingDetail
)
ClosedSemanticFindingFull = _closed_dataclass_response_model(
    CoreSemanticFindingFull
)
ClosedSemanticWaiverSummary = _closed_dataclass_response_model(
    CoreSemanticWaiverSummary
)
ClosedSemanticWaiverDetail = _closed_dataclass_response_model(
    CoreSemanticWaiverDetail
)
ClosedSemanticWaiverFull = _closed_dataclass_response_model(
    CoreSemanticWaiverFull
)
ClosedSemanticSkipSummary = _closed_dataclass_response_model(
    CoreSemanticSkipSummary
)
ClosedSemanticSkipDetail = _closed_dataclass_response_model(
    CoreSemanticSkipDetail
)
ClosedSemanticSkipFull = _closed_dataclass_response_model(
    CoreSemanticSkipFull
)
ClosedSemanticMetricWaiverEvent = _closed_dataclass_response_model(
    CoreSemanticMetricWaiverEvent
)

SemanticAssessmentProjectionResponse = (
    ClosedSemanticAssessmentSummary
    | ClosedSemanticAssessmentDetail
    | ClosedSemanticAssessmentFull
)
SemanticFindingProjectionResponse = (
    ClosedSemanticFindingSummary
    | ClosedSemanticFindingDetail
    | ClosedSemanticFindingFull
)
SemanticWaiverProjectionResponse = (
    ClosedSemanticWaiverSummary
    | ClosedSemanticWaiverDetail
    | ClosedSemanticWaiverFull
)
SemanticSkipProjectionResponse = (
    ClosedSemanticSkipSummary
    | ClosedSemanticSkipDetail
    | ClosedSemanticSkipFull
)


class _PolicyPageResponse(_ClosedModel):
    limit: int
    has_more: bool
    next_cursor: str | None


class GuidelineRevisionPageResponse(_PolicyPageResponse):
    items: list[ClosedGuidelineRevisionListItem]


class GuidelineImpactItemPageResponse(_PolicyPageResponse):
    items: list[ClosedGuidelineImpactItem]


class _SemanticPageResponse(_ClosedModel):
    projection: SemanticPolicyProjection
    next_cursor: str | None
    has_more: bool

    @model_validator(mode="after")
    def require_exact_item_projection(self) -> _SemanticPageResponse:
        expected = self.projection.value
        if any(
            getattr(item, "projection", None) != expected
            for item in getattr(self, "items", ())
        ):
            raise ValueError("semantic_page_projection_mismatch")
        return self


class SemanticAssessmentPageResponse(_SemanticPageResponse):
    items: list[SemanticAssessmentProjectionResponse]


class SemanticFindingPageResponse(_SemanticPageResponse):
    items: list[SemanticFindingProjectionResponse]


class SemanticWaiverPageResponse(_SemanticPageResponse):
    items: list[SemanticWaiverProjectionResponse]


class SemanticSkipPageResponse(_SemanticPageResponse):
    items: list[SemanticSkipProjectionResponse]


class SemanticAssessmentResponse(_ClosedModel):
    contract_version: Literal["v1", "v2"] = "v1"
    assessment: Union[
        SemanticAssessmentProjectionResponse,
        "SemanticAssessmentCurrentV2Response",
    ]


class RecordedSemanticMetricResultResponse(_ClosedModel):
    metric_result_id: str
    metric_id: str
    metric_code: str
    score: int = Field(ge=0, le=100)
    direction: GuidelineMetricDirection
    default_threshold: int = Field(ge=0, le=100)
    effective_threshold: int = Field(ge=0, le=100)
    threshold_source: SemanticThresholdSource
    outcome: SemanticMetricOutcome


class RecordedSemanticAssessmentResponse(_ClosedModel):
    receipt_id: str
    state: SemanticAssessmentOutcome
    confidence_admissible: bool
    metric_results: list[RecordedSemanticMetricResultResponse]
    replayed: bool


class SemanticAnchorSnapshotV2Response(_ClosedModel):
    label: str
    excerpt: str | None
    source_version: str
    availability_at_seal: Literal["available", "removed", "inaccessible"]


class SemanticPinpointV2Response(_ClosedModel):
    contract_version: Literal["v2"]
    pinpoint_key: str
    kind: Literal["evidence", "issue"]
    title: str
    detail: str
    severity: Literal["low", "medium", "high", "critical"] | None
    remediation: str | None
    anchor: SemanticAnchorV2Request
    anchor_snapshot: SemanticAnchorSnapshotV2Response
    blocking: bool


class SemanticMetricResultV2Response(_ClosedModel):
    metric_result_id: str
    metric_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_id: str
    metric_code: str
    score: int = Field(ge=0, le=100)
    direction: Literal["minimum", "maximum"]
    default_threshold: int = Field(ge=0, le=100)
    effective_threshold: int = Field(ge=0, le=100)
    threshold_source: Literal["default", "override"]
    outcome: Literal["pass", "fail"]
    blocking: bool
    pinpoints: list[SemanticPinpointV2Response]


class SemanticAssessmentCurrentV2Response(_ClosedModel):
    receipt_id: str
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    currentness: Literal["current"]
    board_id: str
    subject_type: PolicyEntityType
    subject_id: str
    subject_version: int = Field(ge=1)
    binding_id: str
    guideline_id: str
    guideline_revision_id: str
    confidence: int = Field(ge=0, le=100)
    recorded_at: datetime
    metrics: list[SemanticMetricResultV2Response]


class RecordedSemanticAssessmentV2Response(_ClosedModel):
    contract_version: Literal["v2"]
    receipt_id: str
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    currentness: Literal["current"]
    metrics: list[SemanticMetricResultV2Response]


class SemanticWaiverResponse(_ClosedModel):
    waiver: SemanticWaiverProjectionResponse


class SemanticWaiverEventsResponse(_ClosedModel):
    events: list[ClosedSemanticMetricWaiverEvent]


class RequestedSemanticWaiverResponse(_ClosedModel):
    waiver_id: str
    status: Literal["requested"]
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewedSemanticWaiverResponse(_ClosedModel):
    waiver_id: str
    waiver_revision: int = Field(ge=1)
    status: Literal["approved", "rejected"]
    reviewer_id: str
    replayed: bool


class RevokedSemanticWaiverResponse(_ClosedModel):
    waiver_id: str
    waiver_revision: int = Field(ge=1)
    status: Literal["revoked"]
    replayed: bool


class RevalidatedSemanticWaiverResponse(_ClosedModel):
    waiver_id: str
    waiver_revision: int = Field(ge=1)
    status: Literal["approved", "expired", "anchor_stale", "revoked"]
    current: bool
    reason_code: SemanticWaiverRevalidationReasonCode
    replayed: bool


class SemanticSkipResponse(_ClosedModel):
    skip: SemanticSkipProjectionResponse


class CreatedSemanticSkipResponse(_ClosedModel):
    skip_id: str
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str


class RevokedSemanticSkipResponse(_ClosedModel):
    skip_id: str
    skip_revision: int = Field(ge=1)
    status: Literal["revoked"]
    revoked_by: str
    replayed: bool


class GuidelineRevisionAuthorityResponse(_ClosedModel):
    guideline: ClosedGuideline
    revision: ClosedGuidelineRevision
    head: ClosedGuidelineHead
    retirement: ClosedGuidelineRetirement | None


class CreateGuidelineRevisionResponse(_ClosedModel):
    status: GuidelineRevisionMutationStatus
    revision: ClosedGuidelineRevision | None
    head: ClosedGuidelineHead | None
    minimum_bump: Literal["patch", "minor", "major"] | None
    rejection_code: str | None = None

    @field_validator("minimum_bump", mode="before")
    @classmethod
    def project_minimum_bump(
        cls,
        value: object,
    ) -> object:
        if isinstance(value, GuidelineVersionBump):
            return value.name.lower()
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                return GuidelineVersionBump(value).name.lower()
            except ValueError:
                return value
        return value


class RetirementResponse(_ClosedModel):
    retirement: ClosedGuidelineRetirement


class GuidelineImpactReceiptResponse(_ClosedModel):
    receipt: ClosedGuidelineImpactReceipt


class AdoptionResponse(_ClosedModel):
    binding: ClosedBoardGuidelineBinding
    receipt: ClosedGuidelineImpactReceipt


class PolicyGovernanceFacade(Protocol):
    async def execute(
        self,
        operation: str,
        values: dict[str, Any],
        *,
        actor: Any,
        uow: PulseUnitOfWork,
    ) -> object: ...


_OPERATION_TYPES: dict[str, tuple[str, str]] = {
    "list_revisions": (
        "ListGuidelineRevisionsCommand",
        "ListGuidelineRevisionsUseCase",
    ),
    "get_revision": (
        "GetGuidelineRevisionCommand",
        "GetGuidelineRevisionUseCase",
    ),
    "create_revision": (
        "CreateGuidelineRevisionCommand",
        "CreateGuidelineRevisionUseCase",
    ),
    "retire_guideline": ("RetireGuidelineCommand", "RetireGuidelineUseCase"),
    "preview_impact": (
        "PreviewGuidelineImpactCommand",
        "PreviewGuidelineImpactUseCase",
    ),
    "get_impact": ("GetGuidelineImpactCommand", "GetGuidelineImpactUseCase"),
    "list_impact_items": (
        "ListGuidelineImpactItemsCommand",
        "ListGuidelineImpactItemsUseCase",
    ),
    "adopt_revision": (
        "AdoptGuidelineRevisionCommand",
        "AdoptGuidelineRevisionUseCase",
    ),
}

_SEMANTIC_OPERATION_TYPES: dict[str, tuple[str, str, str]] = {
    "record_semantic_assessment_v2": (
        "okto_pulse.core.application.use_cases.semantic_guideline_v2",
        "SealSemanticGuidelineAssessmentV2Command",
        "SealSemanticGuidelineAssessmentV2UseCase",
    ),
    "record_semantic_assessment": (
        "okto_pulse.core.application.use_cases.policy_governance",
        "RecordSemanticGuidelineAssessmentCommand",
        "RecordSemanticGuidelineAssessmentUseCase",
    ),
    "list_semantic_assessments": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "ListSemanticGuidelineAssessmentsCommand",
        "ListSemanticGuidelineAssessmentsUseCase",
    ),
    "get_semantic_assessment": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "GetSemanticGuidelineAssessmentCommand",
        "GetSemanticGuidelineAssessmentUseCase",
    ),
    "get_current_semantic_assessment": (
        "okto_pulse.core.application.use_cases.semantic_guideline_v2",
        "GetCurrentSemanticGuidelineAssessmentCommand",
        "GetCurrentSemanticGuidelineAssessmentAnyUseCase",
    ),
    "list_semantic_findings": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "ListSemanticGuidelineFindingsCommand",
        "ListSemanticGuidelineFindingsUseCase",
    ),
    "list_semantic_waivers": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "ListSemanticMetricWaiversCommand",
        "ListSemanticMetricWaiversUseCase",
    ),
    "get_semantic_waiver": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "GetSemanticMetricWaiverCommand",
        "GetSemanticMetricWaiverUseCase",
    ),
    "list_semantic_waiver_events": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "ListSemanticMetricWaiverEventsCommand",
        "ListSemanticMetricWaiverEventsUseCase",
    ),
    "request_semantic_waiver": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "RequestSemanticMetricWaiverCommand",
        "RequestSemanticMetricWaiverUseCase",
    ),
    "review_semantic_waiver": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "ReviewSemanticMetricWaiverCommand",
        "ReviewSemanticMetricWaiverUseCase",
    ),
    "revoke_semantic_waiver": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "RevokeSemanticMetricWaiverCommand",
        "RevokeSemanticMetricWaiverUseCase",
    ),
    "revalidate_semantic_waiver": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "RevalidateSemanticMetricWaiverCommand",
        "RevalidateSemanticMetricWaiverUseCase",
    ),
    "list_semantic_skips": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "ListSemanticPolicySkipsCommand",
        "ListSemanticPolicySkipsUseCase",
    ),
    "get_semantic_skip": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "GetSemanticPolicySkipCommand",
        "GetSemanticPolicySkipUseCase",
    ),
    "create_semantic_skip": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "CreateSemanticPolicySkipCommand",
        "CreateSemanticPolicySkipUseCase",
    ),
    "revoke_semantic_skip": (
        "okto_pulse.core.application.use_cases.semantic_guideline_governance",
        "RevokeSemanticPolicySkipCommand",
        "RevokeSemanticPolicySkipUseCase",
    ),
}


def _domain_metric(payload: dict[str, Any]) -> object:
    policy = import_module("okto_pulse.core.domain.guideline_policy")
    return policy.GuidelineMetric(
        metric_id=payload["metric_id"],
        code=payload["code"],
        title=payload["title"],
        description=payload["description"],
        evaluation_rubric=payload["evaluation_rubric"],
        target_entity_types=tuple(
            policy.PolicyEntityType(item) for item in payload["target_entity_types"]
        ),
        direction=policy.GuidelineMetricDirection(payload["direction"]),
        default_threshold=payload["default_threshold"],
    )


def _semantic_evidence(payload: dict[str, Any]) -> object:
    quality = import_module("okto_pulse.core.domain.quality_assessment")
    return quality.EvidenceRef(**payload)


def _semantic_projection(value: object) -> object:
    projection = import_module(
        "okto_pulse.core.domain.guideline_semantic_projection"
    )
    return projection.SemanticGuidelineProjection(value)


def _adapt_semantic_values(
    operation: str,
    values: dict[str, Any],
    *,
    codec: PolicyCursorCodec | None,
    actor: Any,
) -> dict[str, Any]:
    policy = import_module("okto_pulse.core.domain.guideline_policy")
    ports = import_module("okto_pulse.core.ports.guideline_policy")
    exceptions = import_module(
        "okto_pulse.core.domain.guideline_semantic_exceptions"
    )
    adapted = dict(values)
    projection_value = adapted.get("projection")
    if projection_value is not None:
        adapted["projection"] = _semantic_projection(projection_value)
    subject_type = adapted.pop("subject_type", None)
    if subject_type is not None:
        adapted["entity_type"] = subject_type
    entity_type = adapted.get("entity_type")
    if entity_type is not None:
        adapted["entity_type"] = policy.PolicyEntityType(entity_type)
    currentness = adapted.get("currentness")
    if currentness is not None:
        adapted["currentness"] = policy.PolicyCurrentness(currentness)

    query_type_and_kind = {
        "list_semantic_assessments": (
            ports.SemanticAssessmentListQuery,
            "semantic_assessment",
        ),
        "list_semantic_findings": (
            ports.SemanticFindingListQuery,
            "semantic_finding",
        ),
        "list_semantic_waivers": (
            ports.SemanticWaiverListQuery,
            "semantic_waiver",
        ),
        "list_semantic_skips": (
            ports.SemanticSkipListQuery,
            "semantic_skip",
        ),
    }
    query_contract = query_type_and_kind.get(operation)
    if query_contract is not None:
        if codec is None:  # pragma: no cover - facade invariant
            raise RuntimeError("guideline_policy_cursor_codec_missing")
        query_type, cursor_kind = query_contract
        token = adapted.pop("cursor", None)
        adapted["cursor"] = (
            None
            if token is None
            else codec.decode(token, expected_kind=cursor_kind)
        )
        status_value = adapted.get("status")
        if status_value is not None:
            status_type = (
                exceptions.SemanticMetricWaiverStatus
                if operation == "list_semantic_waivers"
                else exceptions.SemanticPolicySkipStatus
            )
            adapted["status"] = status_type(status_value)
        outcome_value = adapted.get("outcome")
        if outcome_value is not None:
            assessment = import_module(
                "okto_pulse.core.domain.guideline_semantic_assessment"
            )
            outcome_type = (
                assessment.SemanticAssessmentState
                if operation == "list_semantic_assessments"
                else assessment.SemanticMetricOutcome
            )
            adapted["outcome"] = outcome_type(outcome_value)
        return {"query": query_type(**adapted)}

    if operation == "record_semantic_assessment":
        assessment = import_module(
            "okto_pulse.core.domain.guideline_semantic_assessment"
        )
        quality = import_module(
            "okto_pulse.core.domain.quality_assessment"
        )
        metric_results = tuple(
            assessment.SemanticMetricAssessment(
                metric_id=item["metric_id"],
                score=item["score"],
                rationale=item["rationale"],
                evidence_refs=tuple(
                    _semantic_evidence(evidence)
                    for evidence in item["evidence_refs"]
                ),
                pinpoints=tuple(
                    quality.UnboundFindingAnchor(
                        anchor_type=quality.FindingAnchorType(
                            pinpoint["anchor_type"]
                        ),
                        anchor_ref=pinpoint.get("anchor_ref"),
                        excerpt_hash=pinpoint.get("excerpt_hash"),
                    )
                    for pinpoint in item["pinpoints"]
                ),
            )
            for item in adapted.pop("metric_results")
        )
        subject = policy.PolicySubjectRef(
            board_id=adapted["board_id"],
            entity_type=adapted.pop("entity_type"),
            subject_id=adapted.pop("subject_id"),
            subject_version=adapted.pop("expected_subject_version"),
        )
        assessor_payload = adapted.pop("assessor")
        submission = assessment.SemanticGuidelineAssessmentSubmission(
            subject=subject,
            binding_id=adapted.pop("binding_id"),
            expected_binding_revision=adapted.pop(
                "expected_binding_revision"
            ),
            guideline_revision_id=adapted.pop(
                "guideline_revision_id"
            ),
            idempotency_key=adapted.pop("idempotency_key"),
            confidence=adapted.pop("confidence"),
            assessor=assessment.SemanticAssessmentAssessor(
                agent_id=assessor_payload["agent_id"],
                model_id=assessor_payload.get("model_id"),
            ),
            metric_results=metric_results,
        )
        adapted["submission"] = submission
        return adapted

    if operation == "record_semantic_assessment_v2":
        assessment = import_module(
            "okto_pulse.core.domain.guideline_semantic_assessment"
        )
        semantic_v2 = import_module(
            "okto_pulse.core.domain.guideline_semantic_v2"
        )
        quality = import_module("okto_pulse.core.domain.quality_assessment")
        adapted.pop("contract_version")
        metric_results = tuple(
            semantic_v2.SemanticMetricAssessmentDraftV2(
                metric_id=item["metric_id"],
                score=item["score"],
                rationale=item["rationale"],
                evidence_refs=tuple(
                    _semantic_evidence(evidence)
                    for evidence in item["evidence_refs"]
                ),
                pinpoints=tuple(
                    semantic_v2.SemanticPinpointDraftV2(
                        pinpoint_key=pinpoint["pinpoint_key"],
                        kind=semantic_v2.SemanticPinpointKind(pinpoint["kind"]),
                        title=pinpoint["title"],
                        detail=pinpoint["detail"],
                        severity=(
                            quality.FindingSeverity(pinpoint["severity"])
                            if pinpoint.get("severity") is not None
                            else None
                        ),
                        remediation=pinpoint.get("remediation"),
                        anchor=quality.UnboundFindingAnchor(
                            anchor_type=quality.FindingAnchorType(
                                pinpoint["anchor"]["anchor_type"]
                            ),
                            anchor_ref=pinpoint["anchor"].get("anchor_ref"),
                            excerpt_hash=pinpoint["anchor"].get("excerpt_hash"),
                        ),
                    )
                    for pinpoint in item["pinpoints"]
                ),
            )
            for item in adapted.pop("metric_results")
        )
        subject = policy.PolicySubjectRef(
            board_id=adapted.pop("board_id"),
            entity_type=adapted.pop("entity_type"),
            subject_id=adapted.pop("subject_id"),
            subject_version=adapted.pop("expected_subject_version"),
        )
        model_id = adapted.pop("model_id", None)
        draft = semantic_v2.SemanticAssessmentDraftV2(
            subject=subject,
            binding_id=adapted.pop("binding_id"),
            expected_binding_revision=adapted.pop("expected_binding_revision"),
            guideline_revision_id=adapted.pop("guideline_revision_id"),
            idempotency_key=adapted.pop("idempotency_key"),
            confidence=adapted.pop("confidence"),
            assessor=assessment.SemanticAssessmentAssessor(
                agent_id=str(actor.actor_id),
                model_id=model_id,
            ),
            metric_results=metric_results,
        )
        return {
            "board_id": subject.board_id,
            "actor_id": str(actor.actor_id),
            "draft": draft,
        }

    if operation in {
        "request_semantic_waiver",
        "review_semantic_waiver",
        "revoke_semantic_waiver",
        "revalidate_semantic_waiver",
    }:
        adapted["evidence_refs"] = tuple(
            _semantic_evidence(item)
            for item in adapted.get("evidence_refs", ())
        )
    if operation == "review_semantic_waiver":
        adapted["decision"] = exceptions.SemanticMetricWaiverEventType(
            adapted["decision"]
        )
    return adapted


def _adapt_values(
    operation: str,
    values: dict[str, Any],
    *,
    codec: PolicyCursorCodec | None,
    actor: Any,
) -> dict[str, Any]:
    """Convert REST enums/nested models to Core-owned immutable values."""

    if operation in _SEMANTIC_OPERATION_TYPES:
        return _adapt_semantic_values(
            operation,
            values,
            codec=codec,
            actor=actor,
        )
    adapted = dict(values)
    policy = import_module("okto_pulse.core.domain.guideline_policy")
    compliance = import_module("okto_pulse.core.domain.guideline_compliance")
    ports = import_module("okto_pulse.core.ports.guideline_policy")
    cursor_kind = {
        "list_revisions": "revision",
        "list_impact_items": "impact",
    }.get(operation)
    if cursor_kind is not None:
        if codec is None:  # pragma: no cover - facade invariant
            raise RuntimeError("guideline_policy_cursor_codec_missing")
        token = adapted.get("cursor")
        adapted["cursor"] = (
            codec.decode(token, expected_kind=cursor_kind)
            if token is not None
            else None
        )
    if "entity_type" in adapted and isinstance(adapted["entity_type"], str):
        adapted["entity_type"] = policy.PolicyEntityType(adapted["entity_type"])
    if "status" in adapted and isinstance(adapted["status"], str):
        if operation == "retire_guideline":
            adapted["status"] = policy.GuidelineLifecycleStatus(adapted["status"])
    if "proposed_enforcement" in adapted:
        adapted["proposed_enforcement"] = policy.GuidelineEnforcement(
            adapted["proposed_enforcement"]
        )
    if "projection" in adapted:
        adapted["projection"] = compliance.PolicyProjection(adapted["projection"])
    if operation == "create_revision":
        lifecycle = import_module("okto_pulse.core.domain.guideline_lifecycle")
        patch = adapted["patch"]
        adapted["patch"] = lifecycle.GuidelineRevisionPatch(
            title=patch.get("title"),
            content=patch.get("content"),
            tags=(tuple(patch["tags"]) if patch.get("tags") is not None else None),
            metrics=(
                tuple(_domain_metric(item) for item in patch["metrics"])
                if patch.get("metrics") is not None
                else None
            ),
        )
    if operation == "list_impact_items":
        guideline_id = adapted.pop("guideline_id")
        item_kind = adapted.pop("item_kind", None)
        adapted = {
            "guideline_id": guideline_id,
            "query": ports.GuidelineImpactListQuery(
                **adapted,
                guideline_id=guideline_id,
                item_kind=(
                    policy.GuidelineImpactItemKind(item_kind)
                    if item_kind is not None
                    else None
                ),
            )
        }
    return adapted


class CorePolicyGovernanceFacade:
    """Late-bound Core invocation seam, also replaceable in route tests."""

    async def execute(
        self,
        operation: str,
        values: dict[str, Any],
        *,
        actor: Any,
        uow: PulseUnitOfWork,
    ) -> object:
        paginated = operation in {
            "list_revisions",
            "list_impact_items",
            "list_semantic_assessments",
            "list_semantic_findings",
            "list_semantic_waivers",
            "list_semantic_skips",
        }
        codec = None
        if paginated:
            from okto_pulse.core import get_settings
            from okto_pulse.core.inbound.guideline_policy_cursor import (
                policy_cursor_codec_from_settings,
            )

            codec = policy_cursor_codec_from_settings(get_settings())
        semantic_contract = _SEMANTIC_OPERATION_TYPES.get(operation)
        if semantic_contract is None:
            command_name, use_case_name = _OPERATION_TYPES[operation]
            module_name = (
                "okto_pulse.core.application.use_cases.policy_governance"
            )
        else:
            module_name, command_name, use_case_name = semantic_contract
        module = import_module(module_name)
        command_type = getattr(module, command_name)
        use_case_type = getattr(module, use_case_name)
        command = command_type(
            **_adapt_values(
                operation,
                values,
                codec=codec,
                actor=actor,
            )
        )
        result = await use_case_type().execute(command, actor=actor, uow=uow)
        if operation == "record_semantic_assessment_v2":
            return module.semantic_assessment_v2_write_projection(result)
        return _project_core_result(
            result,
            codec=codec,
            operation=operation,
        )


_DEFAULT_FACADE = CorePolicyGovernanceFacade()


def get_policy_governance_facade(request: Request) -> PolicyGovernanceFacade:
    return getattr(
        request.app.state,
        "policy_governance_facade",
        _DEFAULT_FACADE,
    )


def _temporary_http_error(exc: Exception) -> HTTPException:
    """Project through the shared bounded Core error contract."""

    from okto_pulse.core.inbound.guideline_policy_error import (
        guideline_policy_http_status,
        project_guideline_policy_error,
    )

    try:
        http_status = guideline_policy_http_status(exc)
        detail = project_guideline_policy_error(exc)
    except TypeError:
        raise exc
    return HTTPException(
        status_code=http_status,
        detail=detail,
    )


def _actor(principal: Principal, *, board_id: str) -> object:
    return RESTAdapterContract.actor_from_principal(
        principal,
        board_id=board_id,
    )


def _wire_result(result: object) -> object:
    envelope = getattr(result, "envelope", None)
    if envelope is not None:
        return guideline_export_payload(envelope)
    return jsonable_encoder(result)


def _restore_required_nones(item: object, payload: object) -> None:
    """Recursively re-add required-but-null dataclass fields after encoding.

    ``exclude_none`` drops nulls at EVERY depth, but the closed response
    models require explicit nulls for no-default nullable fields at every
    depth too (e.g. ``metric_results[].pinpoints[].excerpt_hash``). The
    top-level-only restore missed nested projections and turned the first
    real detail/full page into a 500.
    """

    if is_dataclass(item) and not isinstance(item, type):
        if not isinstance(payload, dict):
            return
        for dataclass_field in fields(item):
            value = getattr(item, dataclass_field.name)
            required = (
                dataclass_field.default is MISSING
                and dataclass_field.default_factory is MISSING
            )
            if value is None:
                if required:
                    payload[dataclass_field.name] = None
                continue
            _restore_required_nones(value, payload.get(dataclass_field.name))
        return
    if isinstance(item, tuple | list) and isinstance(payload, list):
        for child, child_payload in zip(item, payload, strict=False):
            _restore_required_nones(child, child_payload)


def _jsonable_page_items(items: object) -> object:
    """Keep projections slim without deleting required nullable fields.

    A dataclass field with no default remains required even when its annotation
    permits ``None``. Preserve those explicit nulls so validation and the
    route's ``response_model_exclude_unset`` serialization retain them, while
    optional projection-only fields stay absent from the Pydantic field set.
    The restore is recursive: nested projections (metric results, pinpoints,
    evidence refs) carry required nullable fields as well.
    """

    encoded = jsonable_encoder(items, exclude_none=True)
    if not isinstance(items, tuple | list) or not isinstance(encoded, list):
        return encoded
    _restore_required_nones(list(items), encoded)
    return encoded


def _project_core_result(
    result: object,
    *,
    codec: PolicyCursorCodec | None,
    operation: str = "",
) -> object:
    """Keep Core cursors opaque while preserving its immutable projections."""

    page = getattr(result, "page", None)
    if page is not None:
        if codec is None:  # pragma: no cover - facade invariant
            raise RuntimeError("guideline_policy_cursor_codec_missing")
        next_cursor = getattr(page, "next_cursor", None)
        projected = {
            "items": _jsonable_page_items(page.items),
            "has_more": page.has_more,
            "next_cursor": (
                codec.encode(next_cursor)
                if next_cursor is not None
                else None
            ),
        }
        if operation.startswith("list_semantic_"):
            projected["projection"] = page.projection
        else:
            projected["limit"] = page.limit
        return projected

    if operation == "record_semantic_assessment":
        assessment = result.assessment
        receipt = assessment.receipt
        return {
            "receipt_id": receipt.receipt_id,
            "state": receipt.state,
            "confidence_admissible": receipt.confidence_admissible,
            "metric_results": [
                {
                    "metric_result_id": metric.metric_result_id,
                    "metric_id": metric.metric_id,
                    "metric_code": metric.metric_code,
                    "score": metric.score,
                    "direction": metric.direction,
                    "default_threshold": metric.default_threshold,
                    "effective_threshold": metric.effective_threshold,
                    "threshold_source": metric.threshold_source,
                    "outcome": metric.outcome,
                }
                for metric in receipt.metric_results
            ],
            "replayed": assessment.replayed,
        }

    if operation in {
        "request_semantic_waiver",
        "review_semantic_waiver",
        "revoke_semantic_waiver",
    }:
        mutation = result.mutation
        waiver = mutation.waiver
        if operation == "request_semantic_waiver":
            return {
                "waiver_id": waiver.waiver_id,
                "status": waiver.status,
                "scope_digest": waiver.scope_digest,
            }
        if operation == "review_semantic_waiver":
            return {
                "waiver_id": waiver.waiver_id,
                "waiver_revision": waiver.waiver_revision,
                "status": waiver.status,
                "reviewer_id": mutation.event.actor_id,
                "replayed": result.replayed,
            }
        return {
            "waiver_id": waiver.waiver_id,
            "waiver_revision": waiver.waiver_revision,
            "status": waiver.status,
            "replayed": result.replayed,
        }

    if operation == "revalidate_semantic_waiver":
        return {
            "waiver_id": result.waiver_id,
            "waiver_revision": result.waiver_revision,
            "status": result.status,
            "current": result.current,
            "reason_code": result.reason_code,
            "replayed": result.replayed,
        }

    if operation in {"create_semantic_skip", "revoke_semantic_skip"}:
        skip = result.mutation.skip
        if operation == "create_semantic_skip":
            return {
                "skip_id": skip.skip_id,
                "scope_digest": skip.scope_digest,
                "created_by": skip.created_by,
            }
        return {
            "skip_id": skip.skip_id,
            "skip_revision": skip.skip_revision,
            "status": skip.status,
            "revoked_by": skip.revoked_by,
            "replayed": result.replayed,
        }

    return result


async def _execute(
    facade: PolicyGovernanceFacade,
    operation: str,
    values: dict[str, Any],
    *,
    principal: Principal,
    board_id: str,
    uow: PulseUnitOfWork,
) -> object:
    semantic_contract_version = {
        "record_semantic_assessment": "v1",
        "record_semantic_assessment_v2": "v2",
    }.get(operation)
    try:
        result = await facade.execute(
            operation,
            values,
            actor=_actor(principal, board_id=board_id),
            uow=uow,
        )
    except Exception as exc:
        http_error = _temporary_http_error(exc)
        if semantic_contract_version is not None:
            from okto_pulse.core.services.governance_observability import (
                emit_semantic_assessment_write_metric,
            )

            detail = http_error.detail
            reason_code = (
                detail.get("code") if isinstance(detail, dict) else None
            )
            capability_state = (
                detail.get("details", {}).get("capability_state")
                if isinstance(detail, dict)
                and isinstance(detail.get("details"), dict)
                else None
            )
            emit_semantic_assessment_write_metric(
                surface="rest",
                contract_version=semantic_contract_version,
                outcome="error",
                reason_code=(
                    reason_code if isinstance(reason_code, str) else None
                ),
                capability_state=(
                    capability_state
                    if isinstance(capability_state, str)
                    else None
                ),
            )
        raise http_error from exc
    if semantic_contract_version is not None:
        from okto_pulse.core.services.governance_observability import (
            emit_semantic_assessment_write_metric,
        )

        emit_semantic_assessment_write_metric(
            surface="rest",
            contract_version=semantic_contract_version,
            outcome="success",
        )
    return _wire_result(result)


# Literal governance routes are intentionally registered first.


@router.get(
    "/boards/{board_id}/guidelines/export",
    response_model=GuidelineExportV3Request,
)
async def export_guideline_policy_v3(
    board_id: BoardId,
    guideline_ids: list[GuidelineId] | None = Query(default=None),
    include_binding_history: bool = Query(default=True),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    from okto_pulse.core.application.use_cases.guideline_import_export import (
        ExportGuidelinePolicyCommand,
        ExportGuidelinePolicyV3UseCase,
    )

    try:
        result = await ExportGuidelinePolicyV3UseCase().execute(
            ExportGuidelinePolicyCommand(
                board_id=board_id,
                guideline_ids=tuple(guideline_ids or ()),
                include_binding_history=include_binding_history,
            ),
            actor=_actor(principal, board_id=board_id),
            uow=uow,
        )
    except Exception as exc:
        raise _temporary_http_error(exc) from exc
    return guideline_export_payload(result.envelope)


@router.post(
    "/boards/{board_id}/guidelines/import",
    response_model=GuidelineImportResultResponse,
    response_model_exclude_none=True,
)
async def import_guideline_policy_v3(
    board_id: BoardId,
    envelope: GuidelineExportV3Request,
    dry_run: bool = Query(default=False),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    from okto_pulse.core.application.use_cases.guideline_import_export import (
        ImportGuidelinePolicyCommand,
        ImportGuidelinePolicyUseCase,
    )

    try:
        result = await ImportGuidelinePolicyUseCase().execute(
            ImportGuidelinePolicyCommand(
                envelope=envelope.model_dump(mode="json"),
                target_board_id=board_id,
                dry_run=dry_run,
            ),
            actor=_actor(principal, board_id=board_id),
            uow=uow,
        )
    except Exception as exc:
        raise _temporary_http_error(exc) from exc
    return jsonable_encoder(result.result)


@router.get(
    "/boards/{board_id}/guidelines/{guideline_id}/revisions",
    response_model=GuidelineRevisionPageResponse,
    response_model_exclude_unset=True,
)
async def list_guideline_revisions(
    board_id: BoardId,
    guideline_id: GuidelineId,
    limit: int = Query(POLICY_PAGE_LIMIT_DEFAULT, ge=1, le=POLICY_PAGE_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    projection: PolicyProjection = Query(PolicyProjection.SUMMARY),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_revisions",
        {
            "board_id": board_id,
            "guideline_id": guideline_id,
            "limit": limit,
            "cursor": cursor,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/guidelines/{guideline_id}/revisions",
    response_model=CreateGuidelineRevisionResponse,
    response_model_exclude_none=True,
    responses={
        201: {
            "model": CreateGuidelineRevisionResponse,
            "description": "Revision applied or applied replay.",
        }
    },
)
async def create_guideline_revision(
    board_id: BoardId,
    guideline_id: GuidelineId,
    data: CreateGuidelineRevisionRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    payload = await _execute(
        facade,
        "create_revision",
        {
            "board_id": board_id,
            "guideline_id": guideline_id,
            **data.model_dump(mode="python"),
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )
    projected = CreateGuidelineRevisionResponse.model_validate(payload)
    if projected.status == "rejected":
        from okto_pulse.core.application.use_cases.base import (
            CommandValidationError,
        )

        raise _temporary_http_error(
            CommandValidationError(
                projected.rejection_code or "guideline_revision_rejected"
            )
        )
    response_status = (
        status.HTTP_201_CREATED
        if projected.status == "applied"
        else status.HTTP_200_OK
    )
    return JSONResponse(
        status_code=response_status,
        content=jsonable_encoder(
            projected.model_dump(exclude_none=True),
        ),
    )


@router.get(
    "/boards/{board_id}/guidelines/{guideline_id}/revisions/{revision_id}",
    response_model=GuidelineRevisionAuthorityResponse,
    response_model_exclude_none=True,
)
async def get_guideline_revision(
    board_id: BoardId,
    guideline_id: GuidelineId,
    revision_id: RevisionId,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_revision",
        {
            "board_id": board_id,
            "guideline_id": guideline_id,
            "revision_id": revision_id,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/guidelines/{guideline_id}/retire",
    response_model=RetirementResponse,
)
async def retire_guideline(
    board_id: BoardId,
    guideline_id: GuidelineId,
    data: RetireGuidelineRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "retire_guideline",
        {
            "board_id": board_id,
            "guideline_id": guideline_id,
            **data.model_dump(mode="python"),
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/guidelines/{guideline_id}/impact-previews",
    status_code=status.HTTP_201_CREATED,
    response_model=GuidelineImpactReceiptResponse,
)
async def preview_guideline_impact(
    board_id: BoardId,
    guideline_id: GuidelineId,
    data: PreviewGuidelineImpactRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "preview_impact",
        {
            "board_id": board_id,
            "guideline_id": guideline_id,
            **data.model_dump(mode="python"),
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/guidelines/{guideline_id}/impact-previews/{receipt_id}",
    response_model=GuidelineImpactReceiptResponse,
)
async def get_guideline_impact(
    board_id: BoardId,
    guideline_id: GuidelineId,
    receipt_id: ImpactReceiptId,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_impact",
        {
            "board_id": board_id,
            "guideline_id": guideline_id,
            "impact_receipt_id": receipt_id,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/guidelines/{guideline_id}/impact-previews/{receipt_id}/items",
    response_model=GuidelineImpactItemPageResponse,
    response_model_exclude_unset=True,
)
async def list_guideline_impact_items(
    board_id: BoardId,
    guideline_id: GuidelineId,
    receipt_id: ImpactReceiptId,
    limit: int = Query(POLICY_PAGE_LIMIT_DEFAULT, ge=1, le=POLICY_PAGE_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    entity_type: PolicyEntityType | None = Query(default=None),
    item_kind: GuidelineImpactItemKind | None = Query(default=None),
    projection: PolicyProjection = Query(PolicyProjection.SUMMARY),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_impact_items",
        {
            "board_id": board_id,
            "guideline_id": guideline_id,
            "impact_receipt_id": receipt_id,
            "limit": limit,
            "cursor": cursor,
            "entity_type": entity_type.value if entity_type else None,
            "item_kind": item_kind.value if item_kind else None,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/guidelines/{guideline_id}/adoptions",
    response_model=AdoptionResponse,
)
async def adopt_guideline_revision(
    board_id: BoardId,
    guideline_id: GuidelineId,
    data: AdoptGuidelineRevisionRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "adopt_revision",
        {
            "board_id": board_id,
            "guideline_id": guideline_id,
            **data.model_dump(mode="python"),
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/semantic-guideline-assessments",
    status_code=status.HTTP_201_CREATED,
    response_model=RecordedSemanticAssessmentResponse,
)
async def record_semantic_guideline_assessment(
    board_id: BoardId,
    data: RecordSemanticGuidelineAssessmentRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "record_semantic_assessment",
        {"board_id": board_id, **data.model_dump(mode="python")},
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/semantic-guideline-assessments/v2",
    status_code=status.HTTP_201_CREATED,
    response_model=RecordedSemanticAssessmentV2Response,
    responses=_SEMANTIC_V2_UNPROCESSABLE_RESPONSE,
)
async def record_semantic_guideline_assessment_v2(
    board_id: BoardId,
    data: RecordSemanticGuidelineAssessmentV2Request,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "record_semantic_assessment_v2",
        {"board_id": board_id, **data.model_dump(mode="python")},
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/semantic-guideline-assessments",
    response_model=SemanticAssessmentPageResponse,
    response_model_exclude_unset=True,
)
async def list_semantic_guideline_assessments(
    board_id: BoardId,
    subject_type: PolicyEntityType | None = Query(default=None),
    subject_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    guideline_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=GUIDELINE_ID_MAX_LENGTH,
    ),
    binding_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=GUIDELINE_BINDING_ID_MAX_LENGTH,
    ),
    outcome: SemanticAssessmentOutcome | None = Query(default=None),
    currentness: PolicyCurrentness | None = Query(default=None),
    projection: SemanticPolicyProjection = Query(
        SemanticPolicyProjection.SUMMARY
    ),
    limit: int = Query(
        POLICY_PAGE_LIMIT_DEFAULT,
        ge=1,
        le=POLICY_PAGE_LIMIT_MAX,
    ),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_semantic_assessments",
        {
            "board_id": board_id,
            "limit": limit,
            "cursor": cursor,
            "subject_type": (
                subject_type.value if subject_type else None
            ),
            "subject_id": subject_id,
            "guideline_id": guideline_id,
            "binding_id": binding_id,
            "outcome": outcome.value if outcome else None,
            "currentness": (
                currentness.value if currentness else None
            ),
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/semantic-guideline-assessments/current",
    response_model=SemanticAssessmentResponse,
    response_model_exclude_unset=True,
)
async def get_current_semantic_guideline_assessment(
    board_id: BoardId,
    subject_type: PolicyEntityType = Query(...),
    subject_id: str = Query(
        ...,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    binding_id: str = Query(
        ...,
        min_length=1,
        max_length=GUIDELINE_BINDING_ID_MAX_LENGTH,
    ),
    projection: SemanticPolicyProjection = Query(
        SemanticPolicyProjection.FULL
    ),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_current_semantic_assessment",
        {
            "board_id": board_id,
            "subject_type": subject_type.value,
            "subject_id": subject_id,
            "binding_id": binding_id,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/semantic-guideline-assessments/{receipt_id}",
    response_model=SemanticAssessmentResponse,
    response_model_exclude_unset=True,
)
async def get_semantic_guideline_assessment(
    board_id: BoardId,
    receipt_id: ComplianceReceiptId,
    projection: SemanticPolicyProjection = Query(
        SemanticPolicyProjection.FULL
    ),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_semantic_assessment",
        {
            "board_id": board_id,
            "receipt_id": receipt_id,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/semantic-guideline-findings",
    response_model=SemanticFindingPageResponse,
    response_model_exclude_unset=True,
)
async def list_semantic_guideline_findings(
    board_id: BoardId,
    receipt_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_RECEIPT_ID_MAX_LENGTH,
    ),
    guideline_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=GUIDELINE_ID_MAX_LENGTH,
    ),
    binding_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=GUIDELINE_BINDING_ID_MAX_LENGTH,
    ),
    metric_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_METRIC_ID_MAX_LENGTH,
    ),
    subject_type: PolicyEntityType | None = Query(default=None),
    subject_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    outcome: SemanticMetricOutcome | None = Query(default=None),
    projection: SemanticPolicyProjection = Query(
        SemanticPolicyProjection.SUMMARY
    ),
    limit: int = Query(
        POLICY_PAGE_LIMIT_DEFAULT,
        ge=1,
        le=POLICY_PAGE_LIMIT_MAX,
    ),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_semantic_findings",
        {
            "board_id": board_id,
            "limit": limit,
            "cursor": cursor,
            "receipt_id": receipt_id,
            "guideline_id": guideline_id,
            "subject_id": subject_id,
            "subject_type": (
                subject_type.value if subject_type else None
            ),
            "binding_id": binding_id,
            "metric_id": metric_id,
            "outcome": outcome.value if outcome else None,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-waivers",
    response_model=SemanticWaiverPageResponse,
    response_model_exclude_unset=True,
)
async def list_semantic_metric_waivers(
    board_id: BoardId,
    evaluated_at: datetime = Query(...),
    finding_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_FINDING_ID_MAX_LENGTH,
    ),
    metric_result_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_RECEIPT_ID_MAX_LENGTH,
    ),
    receipt_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_RECEIPT_ID_MAX_LENGTH,
    ),
    guideline_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=GUIDELINE_ID_MAX_LENGTH,
    ),
    binding_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=GUIDELINE_BINDING_ID_MAX_LENGTH,
    ),
    metric_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_METRIC_ID_MAX_LENGTH,
    ),
    subject_type: PolicyEntityType | None = Query(default=None),
    subject_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    waiver_status: SemanticWaiverStatus | None = Query(
        default=None,
        alias="status",
    ),
    projection: SemanticPolicyProjection = Query(
        SemanticPolicyProjection.SUMMARY
    ),
    limit: int = Query(
        POLICY_PAGE_LIMIT_DEFAULT,
        ge=1,
        le=POLICY_PAGE_LIMIT_MAX,
    ),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_semantic_waivers",
        {
            "board_id": board_id,
            "evaluated_at": evaluated_at,
            "limit": limit,
            "cursor": cursor,
            "finding_id": finding_id,
            "metric_result_id": metric_result_id,
            "receipt_id": receipt_id,
            "guideline_id": guideline_id,
            "binding_id": binding_id,
            "metric_id": metric_id,
            "subject_type": (
                subject_type.value if subject_type else None
            ),
            "subject_id": subject_id,
            "status": waiver_status.value if waiver_status else None,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/policy-waivers",
    status_code=status.HTTP_201_CREATED,
    response_model=RequestedSemanticWaiverResponse,
)
async def request_semantic_metric_waiver(
    board_id: BoardId,
    data: RequestSemanticWaiverRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "request_semantic_waiver",
        {"board_id": board_id, **data.model_dump(mode="python")},
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-waivers/{waiver_id}/events",
    response_model=SemanticWaiverEventsResponse,
)
async def list_semantic_metric_waiver_events(
    board_id: BoardId,
    waiver_id: WaiverId,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_semantic_waiver_events",
        {"board_id": board_id, "waiver_id": waiver_id},
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/policy-waivers/{waiver_id}/review",
    response_model=ReviewedSemanticWaiverResponse,
)
async def review_semantic_metric_waiver(
    board_id: BoardId,
    waiver_id: WaiverId,
    data: ReviewSemanticWaiverRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "review_semantic_waiver",
        {
            "board_id": board_id,
            "waiver_id": waiver_id,
            **data.model_dump(mode="python"),
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/policy-waivers/{waiver_id}/revoke",
    response_model=RevokedSemanticWaiverResponse,
)
async def revoke_semantic_metric_waiver(
    board_id: BoardId,
    waiver_id: WaiverId,
    data: RevokeSemanticWaiverRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "revoke_semantic_waiver",
        {
            "board_id": board_id,
            "waiver_id": waiver_id,
            **data.model_dump(mode="python"),
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/policy-waivers/{waiver_id}/revalidate",
    response_model=RevalidatedSemanticWaiverResponse,
)
async def revalidate_semantic_metric_waiver(
    board_id: BoardId,
    waiver_id: WaiverId,
    data: RevalidateSemanticWaiverRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "revalidate_semantic_waiver",
        {
            "board_id": board_id,
            "waiver_id": waiver_id,
            **data.model_dump(mode="python"),
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-waivers/{waiver_id}",
    response_model=SemanticWaiverResponse,
    response_model_exclude_unset=True,
)
async def get_semantic_metric_waiver(
    board_id: BoardId,
    waiver_id: WaiverId,
    evaluated_at: datetime = Query(...),
    projection: SemanticPolicyProjection = Query(
        SemanticPolicyProjection.FULL
    ),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_semantic_waiver",
        {
            "board_id": board_id,
            "waiver_id": waiver_id,
            "evaluated_at": evaluated_at,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/semantic-guideline-skips",
    response_model=SemanticSkipPageResponse,
    response_model_exclude_unset=True,
)
async def list_semantic_policy_skips(
    board_id: BoardId,
    subject_type: PolicyEntityType | None = Query(default=None),
    subject_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    binding_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=GUIDELINE_BINDING_ID_MAX_LENGTH,
    ),
    skip_status: Literal["active", "revoked"] | None = Query(
        default=None,
        alias="status",
    ),
    currentness: PolicyCurrentness | None = Query(default=None),
    projection: SemanticPolicyProjection = Query(
        SemanticPolicyProjection.SUMMARY
    ),
    limit: int = Query(
        POLICY_PAGE_LIMIT_DEFAULT,
        ge=1,
        le=POLICY_PAGE_LIMIT_MAX,
    ),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_semantic_skips",
        {
            "board_id": board_id,
            "limit": limit,
            "cursor": cursor,
            "subject_type": (
                subject_type.value if subject_type else None
            ),
            "subject_id": subject_id,
            "binding_id": binding_id,
            "status": skip_status,
            "currentness": (
                currentness.value if currentness else None
            ),
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/semantic-guideline-skips",
    status_code=status.HTTP_201_CREATED,
    response_model=CreatedSemanticSkipResponse,
)
async def create_semantic_policy_skip(
    board_id: BoardId,
    data: CreateSemanticPolicySkipRequest,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    ),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "create_semantic_skip",
        {
            "board_id": board_id,
            **data.model_dump(mode="python"),
            "idempotency_key": idempotency_key,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/semantic-guideline-skips/{skip_id}/revoke",
    response_model=RevokedSemanticSkipResponse,
)
async def revoke_semantic_policy_skip(
    board_id: BoardId,
    skip_id: ComplianceReceiptId,
    data: RevokeSemanticPolicySkipRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "revoke_semantic_skip",
        {
            "board_id": board_id,
            "skip_id": skip_id,
            **data.model_dump(mode="python"),
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/semantic-guideline-skips/{skip_id}",
    response_model=SemanticSkipResponse,
    response_model_exclude_unset=True,
)
async def get_semantic_policy_skip(
    board_id: BoardId,
    skip_id: ComplianceReceiptId,
    projection: SemanticPolicyProjection = Query(
        SemanticPolicyProjection.FULL
    ),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_semantic_skip",
        {
            "board_id": board_id,
            "skip_id": skip_id,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


__all__ = [
    "CorePolicyGovernanceFacade",
    "PolicyGovernanceFacade",
    "get_policy_governance_facade",
    "router",
]
