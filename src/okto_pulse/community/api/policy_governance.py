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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
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
    PolicyComplianceFindingListItem as CorePolicyComplianceFindingListItem,
    PolicyComplianceReceiptListItem as CorePolicyComplianceReceiptListItem,
    PolicyCursorCodec,
    PolicyWaiverListItem as CorePolicyWaiverListItem,
)
from okto_pulse.core.domain.guideline_import_export import (
    guideline_export_payload,
)
from okto_pulse.core.domain.guideline_lifecycle import GuidelineVersionBump
from okto_pulse.core.domain.guideline_policy import (
    GUIDELINE_BINDING_ID_MAX_LENGTH,
    GUIDELINE_ID_MAX_LENGTH,
    GUIDELINE_RETIREMENT_ID_MAX_LENGTH,
    GUIDELINE_REVISION_ID_MAX_LENGTH,
    GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    GUIDELINE_TITLE_MAX_LENGTH,
    POLICY_ACTOR_ID_MAX_LENGTH,
    POLICY_EVALUATION_ID_MAX_LENGTH,
    POLICY_FINDING_ID_MAX_LENGTH,
    POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    POLICY_IMPACT_RECEIPT_ID_MAX_LENGTH,
    POLICY_RECEIPT_ID_MAX_LENGTH,
    POLICY_RULE_ID_MAX_LENGTH,
    POLICY_SQL_INTEGER_MAX,
    POLICY_SUBJECT_ID_MAX_LENGTH,
    POLICY_WAIVER_EVENT_ID_MAX_LENGTH,
    POLICY_WAIVER_ID_MAX_LENGTH,
    BoardGuidelineBinding as CoreBoardGuidelineBinding,
    Guideline as CoreGuideline,
    GuidelineHead as CoreGuidelineHead,
    GuidelineImpactItem as CoreGuidelineImpactItem,
    GuidelineImpactReceipt as CoreGuidelineImpactReceipt,
    GuidelineRetirement as CoreGuidelineRetirement,
    GuidelineRevision as CoreGuidelineRevision,
    PolicyComplianceReceipt as CorePolicyComplianceReceipt,
    PolicyEvaluationResult as CorePolicyEvaluationResult,
    PolicyWaiver as CorePolicyWaiver,
    PolicyWaiverEvent as CorePolicyWaiverEvent,
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
                    GuidelineExportV2Request.model_validate_json(
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
    ]
    code: str
    error_code: str
    message: str
    category: Literal[
        "invalid_argument",
        "permission_denied",
        "not_found",
        "conflict",
        "service_unavailable",
    ]
    status_category: str
    http_status: Literal[400, 401, 403, 404, 409, 503]
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
    for dataclass_field in fields(domain_type):
        annotation = _closed_response_annotation(
            annotations.get(dataclass_field.name, dataclass_field.type)
        )
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


class GuidelineRuleOperator(str, Enum):
    ALL = "all"
    ANY = "any"


class GuidelineImpactItemKind(str, Enum):
    BINDING = "binding"
    TARGET = "target"
    ARTIFACT = "artifact"
    WAIVER = "waiver"


class PolicyEvaluationOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"


class PolicyCurrentness(str, Enum):
    CURRENT = "current"
    STALE = "stale"


class PolicyWaiverStatus(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"


class PolicyWaiverReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


PolicyScalar = str | int | float | bool | None


class GuidelinePredicateRequest(_ClosedModel):
    predicate_code: str = Field(min_length=1, max_length=200)
    parameters: dict[str, PolicyScalar | list[PolicyScalar]] = Field(
        default_factory=dict
    )


class GuidelineRuleRequest(_ClosedModel):
    rule_id: str = Field(min_length=1, max_length=POLICY_RULE_ID_MAX_LENGTH)
    code: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1)
    target_entity_types: list[PolicyEntityType] = Field(min_length=1)
    predicates: list[GuidelinePredicateRequest] = Field(min_length=1)
    enforcement: GuidelineEnforcement = GuidelineEnforcement.ADVISORY
    operator: GuidelineRuleOperator = GuidelineRuleOperator.ALL
    waivable: bool = False
    policy_class: str = Field(default="standard", min_length=1, max_length=200)


class GuidelineRevisionPatchRequest(_ClosedModel):
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=GUIDELINE_TITLE_MAX_LENGTH,
    )
    content: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = None
    rules: list[GuidelineRuleRequest] | None = None

    @model_validator(mode="after")
    def require_change(self) -> GuidelineRevisionPatchRequest:
        if all(
            value is None
            for value in (self.title, self.content, self.tags, self.rules)
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
    proposed_default_enforcement: GuidelineEnforcement
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


class EvaluatePolicyComplianceRequest(_ClosedModel):
    entity_type: PolicyEntityType
    subject_id: str = Field(
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    )
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    evaluation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=POLICY_EVALUATION_ID_MAX_LENGTH,
    )
    requested_at: datetime | None = None
    evaluated_at: datetime | None = None


class RequestPolicyWaiverRequest(_ClosedModel):
    waiver_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=POLICY_WAIVER_ID_MAX_LENGTH,
    )
    event_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=POLICY_WAIVER_EVENT_ID_MAX_LENGTH,
    )
    finding_id: str = Field(
        min_length=1,
        max_length=POLICY_FINDING_ID_MAX_LENGTH,
    )
    justification: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    expires_at: datetime
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    occurred_at: datetime | None = None


class ReviewPolicyWaiverRequest(_ClosedModel):
    decision: PolicyWaiverReviewDecision
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    expected_waiver_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    event_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=POLICY_WAIVER_EVENT_ID_MAX_LENGTH,
    )
    occurred_at: datetime | None = None


class RevokePolicyWaiverRequest(_ClosedModel):
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    expected_waiver_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    event_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=POLICY_WAIVER_EVENT_ID_MAX_LENGTH,
    )
    occurred_at: datetime | None = None


class RevalidatePolicyWaiverRequest(_ClosedModel):
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    expected_waiver_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    new_expires_at: datetime
    idempotency_key: str = Field(
        min_length=1,
        max_length=POLICY_IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    event_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=POLICY_WAIVER_EVENT_ID_MAX_LENGTH,
    )
    occurred_at: datetime | None = None


class GuidelineExportPredicateV2(_ClosedModel):
    predicate_code: str
    parameters: list[tuple[str, PolicyScalar | list[PolicyScalar]]]


class GuidelineExportRuleV2(_ClosedModel):
    rule_id: str = Field(max_length=POLICY_RULE_ID_MAX_LENGTH)
    code: str
    title: str
    description: str
    target_entity_types: list[PolicyEntityType]
    predicates: list[GuidelineExportPredicateV2]
    enforcement: GuidelineEnforcement
    operator: GuidelineRuleOperator
    waivable: bool
    policy_class: str | None


class GuidelineExportRevisionV2(_ClosedModel):
    revision_id: RevisionId
    guideline_id: GuidelineId
    revision_number: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    semantic_version: str = Field(
        min_length=1,
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    title: str = Field(min_length=1, max_length=GUIDELINE_TITLE_MAX_LENGTH)
    content: str
    content_digest: str
    rules: list[GuidelineExportRuleV2]
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


class GuidelineExportIdentityV2(_ClosedModel):
    guideline_id: GuidelineId
    owner_id: str = Field(min_length=1, max_length=POLICY_ACTOR_ID_MAX_LENGTH)
    scope: GuidelineScope
    board_id: BoardId | None
    context_scope: GuidelineContextScope
    created_at: datetime


class GuidelineExportHeadV2(_ClosedModel):
    guideline_id: GuidelineId
    revision_id: RevisionId
    revision_number: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    semantic_version: str = Field(
        min_length=1,
        max_length=GUIDELINE_SEMANTIC_VERSION_MAX_LENGTH,
    )
    head_revision: int = Field(ge=1, le=POLICY_SQL_INTEGER_MAX)
    updated_at: datetime


class GuidelineExportRetirementV2(_ClosedModel):
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


class GuidelineExportLogicalBindingV2(_ClosedModel):
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
    default_enforcement: GuidelineEnforcement
    state: GuidelineBindingState
    source_kind: GuidelineBindingProvenance


class GuidelineExportBindingV2(_ClosedModel):
    binding: GuidelineExportLogicalBindingV2
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


class GuidelineExportAggregateV2(_ClosedModel):
    identity: GuidelineExportIdentityV2
    revisions: list[GuidelineExportRevisionV2]
    head: GuidelineExportHeadV2
    retirement: GuidelineExportRetirementV2 | None
    bindings: list[GuidelineExportBindingV2]
    history_status: GuidelineHistoryStatus
    migration_notes: list[str]


class GuidelineExportV2Request(_ClosedModel):
    contract_version: Literal["guideline-export/v2"]
    schema_version: Literal["2"]
    kind: Literal["guidelines"]
    exported_at: datetime
    source_board_id: BoardId | None
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    guidelines: list[GuidelineExportAggregateV2]


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
ClosedPolicyComplianceReceiptListItem = _closed_dataclass_response_model(
    CorePolicyComplianceReceiptListItem
)
ClosedPolicyComplianceFindingListItem = _closed_dataclass_response_model(
    CorePolicyComplianceFindingListItem
)
ClosedPolicyWaiverListItem = _closed_dataclass_response_model(
    CorePolicyWaiverListItem
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
ClosedPolicyComplianceReceipt = _closed_dataclass_response_model(
    CorePolicyComplianceReceipt
)
ClosedBoardGuidelineBinding = _closed_dataclass_response_model(
    CoreBoardGuidelineBinding
)
ClosedPolicyEvaluationResult = _closed_dataclass_response_model(
    CorePolicyEvaluationResult
)
ClosedPolicyWaiver = _closed_dataclass_response_model(CorePolicyWaiver)
ClosedPolicyWaiverEvent = _closed_dataclass_response_model(CorePolicyWaiverEvent)


class _PolicyPageResponse(_ClosedModel):
    limit: int
    has_more: bool
    next_cursor: str | None


class GuidelineRevisionPageResponse(_PolicyPageResponse):
    items: list[ClosedGuidelineRevisionListItem]


class GuidelineImpactItemPageResponse(_PolicyPageResponse):
    items: list[ClosedGuidelineImpactItem]


class PolicyComplianceReceiptPageResponse(_PolicyPageResponse):
    items: list[ClosedPolicyComplianceReceiptListItem]


class PolicyComplianceFindingPageResponse(_PolicyPageResponse):
    items: list[ClosedPolicyComplianceFindingListItem]


class PolicyWaiverPageResponse(_PolicyPageResponse):
    items: list[ClosedPolicyWaiverListItem]


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


class PolicyComplianceReceiptResponse(_ClosedModel):
    receipt: ClosedPolicyComplianceReceipt


class AdoptionResponse(_ClosedModel):
    binding: ClosedBoardGuidelineBinding
    receipt: ClosedGuidelineImpactReceipt


class EvaluationResponse(_ClosedModel):
    evaluation: ClosedPolicyEvaluationResult


class WaiverResponse(_ClosedModel):
    waiver: ClosedPolicyWaiver


class WaiverEventsResponse(_ClosedModel):
    events: list[ClosedPolicyWaiverEvent]


class WaiverMutationResponse(_ClosedModel):
    waiver: ClosedPolicyWaiver
    event: ClosedPolicyWaiverEvent


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
    "evaluate_compliance": (
        "EvaluatePolicyComplianceCommand",
        "EvaluatePolicyComplianceUseCase",
    ),
    "list_compliance_receipts": (
        "ListPolicyComplianceReceiptsCommand",
        "ListPolicyComplianceReceiptsUseCase",
    ),
    "get_compliance_receipt": (
        "GetPolicyComplianceReceiptCommand",
        "GetPolicyComplianceReceiptUseCase",
    ),
    "get_current_compliance": (
        "GetCurrentPolicyComplianceReceiptCommand",
        "GetCurrentPolicyComplianceReceiptUseCase",
    ),
    "list_compliance_findings": (
        "ListPolicyComplianceFindingsCommand",
        "ListPolicyComplianceFindingsUseCase",
    ),
    "list_waivers": ("ListPolicyWaiversCommand", "ListPolicyWaiversUseCase"),
    "get_waiver": ("GetPolicyWaiverCommand", "GetPolicyWaiverUseCase"),
    "list_waiver_events": (
        "ListPolicyWaiverEventsCommand",
        "ListPolicyWaiverEventsUseCase",
    ),
    "request_waiver": (
        "RequestPolicyWaiverCommand",
        "RequestPolicyWaiverUseCase",
    ),
    "review_waiver": (
        "ReviewPolicyWaiverCommand",
        "ReviewPolicyWaiverUseCase",
    ),
    "revoke_waiver": (
        "RevokePolicyWaiverCommand",
        "RevokePolicyWaiverUseCase",
    ),
    "revalidate_waiver": (
        "RevalidatePolicyWaiverCommand",
        "RevalidatePolicyWaiverUseCase",
    ),
}


def _domain_rule(payload: dict[str, Any]) -> object:
    policy = import_module("okto_pulse.core.domain.guideline_policy")
    predicates = tuple(
        policy.GuidelinePredicate(
            item["predicate_code"],
            tuple(sorted(item["parameters"].items())),
        )
        for item in payload["predicates"]
    )
    return policy.GuidelineRule(
        rule_id=payload["rule_id"],
        code=payload["code"],
        title=payload["title"],
        description=payload["description"],
        target_entity_types=tuple(
            policy.PolicyEntityType(item) for item in payload["target_entity_types"]
        ),
        predicates=predicates,
        enforcement=policy.GuidelineEnforcement(payload["enforcement"]),
        operator=policy.GuidelineRuleOperator(payload["operator"]),
        waivable=payload["waivable"],
        policy_class=payload["policy_class"],
    )


def _adapt_values(
    operation: str,
    values: dict[str, Any],
    *,
    codec: PolicyCursorCodec | None,
) -> dict[str, Any]:
    """Convert REST enums/nested models to Core-owned immutable values."""

    adapted = dict(values)
    policy = import_module("okto_pulse.core.domain.guideline_policy")
    compliance = import_module("okto_pulse.core.domain.guideline_compliance")
    ports = import_module("okto_pulse.core.ports.guideline_policy")
    cursor_kind = {
        "list_revisions": "revision",
        "list_impact_items": "impact",
        "list_compliance_receipts": "receipt",
        "list_compliance_findings": "finding",
        "list_waivers": "waiver",
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
    if "outcome" in adapted and isinstance(adapted["outcome"], str):
        adapted["outcome"] = policy.PolicyEvaluationOutcome(adapted["outcome"])
    if "currentness" in adapted and isinstance(adapted["currentness"], str):
        adapted["currentness"] = policy.PolicyCurrentness(adapted["currentness"])
    if "status" in adapted and isinstance(adapted["status"], str):
        if operation == "list_waivers":
            adapted["status"] = policy.PolicyWaiverStatus(adapted["status"])
        elif operation == "retire_guideline":
            adapted["status"] = policy.GuidelineLifecycleStatus(adapted["status"])
    if "proposed_default_enforcement" in adapted:
        adapted["proposed_default_enforcement"] = policy.GuidelineEnforcement(
            adapted["proposed_default_enforcement"]
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
            rules=(
                tuple(_domain_rule(item) for item in patch["rules"])
                if patch.get("rules") is not None
                else None
            ),
        )
    if operation in {
        "request_waiver",
        "review_waiver",
        "revoke_waiver",
        "revalidate_waiver",
    }:
        adapted["evidence_refs"] = tuple(adapted.get("evidence_refs") or ())
    if operation == "review_waiver":
        adapted["approve"] = adapted.pop("decision") == "approve"
    if operation == "request_waiver":
        adapted["reason"] = adapted.pop("justification")
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
    elif operation == "list_compliance_receipts":
        adapted = {"query": ports.PolicyComplianceReceiptListQuery(**adapted)}
    elif operation == "list_compliance_findings":
        adapted = {"query": ports.PolicyComplianceFindingListQuery(**adapted)}
    elif operation == "list_waivers":
        adapted = {"query": ports.PolicyWaiverListQuery(**adapted)}
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
            "list_compliance_receipts",
            "list_compliance_findings",
            "list_waivers",
        }
        codec = None
        if paginated:
            from okto_pulse.core import get_settings
            from okto_pulse.core.inbound.guideline_policy_cursor import (
                policy_cursor_codec_from_settings,
            )

            codec = policy_cursor_codec_from_settings(get_settings())
        command_name, use_case_name = _OPERATION_TYPES[operation]
        module = import_module(
            "okto_pulse.core.application.use_cases.policy_governance"
        )
        command_type = getattr(module, command_name)
        use_case_type = getattr(module, use_case_name)
        command = command_type(**_adapt_values(operation, values, codec=codec))
        result = await use_case_type().execute(command, actor=actor, uow=uow)
        return _project_core_result(result, codec=codec)


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


def _jsonable_page_items(items: object) -> object:
    """Keep projections slim without deleting required nullable fields.

    A dataclass field with no default remains required even when its annotation
    permits ``None``. Preserve those explicit nulls so validation and the
    route's ``response_model_exclude_unset`` serialization retain them, while
    optional projection-only fields stay absent from the Pydantic field set.
    """

    encoded = jsonable_encoder(items, exclude_none=True)
    if not isinstance(items, tuple | list) or not isinstance(encoded, list):
        return encoded
    for item, payload in zip(items, encoded, strict=True):
        if not is_dataclass(item) or not isinstance(payload, dict):
            continue
        for dataclass_field in fields(item):
            required = (
                dataclass_field.default is MISSING
                and dataclass_field.default_factory is MISSING
            )
            if required and getattr(item, dataclass_field.name) is None:
                payload[dataclass_field.name] = None
    return encoded


def _project_core_result(
    result: object,
    *,
    codec: PolicyCursorCodec | None,
) -> object:
    """Keep Core cursors opaque while preserving its immutable projections."""

    page = getattr(result, "page", None)
    if page is None:
        return result
    if codec is None:  # pragma: no cover - facade invariant
        raise RuntimeError("guideline_policy_cursor_codec_missing")
    next_cursor = getattr(page, "next_cursor", None)
    return {
        "items": _jsonable_page_items(page.items),
        "limit": page.limit,
        "has_more": page.has_more,
        "next_cursor": codec.encode(next_cursor) if next_cursor is not None else None,
    }


async def _execute(
    facade: PolicyGovernanceFacade,
    operation: str,
    values: dict[str, Any],
    *,
    principal: Principal,
    board_id: str,
    uow: PulseUnitOfWork,
) -> object:
    try:
        result = await facade.execute(
            operation,
            values,
            actor=_actor(principal, board_id=board_id),
            uow=uow,
        )
    except Exception as exc:
        raise _temporary_http_error(exc) from exc
    return _wire_result(result)


# Literal governance routes are intentionally registered first.


@router.get(
    "/boards/{board_id}/guidelines/export",
    response_model=GuidelineExportV2Request,
)
async def export_guideline_policy_v2(
    board_id: BoardId,
    guideline_ids: list[GuidelineId] | None = Query(default=None),
    include_binding_history: bool = Query(default=True),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    from okto_pulse.core.application.use_cases.guideline_import_export import (
        ExportGuidelinePolicyCommand,
        ExportGuidelinePolicyV2UseCase,
    )

    try:
        result = await ExportGuidelinePolicyV2UseCase().execute(
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
async def import_guideline_policy_v2(
    board_id: BoardId,
    envelope: GuidelineExportV2Request,
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
    "/boards/{board_id}/policy-compliance/evaluations",
    status_code=status.HTTP_201_CREATED,
    response_model=EvaluationResponse,
)
async def evaluate_policy_compliance(
    board_id: BoardId,
    data: EvaluatePolicyComplianceRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "evaluate_compliance",
        {"board_id": board_id, **data.model_dump(mode="python")},
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-compliance/receipts",
    response_model=PolicyComplianceReceiptPageResponse,
    response_model_exclude_unset=True,
)
async def list_policy_compliance_receipts(
    board_id: BoardId,
    limit: int = Query(POLICY_PAGE_LIMIT_DEFAULT, ge=1, le=POLICY_PAGE_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    entity_type: PolicyEntityType | None = Query(default=None),
    subject_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    outcome: PolicyEvaluationOutcome | None = Query(default=None),
    currentness: PolicyCurrentness | None = Query(default=None),
    projection: PolicyProjection = Query(PolicyProjection.SUMMARY),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_compliance_receipts",
        {
            "board_id": board_id,
            "limit": limit,
            "cursor": cursor,
            "entity_type": entity_type.value if entity_type else None,
            "subject_id": subject_id,
            "outcome": outcome.value if outcome else None,
            "currentness": currentness.value if currentness else None,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-compliance/receipts/current",
    response_model=PolicyComplianceReceiptResponse,
)
async def get_current_policy_compliance_receipt(
    board_id: BoardId,
    entity_type: PolicyEntityType = Query(...),
    subject_id: str = Query(
        ...,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_current_compliance",
        {
            "board_id": board_id,
            "entity_type": entity_type.value,
            "subject_id": subject_id,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-compliance/receipts/{receipt_id}",
    response_model=PolicyComplianceReceiptResponse,
)
async def get_policy_compliance_receipt(
    board_id: BoardId,
    receipt_id: ComplianceReceiptId,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_compliance_receipt",
        {"board_id": board_id, "receipt_id": receipt_id},
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-compliance/findings",
    response_model=PolicyComplianceFindingPageResponse,
    response_model_exclude_unset=True,
)
async def list_policy_compliance_findings(
    board_id: BoardId,
    limit: int = Query(POLICY_PAGE_LIMIT_DEFAULT, ge=1, le=POLICY_PAGE_LIMIT_MAX),
    cursor: str | None = Query(default=None),
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
    rule_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_RULE_ID_MAX_LENGTH,
    ),
    subject_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    outcome: PolicyEvaluationOutcome | None = Query(default=None),
    projection: PolicyProjection = Query(PolicyProjection.SUMMARY),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_compliance_findings",
        {
            "board_id": board_id,
            "limit": limit,
            "cursor": cursor,
            "receipt_id": receipt_id,
            "guideline_id": guideline_id,
            "rule_id": rule_id,
            "subject_id": subject_id,
            "outcome": outcome.value if outcome else None,
            "projection": projection.value,
        },
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-waivers",
    response_model=PolicyWaiverPageResponse,
    response_model_exclude_unset=True,
)
async def list_policy_waivers(
    board_id: BoardId,
    evaluated_at: datetime = Query(...),
    limit: int = Query(POLICY_PAGE_LIMIT_DEFAULT, ge=1, le=POLICY_PAGE_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    finding_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_FINDING_ID_MAX_LENGTH,
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
    revision_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=GUIDELINE_REVISION_ID_MAX_LENGTH,
    ),
    rule_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_RULE_ID_MAX_LENGTH,
    ),
    entity_type: PolicyEntityType | None = Query(default=None),
    subject_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=POLICY_SUBJECT_ID_MAX_LENGTH,
    ),
    subject_version: int | None = Query(
        default=None,
        ge=1,
        le=POLICY_SQL_INTEGER_MAX,
    ),
    waiver_status: PolicyWaiverStatus | None = Query(default=None, alias="status"),
    projection: PolicyProjection = Query(PolicyProjection.SUMMARY),
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_waivers",
        {
            "board_id": board_id,
            "evaluated_at": evaluated_at,
            "limit": limit,
            "cursor": cursor,
            "finding_id": finding_id,
            "receipt_id": receipt_id,
            "guideline_id": guideline_id,
            "revision_id": revision_id,
            "rule_id": rule_id,
            "entity_type": entity_type.value if entity_type else None,
            "subject_id": subject_id,
            "subject_version": subject_version,
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
    response_model=WaiverMutationResponse,
)
async def request_policy_waiver(
    board_id: BoardId,
    data: RequestPolicyWaiverRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "request_waiver",
        {"board_id": board_id, **data.model_dump(mode="python")},
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.get(
    "/boards/{board_id}/policy-waivers/{waiver_id}/events",
    response_model=WaiverEventsResponse,
)
async def list_policy_waiver_events(
    board_id: BoardId,
    waiver_id: WaiverId,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "list_waiver_events",
        {"board_id": board_id, "waiver_id": waiver_id},
        principal=principal,
        board_id=board_id,
        uow=uow,
    )


@router.post(
    "/boards/{board_id}/policy-waivers/{waiver_id}/review",
    response_model=WaiverMutationResponse,
)
async def review_policy_waiver(
    board_id: BoardId,
    waiver_id: WaiverId,
    data: ReviewPolicyWaiverRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "review_waiver",
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
    response_model=WaiverMutationResponse,
)
async def revoke_policy_waiver(
    board_id: BoardId,
    waiver_id: WaiverId,
    data: RevokePolicyWaiverRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "revoke_waiver",
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
    response_model=WaiverMutationResponse,
)
async def revalidate_policy_waiver(
    board_id: BoardId,
    waiver_id: WaiverId,
    data: RevalidatePolicyWaiverRequest,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "revalidate_waiver",
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
    response_model=WaiverResponse,
)
async def get_policy_waiver(
    board_id: BoardId,
    waiver_id: WaiverId,
    principal: Principal = Depends(require_principal),
    facade: PolicyGovernanceFacade = Depends(get_policy_governance_facade),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _execute(
        facade,
        "get_waiver",
        {"board_id": board_id, "waiver_id": waiver_id},
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
