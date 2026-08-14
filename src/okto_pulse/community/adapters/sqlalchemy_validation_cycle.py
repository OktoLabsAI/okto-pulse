"""Lifecycle-edition validation-cycle read model for Community."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    String,
    cast,
    event,
    false,
    func,
    literal,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    ChecklistExecutionHeadRow,
    ChecklistItemResultRow,
    ChecklistReceiptRow,
    ChecklistValidationBindingSnapshotRow,
    Ideation,
    GuidelineBoardBindingRow,
    GuidelineRevisionRow,
    QualityAssessmentHeadRow,
    QualityAssessmentLifecycleTransitionRow,
    QualityAssessmentReceiptRow,
    Refinement,
    SemanticGuidelineAssessmentV2Row,
    SemanticGuidelineAssessmentReceiptRow,
    SemanticGuidelineBindingConfigurationRow,
    SemanticGuidelineMetricResultRow,
    SemanticGuidelineMetricResultV2Row,
    SemanticGuidelineRevisionRow,
    SemanticGuidelineSkipRow,
    SemanticGuidelineValidationScopeRow,
    SemanticGuidelineWaiverEventRow,
    SemanticGuidelineWaiverRow,
    Spec,
)
from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
    _quality_actor_permissions,
)
from okto_pulse.core.domain.quality_assessment import (
    AssessmentKind,
    AssessmentSubjectType,
)
from okto_pulse.core.domain.human_validation_cycle import is_current_edition
from okto_pulse.core.domain.realm import LOCAL_REALM_ID, RealmScope
from okto_pulse.core.domain.validation_cycle import (
    ValidationCycleCheckSummary,
    ValidationCycleResultSummary,
    ValidationCycleResultType,
    ValidationCycleState,
    ValidationCycleSubjectRef,
    ValidationCycleSummary,
    ValidationEditionExceptionAudit,
    ValidationEditionExceptionType,
    ValidationSubmissionFence,
    ValidationTechnicalAudit,
    ValidationTechnicalAuditDetails,
)
from okto_pulse.core.ports.validation_cycle import (
    ValidationCycleReadAccessDenied,
    ValidationCycleResultNotFound,
    ValidationCycleSubjectNotFound,
)
from okto_pulse.core.services.ska_observability import (
    observe_validation_cycle_summary_selects,
)
from okto_pulse.core.services.ambiguity_assessment import (
    AmbiguityGateReason,
    resolve_ambiguity_gate_configuration,
)


_SUBJECT_MODEL = {
    AssessmentSubjectType.IDEATION: Ideation,
    AssessmentSubjectType.REFINEMENT: Refinement,
    AssessmentSubjectType.SPEC: Spec,
}
_VALIDATION_STATUS = {
    AssessmentSubjectType.IDEATION: "evaluating",
    AssessmentSubjectType.REFINEMENT: "approved",
    AssessmentSubjectType.SPEC: "approved",
}

_SPEC_SECTION_PERMISSIONS = (
    (ValidationCycleResultType.SPEC_VALIDATION, "spec.validation.read"),
    (ValidationCycleResultType.REQUIREMENT_LINT, "spec.quality.read"),
    (ValidationCycleResultType.CURATED_CHECKLIST, "spec.checklist.read"),
    (
        ValidationCycleResultType.POLICY_COMPLIANCE,
        "guidelines.assessments.read",
    ),
)


@dataclass(frozen=True, slots=True)
class _ValidationCycleAccess:
    permissions: object
    visible_sections: tuple[ValidationCycleResultType, ...]

    def can(self, section: ValidationCycleResultType) -> bool:
        return section in self.visible_sections

    def has(self, permission: str) -> bool:
        checker = getattr(self.permissions, "has", None)
        return bool(callable(checker) and checker(permission))

    @property
    def visible_exception_types(
        self,
    ) -> tuple[ValidationEditionExceptionType, ...]:
        items: list[ValidationEditionExceptionType] = []
        if self.can(ValidationCycleResultType.AMBIGUITY_ASSESSMENT):
            items.append(ValidationEditionExceptionType.AMBIGUITY_GATE_SKIP)
        if self.can(ValidationCycleResultType.POLICY_COMPLIANCE):
            items.append(ValidationEditionExceptionType.POLICY_SKIP)
        if self.has("guidelines.waiver.read"):
            items.append(ValidationEditionExceptionType.POLICY_WAIVER)
        return tuple(items)


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_POLICY_DETAILS_LIMIT = 100


@dataclass(frozen=True, slots=True)
class _PolicyScopeItem:
    board_id: str
    subject_id: str
    subject_edition: int
    binding_id: str
    binding_revision: int
    guideline_id: str
    revision_id: str
    revision_digest: str
    configuration_digest: str
    state: str
    enforcement: str

    @property
    def authority_key(self) -> tuple[str, str, int, str, str, str, str]:
        return (
            self.board_id,
            self.binding_id,
            self.binding_revision,
            self.guideline_id,
            self.revision_id,
            self.revision_digest,
            self.configuration_digest,
        )

    @property
    def evidence_key(self) -> tuple[str, str, int, str, str, str, str, str, int]:
        return (
            *self.authority_key,
            self.subject_id,
            self.subject_edition,
        )


@dataclass(frozen=True, slots=True)
class _PolicyAuthority:
    title: str
    state: str
    enforcement: str
    minimum_confidence: int
    metrics: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class _PolicyReceiptEvidence:
    contract: str
    recorded_at: datetime
    stable_id: str
    outcome: str
    metric_count: int
    failed_metric_count: int
    metric_results: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _PolicySnapshotData:
    authorities: Mapping[
        tuple[str, str, int, str, str, str, str],
        _PolicyAuthority,
    ]
    receipts: Mapping[
        tuple[str, str, int, str, str, str, str, str, int],
        tuple[_PolicyReceiptEvidence, ...],
    ]
    skipped: frozenset[tuple[str, str, int, str, str, str, str, str, int]]
    inconsistent_authorities: frozenset[tuple[str, str, int, str, str, str, str]]
    inconsistent_evidence: frozenset[tuple[str, str, int, str, str, str, str, str, int]]
    waived_metrics: frozenset[
        tuple[
            tuple[str, str, int, str, str, str, str, str, int],
            str,
            str,
        ]
    ]
    scope_required: frozenset[tuple[str, str, int]]


def _policy_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _policy_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized


def _policy_json(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _policy_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def _bounded_policy_detail(value: str) -> tuple[str, bool]:
    """Bound human policy copy to Core's summary envelope without read failure."""

    if len(value) <= 4096:
        return value, False
    return value[:4095].rstrip() + "…", True


def _policy_scope_items(
    scope: SemanticGuidelineValidationScopeRow | None,
) -> tuple[tuple[_PolicyScopeItem, ...], int]:
    """Parse one frozen scope strictly, returning an inconsistency counter."""

    if scope is None:
        return (), 0
    raw_scope = scope.scope_json
    if not isinstance(raw_scope, list) or len(raw_scope) > _POLICY_DETAILS_LIMIT:
        return (), 1
    items: list[_PolicyScopeItem] = []
    seen: set[tuple[str, int]] = set()
    for raw in raw_scope:
        if not isinstance(raw, Mapping):
            return (), 1
        binding_id = raw.get("binding_id")
        binding_revision = raw.get("binding_revision")
        guideline_id = raw.get("guideline_id")
        revision_id = raw.get("revision_id")
        revision_digest = raw.get("revision_digest")
        configuration_digest = raw.get("configuration_digest")
        state = raw.get("state", "active")
        enforcement = raw.get("enforcement")
        if (
            not isinstance(binding_id, str)
            or not binding_id.strip()
            or isinstance(binding_revision, bool)
            or not isinstance(binding_revision, int)
            or binding_revision < 1
            or not isinstance(guideline_id, str)
            or not guideline_id.strip()
            or not isinstance(revision_id, str)
            or not revision_id.strip()
            or not isinstance(revision_digest, str)
            or len(revision_digest) != 64
            or not isinstance(configuration_digest, str)
            or len(configuration_digest) != 64
            or state not in {"active", "unlinked"}
            or enforcement not in {"advisory", "blocking"}
        ):
            return (), 1
        identity = (binding_id, binding_revision)
        if identity in seen:
            return (), 1
        seen.add(identity)
        items.append(
            _PolicyScopeItem(
                board_id=str(scope.board_id),
                subject_id=str(scope.subject_id),
                subject_edition=int(scope.validation_edition),
                binding_id=binding_id,
                binding_revision=binding_revision,
                guideline_id=guideline_id,
                revision_id=revision_id,
                revision_digest=revision_digest,
                configuration_digest=configuration_digest,
                state=state,
                enforcement=enforcement,
            )
        )
    return tuple(items), 0


def _policy_metric_details(
    raw_metrics: object,
    raw_overrides: object,
) -> tuple[tuple[dict[str, object], ...] | None, bool]:
    """Resolve effective Spec metric definitions; ``None`` is corrupt."""

    metrics = _policy_json(raw_metrics)
    overrides = _policy_json(raw_overrides)
    if (
        not isinstance(metrics, list)
        or len(metrics) > _POLICY_DETAILS_LIMIT
        or not isinstance(overrides, Mapping)
        or len(overrides) > _POLICY_DETAILS_LIMIT
    ):
        return None, False
    resolved: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_codes: set[str] = set()
    for raw in metrics:
        if not isinstance(raw, Mapping):
            return None, False
        metric_id = raw.get("metric_id")
        code = raw.get("code")
        title = raw.get("title")
        description = raw.get("description")
        rubric = raw.get("evaluation_rubric")
        targets = raw.get("target_entity_types")
        direction = raw.get("direction")
        default_threshold = raw.get("default_threshold")
        if (
            not isinstance(metric_id, str)
            or not metric_id.strip()
            or not isinstance(code, str)
            or not code.strip()
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(description, str)
            or not description.strip()
            or not isinstance(rubric, str)
            or not rubric.strip()
            or not isinstance(targets, list)
            or not targets
            or any(not isinstance(target, str) for target in targets)
            or direction not in {"minimum", "maximum"}
            or isinstance(default_threshold, bool)
            or not isinstance(default_threshold, int)
            or not 0 <= default_threshold <= 100
            or metric_id in seen_ids
            or code.casefold() in seen_codes
        ):
            return None, False
        seen_ids.add(metric_id)
        seen_codes.add(code.casefold())
        if "spec" not in targets:
            continue
        bounded_description, description_truncated = _bounded_policy_detail(description)
        bounded_rubric, rubric_truncated = _bounded_policy_detail(rubric)
        override = overrides.get(code)
        if override is not None and (
            isinstance(override, bool)
            or not isinstance(override, int)
            or not 0 <= override <= 100
        ):
            return None, False
        resolved.append(
            {
                "metric_id": metric_id,
                "code": code,
                "title": title,
                "description": bounded_description,
                "description_truncated": description_truncated,
                "evaluation_rubric": bounded_rubric,
                "evaluation_rubric_truncated": rubric_truncated,
                "direction": direction,
                "default_threshold": default_threshold,
                "effective_threshold": (
                    default_threshold if override is None else override
                ),
                "threshold_source": "default" if override is None else "override",
            }
        )
    unknown_overrides = set(overrides).difference(
        raw.get("code") for raw in metrics if isinstance(raw, Mapping)
    )
    if unknown_overrides:
        return None, False
    return tuple(resolved), not resolved


def _policy_union_statement(
    *,
    board_ids: tuple[str, ...],
    subject_ids: tuple[str, ...],
    binding_ids: tuple[str, ...],
):
    """Build one cross-dialect SELECT for authority, v1/v2 evidence and skips."""

    def value(column: object, name: str):
        return cast(column, String).label(name)

    def empty(name: str):
        return literal(None, type_=String).label(name)

    names = (
        "kind",
        "board_id",
        "subject_id",
        "binding_id",
        "binding_revision",
        "guideline_id",
        "revision_id",
        "revision_digest",
        "configuration_digest",
        "text_a",
        "text_b",
        "text_c",
        "text_d",
        "text_e",
        "json_a",
        "json_b",
        "int_a",
        "int_b",
        "int_c",
        "time_a",
        "id_a",
    )

    authority_filter = (
        SemanticGuidelineBindingConfigurationRow.board_id.in_(board_ids)
        & SemanticGuidelineBindingConfigurationRow.binding_id.in_(binding_ids)
        if board_ids and binding_ids
        else false()
    )
    authority = (
        select(
            literal("authority").label("kind"),
            value(SemanticGuidelineBindingConfigurationRow.board_id, "board_id"),
            empty("subject_id"),
            value(SemanticGuidelineBindingConfigurationRow.binding_id, "binding_id"),
            value(
                SemanticGuidelineBindingConfigurationRow.binding_revision,
                "binding_revision",
            ),
            value(
                SemanticGuidelineBindingConfigurationRow.guideline_id,
                "guideline_id",
            ),
            value(
                SemanticGuidelineBindingConfigurationRow.revision_id,
                "revision_id",
            ),
            value(
                SemanticGuidelineBindingConfigurationRow.revision_digest,
                "revision_digest",
            ),
            value(
                SemanticGuidelineBindingConfigurationRow.configuration_digest,
                "configuration_digest",
            ),
            value(GuidelineRevisionRow.title, "text_a"),
            value(GuidelineBoardBindingRow.state, "text_b"),
            value(GuidelineBoardBindingRow.enforcement, "text_c"),
            value(SemanticGuidelineBindingConfigurationRow.enforcement, "text_d"),
            value(SemanticGuidelineRevisionRow.authority_state, "text_e"),
            value(SemanticGuidelineRevisionRow.metrics, "json_a"),
            value(
                SemanticGuidelineBindingConfigurationRow.metric_threshold_overrides,
                "json_b",
            ),
            value(
                SemanticGuidelineBindingConfigurationRow.minimum_confidence,
                "int_a",
            ),
            empty("int_b"),
            empty("int_c"),
            empty("time_a"),
            empty("id_a"),
        )
        .select_from(SemanticGuidelineBindingConfigurationRow)
        .join(
            GuidelineBoardBindingRow,
            (
                GuidelineBoardBindingRow.board_id
                == SemanticGuidelineBindingConfigurationRow.board_id
            )
            & (
                GuidelineBoardBindingRow.binding_id
                == SemanticGuidelineBindingConfigurationRow.binding_id
            )
            & (
                GuidelineBoardBindingRow.binding_revision
                == SemanticGuidelineBindingConfigurationRow.binding_revision
            )
            & (
                GuidelineBoardBindingRow.guideline_id
                == SemanticGuidelineBindingConfigurationRow.guideline_id
            )
            & (
                GuidelineBoardBindingRow.revision_id
                == SemanticGuidelineBindingConfigurationRow.revision_id
            ),
        )
        .join(
            SemanticGuidelineRevisionRow,
            (
                SemanticGuidelineRevisionRow.guideline_id
                == SemanticGuidelineBindingConfigurationRow.guideline_id
            )
            & (
                SemanticGuidelineRevisionRow.revision_id
                == SemanticGuidelineBindingConfigurationRow.revision_id
            )
            & (
                SemanticGuidelineRevisionRow.revision_digest
                == SemanticGuidelineBindingConfigurationRow.revision_digest
            ),
        )
        .join(
            GuidelineRevisionRow,
            (GuidelineRevisionRow.guideline_id == GuidelineBoardBindingRow.guideline_id)
            & (
                GuidelineRevisionRow.revision_id == GuidelineBoardBindingRow.revision_id
            ),
        )
        .where(authority_filter)
    )

    evidence_filter_v1 = (
        SemanticGuidelineAssessmentReceiptRow.board_id.in_(board_ids)
        & SemanticGuidelineAssessmentReceiptRow.subject_id.in_(subject_ids)
        & SemanticGuidelineAssessmentReceiptRow.binding_id.in_(binding_ids)
        if board_ids and subject_ids and binding_ids
        else false()
    )
    v1 = (
        select(
            literal("v1").label("kind"),
            value(SemanticGuidelineAssessmentReceiptRow.board_id, "board_id"),
            value(SemanticGuidelineAssessmentReceiptRow.subject_id, "subject_id"),
            value(SemanticGuidelineAssessmentReceiptRow.binding_id, "binding_id"),
            value(
                SemanticGuidelineAssessmentReceiptRow.binding_revision,
                "binding_revision",
            ),
            value(
                SemanticGuidelineAssessmentReceiptRow.guideline_id,
                "guideline_id",
            ),
            value(SemanticGuidelineAssessmentReceiptRow.revision_id, "revision_id"),
            value(
                SemanticGuidelineAssessmentReceiptRow.revision_digest,
                "revision_digest",
            ),
            value(
                SemanticGuidelineAssessmentReceiptRow.configuration_digest,
                "configuration_digest",
            ),
            value(SemanticGuidelineAssessmentReceiptRow.state, "text_a"),
            value(SemanticGuidelineMetricResultRow.metric_id, "text_b"),
            value(SemanticGuidelineMetricResultRow.outcome, "text_c"),
            value(SemanticGuidelineMetricResultRow.metric_code, "text_d"),
            empty("text_e"),
            empty("json_a"),
            empty("json_b"),
            value(
                SemanticGuidelineAssessmentReceiptRow.metric_result_count,
                "int_a",
            ),
            value(
                SemanticGuidelineAssessmentReceiptRow.failed_metric_count,
                "int_b",
            ),
            value(
                SemanticGuidelineAssessmentReceiptRow.validation_edition,
                "int_c",
            ),
            value(SemanticGuidelineAssessmentReceiptRow.assessed_at, "time_a"),
            value(SemanticGuidelineAssessmentReceiptRow.receipt_id, "id_a"),
        )
        .select_from(SemanticGuidelineAssessmentReceiptRow)
        .outerjoin(
            SemanticGuidelineMetricResultRow,
            SemanticGuidelineMetricResultRow.receipt_id
            == SemanticGuidelineAssessmentReceiptRow.receipt_id,
        )
        .where(
            SemanticGuidelineAssessmentReceiptRow.subject_type == "spec",
            SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
            evidence_filter_v1,
        )
    )

    evidence_filter_v2 = (
        SemanticGuidelineAssessmentV2Row.board_id.in_(board_ids)
        & SemanticGuidelineAssessmentV2Row.subject_id.in_(subject_ids)
        & SemanticGuidelineAssessmentV2Row.binding_id.in_(binding_ids)
        if board_ids and subject_ids and binding_ids
        else false()
    )
    v2 = (
        select(
            literal("v2").label("kind"),
            value(SemanticGuidelineAssessmentV2Row.board_id, "board_id"),
            value(SemanticGuidelineAssessmentV2Row.subject_id, "subject_id"),
            value(SemanticGuidelineAssessmentV2Row.binding_id, "binding_id"),
            value(
                SemanticGuidelineAssessmentV2Row.binding_revision, "binding_revision"
            ),
            value(SemanticGuidelineAssessmentV2Row.guideline_id, "guideline_id"),
            value(SemanticGuidelineAssessmentV2Row.revision_id, "revision_id"),
            value(SemanticGuidelineAssessmentV2Row.revision_digest, "revision_digest"),
            value(
                SemanticGuidelineAssessmentV2Row.configuration_digest,
                "configuration_digest",
            ),
            empty("text_a"),
            value(SemanticGuidelineMetricResultV2Row.metric_id, "text_b"),
            value(SemanticGuidelineMetricResultV2Row.outcome, "text_c"),
            empty("text_d"),
            empty("text_e"),
            empty("json_a"),
            empty("json_b"),
            empty("int_a"),
            empty("int_b"),
            value(SemanticGuidelineAssessmentV2Row.validation_edition, "int_c"),
            value(SemanticGuidelineAssessmentV2Row.recorded_at, "time_a"),
            value(SemanticGuidelineAssessmentV2Row.receipt_id, "id_a"),
        )
        .select_from(SemanticGuidelineAssessmentV2Row)
        .outerjoin(
            SemanticGuidelineMetricResultV2Row,
            SemanticGuidelineMetricResultV2Row.receipt_id
            == SemanticGuidelineAssessmentV2Row.receipt_id,
        )
        .where(
            SemanticGuidelineAssessmentV2Row.subject_type == "spec",
            evidence_filter_v2,
        )
    )

    skip_filter = (
        SemanticGuidelineSkipRow.board_id.in_(board_ids)
        & SemanticGuidelineSkipRow.subject_id.in_(subject_ids)
        & SemanticGuidelineSkipRow.binding_id.in_(binding_ids)
        if board_ids and subject_ids and binding_ids
        else false()
    )
    skips = select(
        literal("skip").label("kind"),
        value(SemanticGuidelineSkipRow.board_id, "board_id"),
        value(SemanticGuidelineSkipRow.subject_id, "subject_id"),
        value(SemanticGuidelineSkipRow.binding_id, "binding_id"),
        value(SemanticGuidelineSkipRow.binding_revision, "binding_revision"),
        value(SemanticGuidelineSkipRow.guideline_id, "guideline_id"),
        value(SemanticGuidelineSkipRow.revision_id, "revision_id"),
        value(SemanticGuidelineSkipRow.revision_digest, "revision_digest"),
        value(SemanticGuidelineSkipRow.configuration_digest, "configuration_digest"),
        value(SemanticGuidelineSkipRow.status, "text_a"),
        empty("text_b"),
        empty("text_c"),
        empty("text_d"),
        empty("text_e"),
        empty("json_a"),
        empty("json_b"),
        value(SemanticGuidelineSkipRow.skip_revision, "int_a"),
        empty("int_b"),
        value(SemanticGuidelineSkipRow.validation_edition, "int_c"),
        value(SemanticGuidelineSkipRow.occurred_at, "time_a"),
        value(SemanticGuidelineSkipRow.skip_id, "id_a"),
    ).where(
        SemanticGuidelineSkipRow.subject_type == "spec",
        skip_filter,
    )
    waiver_filter = (
        SemanticGuidelineWaiverRow.board_id.in_(board_ids)
        & SemanticGuidelineWaiverRow.subject_id.in_(subject_ids)
        & SemanticGuidelineWaiverRow.binding_id.in_(binding_ids)
        if board_ids and subject_ids and binding_ids
        else false()
    )
    waivers = select(
        literal("waiver").label("kind"),
        value(SemanticGuidelineWaiverRow.board_id, "board_id"),
        value(SemanticGuidelineWaiverRow.subject_id, "subject_id"),
        value(SemanticGuidelineWaiverRow.binding_id, "binding_id"),
        value(SemanticGuidelineWaiverRow.binding_revision, "binding_revision"),
        value(SemanticGuidelineWaiverRow.guideline_id, "guideline_id"),
        value(SemanticGuidelineWaiverRow.revision_id, "revision_id"),
        value(SemanticGuidelineWaiverRow.revision_digest, "revision_digest"),
        value(
            SemanticGuidelineWaiverRow.configuration_digest,
            "configuration_digest",
        ),
        value(SemanticGuidelineWaiverRow.status, "text_a"),
        value(SemanticGuidelineWaiverRow.metric_id, "text_b"),
        empty("text_c"),
        empty("text_d"),
        value(SemanticGuidelineWaiverRow.receipt_id, "text_e"),
        empty("json_a"),
        empty("json_b"),
        value(SemanticGuidelineWaiverRow.waiver_revision, "int_a"),
        empty("int_b"),
        value(SemanticGuidelineWaiverRow.validation_edition, "int_c"),
        value(SemanticGuidelineWaiverRow.expires_at, "time_a"),
        value(SemanticGuidelineWaiverRow.waiver_id, "id_a"),
    ).where(
        SemanticGuidelineWaiverRow.subject_type == "spec",
        SemanticGuidelineWaiverRow.status == "approved",
        waiver_filter,
    )
    scope_required_filter = (
        QualityAssessmentLifecycleTransitionRow.board_id.in_(board_ids)
        & QualityAssessmentLifecycleTransitionRow.subject_id.in_(subject_ids)
        if board_ids and subject_ids
        else false()
    )
    scope_required = select(
        literal("scope_required").label("kind"),
        value(QualityAssessmentLifecycleTransitionRow.board_id, "board_id"),
        value(QualityAssessmentLifecycleTransitionRow.subject_id, "subject_id"),
        empty("binding_id"),
        empty("binding_revision"),
        empty("guideline_id"),
        empty("revision_id"),
        empty("revision_digest"),
        empty("configuration_digest"),
        empty("text_a"),
        empty("text_b"),
        empty("text_c"),
        empty("text_d"),
        empty("text_e"),
        empty("json_a"),
        empty("json_b"),
        empty("int_a"),
        empty("int_b"),
        value(QualityAssessmentLifecycleTransitionRow.after_edition, "int_c"),
        empty("time_a"),
        empty("id_a"),
    ).where(
        QualityAssessmentLifecycleTransitionRow.subject_type == "spec",
        QualityAssessmentLifecycleTransitionRow.action == "admit_validation",
        QualityAssessmentLifecycleTransitionRow.after_status == "approved",
        QualityAssessmentLifecycleTransitionRow.after_edition.is_not(None),
        scope_required_filter,
    )
    statement = union_all(authority, v1, v2, skips, waivers, scope_required)
    # Defensive proof that future edits keep the UNION column contract aligned.
    if tuple(statement.selected_columns.keys()) != names:  # pragma: no cover
        raise RuntimeError("policy_validation_cycle_union_shape_invalid")
    return statement


async def _load_policy_snapshot_data(
    session: AsyncSession,
    *,
    scope_items: tuple[_PolicyScopeItem, ...],
    board_ids: tuple[str, ...],
    subject_ids: tuple[str, ...],
) -> _PolicySnapshotData:
    """Load all exact policy authority/evidence in one bounded SELECT."""

    binding_ids = tuple(sorted({item.binding_id for item in scope_items}))
    rows = (
        await session.execute(
            _policy_union_statement(
                board_ids=board_ids,
                subject_ids=subject_ids,
                binding_ids=binding_ids,
            )
        )
    ).mappings()

    authorities: dict[tuple[str, str, int, str, str, str, str], _PolicyAuthority] = {}
    inconsistent_authorities: set[tuple[str, str, int, str, str, str, str]] = set()
    receipt_rows: dict[
        tuple[str, str, int, str, str, str, str, str, int],
        list[_PolicyReceiptEvidence],
    ] = {}
    v1_groups: dict[
        tuple[tuple[str, str, int, str, str, str, str, str, int], str],
        dict[str, object],
    ] = {}
    v2_groups: dict[
        tuple[tuple[str, str, int, str, str, str, str, str, int], str],
        dict[str, object],
    ] = {}
    skip_heads: dict[
        tuple[tuple[str, str, int, str, str, str, str, str, int], str],
        tuple[int, str],
    ] = {}
    inconsistent_evidence: set[tuple[str, str, int, str, str, str, str, str, int]] = (
        set()
    )
    active_waiver_ids: dict[
        tuple[
            tuple[str, str, int, str, str, str, str, str, int],
            str,
            str,
        ],
        set[str],
    ] = {}
    scope_required: set[tuple[str, str, int]] = set()
    evaluated_at = datetime.now(timezone.utc)

    for row in rows:
        kind = _policy_text(row["kind"])
        board_id = _policy_text(row["board_id"])
        if kind == "scope_required":
            subject_id = _policy_text(row["subject_id"])
            subject_edition = _policy_int(row["int_c"])
            if (
                board_id is not None
                and subject_id is not None
                and subject_edition is not None
                and subject_edition >= 1
            ):
                scope_required.add((board_id, subject_id, subject_edition))
            continue
        binding_id = _policy_text(row["binding_id"])
        binding_revision = _policy_int(row["binding_revision"])
        guideline_id = _policy_text(row["guideline_id"])
        revision_id = _policy_text(row["revision_id"])
        revision_digest = _policy_text(row["revision_digest"])
        configuration_digest = _policy_text(row["configuration_digest"])
        if (
            kind is None
            or board_id is None
            or binding_id is None
            or binding_revision is None
            or guideline_id is None
            or revision_id is None
            or revision_digest is None
            or configuration_digest is None
        ):
            continue
        authority_key = (
            board_id,
            binding_id,
            binding_revision,
            guideline_id,
            revision_id,
            revision_digest,
            configuration_digest,
        )
        if kind == "authority":
            title = _policy_text(row["text_a"])
            state = _policy_text(row["text_b"])
            legacy_enforcement = _policy_text(row["text_c"])
            enforcement = _policy_text(row["text_d"])
            authority_state = _policy_text(row["text_e"])
            minimum_confidence = _policy_int(row["int_a"])
            metrics, context_only = _policy_metric_details(row["json_a"], row["json_b"])
            if (
                title is None
                or state not in {"active", "unlinked"}
                or enforcement not in {"advisory", "blocking"}
                or legacy_enforcement != enforcement
                or minimum_confidence is None
                or not 0 <= minimum_confidence <= 100
                or metrics is None
                or (authority_state != "native" and not context_only)
            ):
                inconsistent_authorities.add(authority_key)
                continue
            authority = _PolicyAuthority(
                title=title,
                state=state,
                enforcement=enforcement,
                minimum_confidence=minimum_confidence,
                metrics=metrics,
            )
            existing = authorities.get(authority_key)
            if existing is not None and existing != authority:
                inconsistent_authorities.add(authority_key)
                authorities.pop(authority_key, None)
            elif authority_key not in inconsistent_authorities:
                authorities[authority_key] = authority
            continue

        subject_id = _policy_text(row["subject_id"])
        subject_edition = _policy_int(row["int_c"])
        if subject_id is None or subject_edition is None:
            continue
        evidence_key = (*authority_key, subject_id, subject_edition)
        stable_id = _policy_text(row["id_a"])
        if stable_id is None:
            inconsistent_evidence.add(evidence_key)
            continue
        if kind == "v1":
            recorded_at = _policy_time(row["time_a"])
            state = _policy_text(row["text_a"])
            metric_count = _policy_int(row["int_a"])
            failed_count = _policy_int(row["int_b"])
            if (
                recorded_at is None
                or state not in {"passed", "metric_threshold_failed"}
                or metric_count is None
                or failed_count is None
                or metric_count < 0
                or failed_count < 0
                or failed_count > metric_count
                or (state == "passed" and failed_count != 0)
                or (state == "metric_threshold_failed" and failed_count == 0)
            ):
                inconsistent_evidence.add(evidence_key)
                continue
            group = v1_groups.setdefault(
                (evidence_key, stable_id),
                {
                    "recorded_at": recorded_at,
                    "state": state,
                    "metric_count": metric_count,
                    "failed_count": failed_count,
                    "metrics": {},
                },
            )
            if any(
                group[field_name] != expected
                for field_name, expected in (
                    ("recorded_at", recorded_at),
                    ("state", state),
                    ("metric_count", metric_count),
                    ("failed_count", failed_count),
                )
            ):
                inconsistent_evidence.add(evidence_key)
                continue
            metric_id = _policy_text(row["text_b"])
            outcome = _policy_text(row["text_c"])
            if metric_id is None and metric_count == 0:
                continue
            if metric_id is None or outcome not in {"pass", "fail"}:
                inconsistent_evidence.add(evidence_key)
                continue
            metric_results = group["metrics"]
            if not isinstance(metric_results, dict):  # pragma: no cover
                inconsistent_evidence.add(evidence_key)
                continue
            previous = metric_results.get(metric_id)
            if previous is not None and previous != outcome:
                inconsistent_evidence.add(evidence_key)
                continue
            metric_results[metric_id] = outcome
        elif kind == "v2":
            recorded_at = _policy_time(row["time_a"])
            if recorded_at is None:
                inconsistent_evidence.add(evidence_key)
                continue
            group = v2_groups.setdefault(
                (evidence_key, stable_id),
                {"recorded_at": recorded_at, "metrics": {}},
            )
            if group["recorded_at"] != recorded_at:
                inconsistent_evidence.add(evidence_key)
                continue
            metric_id = _policy_text(row["text_b"])
            outcome = _policy_text(row["text_c"])
            if metric_id is None or outcome not in {"pass", "fail"}:
                inconsistent_evidence.add(evidence_key)
                continue
            metric_results = group["metrics"]
            if not isinstance(metric_results, dict):  # pragma: no cover
                inconsistent_evidence.add(evidence_key)
                continue
            previous = metric_results.get(metric_id)
            if previous is not None and previous != outcome:
                inconsistent_evidence.add(evidence_key)
                continue
            metric_results[metric_id] = outcome
        elif kind == "skip":
            revision = _policy_int(row["int_a"])
            status = _policy_text(row["text_a"])
            if revision is None or revision < 1 or status not in {"active", "revoked"}:
                inconsistent_evidence.add(evidence_key)
                continue
            head_key = (evidence_key, stable_id)
            previous = skip_heads.get(head_key)
            if previous is None or revision > previous[0]:
                skip_heads[head_key] = (revision, status)
            elif revision == previous[0] and status != previous[1]:
                inconsistent_evidence.add(evidence_key)
        elif kind == "waiver":
            waiver_revision = _policy_int(row["int_a"])
            status = _policy_text(row["text_a"])
            metric_id = _policy_text(row["text_b"])
            receipt_id = _policy_text(row["text_e"])
            raw_expiry = row["time_a"]
            expires_at = _policy_time(raw_expiry)
            if (
                waiver_revision is None
                or waiver_revision < 2
                or status != "approved"
                or metric_id is None
                or receipt_id is None
                or (raw_expiry is not None and expires_at is None)
            ):
                inconsistent_evidence.add(evidence_key)
                continue
            if expires_at is not None and evaluated_at >= expires_at:
                continue
            active_waiver_ids.setdefault(
                (evidence_key, receipt_id, metric_id), set()
            ).add(stable_id)

    for (evidence_key, stable_id), group in v1_groups.items():
        metric_results = group["metrics"]
        recorded_at = group["recorded_at"]
        metric_count = group["metric_count"]
        failed_count = group["failed_count"]
        state = group["state"]
        if (
            evidence_key in inconsistent_evidence
            or not isinstance(metric_results, dict)
            or not isinstance(recorded_at, datetime)
            or not isinstance(metric_count, int)
            or not isinstance(failed_count, int)
            or not isinstance(state, str)
        ):
            continue
        normalized = tuple(
            sorted(
                (str(metric_id), str(outcome))
                for metric_id, outcome in metric_results.items()
            )
        )
        observed_failed = sum(
            1 for _metric_id, outcome in normalized if outcome == "fail"
        )
        if (
            len(normalized) != metric_count
            or observed_failed != failed_count
            or (state == "passed") != (observed_failed == 0)
        ):
            inconsistent_evidence.add(evidence_key)
            continue
        receipt_rows.setdefault(evidence_key, []).append(
            _PolicyReceiptEvidence(
                contract="v1",
                recorded_at=recorded_at,
                stable_id=stable_id,
                outcome="failed" if observed_failed else "passed",
                metric_count=metric_count,
                failed_metric_count=failed_count,
                metric_results=normalized,
            )
        )

    for (evidence_key, stable_id), group in v2_groups.items():
        metric_results = group["metrics"]
        recorded_at = group["recorded_at"]
        if (
            evidence_key in inconsistent_evidence
            or not isinstance(metric_results, dict)
            or not isinstance(recorded_at, datetime)
        ):
            continue
        normalized = tuple(
            sorted(
                (str(metric_id), str(outcome))
                for metric_id, outcome in metric_results.items()
            )
        )
        failed_count = sum(1 for _metric_id, outcome in normalized if outcome == "fail")
        receipt_rows.setdefault(evidence_key, []).append(
            _PolicyReceiptEvidence(
                contract="v2",
                recorded_at=recorded_at,
                stable_id=stable_id,
                outcome="failed" if failed_count else "passed",
                metric_count=len(normalized),
                failed_metric_count=failed_count,
                metric_results=normalized,
            )
        )

    active_skip_ids: dict[
        tuple[str, str, int, str, str, str, str, str, int], set[str]
    ] = {}
    for (evidence_key, skip_id), (_revision, status) in skip_heads.items():
        if status == "active":
            active_skip_ids.setdefault(evidence_key, set()).add(skip_id)
    for evidence_key, skip_ids in active_skip_ids.items():
        if len(skip_ids) > 1:
            inconsistent_evidence.add(evidence_key)
    active_skips = frozenset(
        evidence_key
        for evidence_key, skip_ids in active_skip_ids.items()
        if len(skip_ids) == 1
    )
    for (
        evidence_key,
        _receipt_id,
        _metric_id,
    ), waiver_ids in active_waiver_ids.items():
        if len(waiver_ids) > 1:
            inconsistent_evidence.add(evidence_key)
    active_waivers = frozenset(
        waiver_key
        for waiver_key, waiver_ids in active_waiver_ids.items()
        if len(waiver_ids) == 1
    )
    return _PolicySnapshotData(
        authorities=authorities,
        receipts={key: tuple(value) for key, value in receipt_rows.items()},
        skipped=active_skips,
        inconsistent_authorities=frozenset(inconsistent_authorities),
        inconsistent_evidence=frozenset(inconsistent_evidence),
        waived_metrics=active_waivers,
        scope_required=frozenset(scope_required),
    )


def _policy_check(
    *,
    scope_items: tuple[_PolicyScopeItem, ...],
    scope_inconsistent: int,
    scope_missing: bool,
    scope_required: bool,
    snapshot: _PolicySnapshotData,
) -> tuple[str, str, dict[str, object]]:
    """Project one human summary from the frozen scope and exact evidence.

    An edition admitted through the canonical lifecycle always freezes a scope,
    including an empty one.  Missing scope is therefore fail-closed for those
    editions.  Records without that durable lifecycle discriminator retain the
    legacy ``No applicable policies`` interpretation.
    """

    counts = {
        "applicable": 0,
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "waived": 0,
        "pending": 0,
        "context_only": 0,
        "inconsistent": 0,
        "scope_inconsistent": int(scope_inconsistent)
        + int(scope_missing and scope_required),
        "blocking": 0,
        "advisory": 0,
        "blocking_failed": 0,
        "blocking_pending": 0,
        "advisory_failed": 0,
        "advisory_pending": 0,
        "failed_metrics": 0,
        "waived_metrics": 0,
        "unwaived_failed_metrics": 0,
    }
    applicable_bindings: list[dict[str, object]] = []
    for item in scope_items:
        if item.state == "unlinked":
            continue
        authority = snapshot.authorities.get(item.authority_key)
        if (
            item.authority_key in snapshot.inconsistent_authorities
            or authority is None
            or authority.state != item.state
            or authority.enforcement != item.enforcement
        ):
            counts["scope_inconsistent"] += 1
            continue
        if not authority.metrics:
            counts["context_only"] += 1
            continue

        counts["applicable"] += 1
        counts[item.enforcement] += 1
        evidence_inconsistent = item.evidence_key in snapshot.inconsistent_evidence
        status = "pending"
        failed_metric_count = 0
        waived_metric_count = 0
        unwaived_failed_metric_count = 0
        metric_outcomes = {
            str(metric["metric_id"]): "pending" for metric in authority.metrics
        }
        if item.evidence_key in snapshot.skipped:
            status = "skipped"
        elif evidence_inconsistent:
            status = "inconsistent"
        else:
            candidates = snapshot.receipts.get(item.evidence_key, ())
            duplicate_ids = len(
                {candidate.stable_id for candidate in candidates}
            ) != len(candidates)
            if duplicate_ids:
                status = "inconsistent"
            elif candidates:
                latest = max(
                    candidates,
                    key=lambda candidate: (candidate.recorded_at, candidate.stable_id),
                )
                expected_metric_ids = {
                    str(metric["metric_id"]) for metric in authority.metrics
                }
                observed_metric_ids = {
                    metric_id for metric_id, _outcome in latest.metric_results
                }
                observed_failed = sum(
                    1
                    for _metric_id, outcome in latest.metric_results
                    if outcome == "fail"
                )
                if (
                    latest.metric_count != len(expected_metric_ids)
                    or observed_metric_ids != expected_metric_ids
                    or latest.failed_metric_count != observed_failed
                ):
                    status = "inconsistent"
                else:
                    waived_metric_ids = {
                        metric_id
                        for metric_id, outcome in latest.metric_results
                        if outcome == "fail"
                        and latest.contract == "v1"
                        and (
                            item.evidence_key,
                            latest.stable_id,
                            metric_id,
                        )
                        in snapshot.waived_metrics
                    }
                    failed_metric_count = observed_failed
                    waived_metric_count = len(waived_metric_ids)
                    unwaived_failed_metric_count = (
                        failed_metric_count - waived_metric_count
                    )
                    metric_outcomes = {
                        metric_id: (
                            "passed"
                            if outcome == "pass"
                            else (
                                "waived" if metric_id in waived_metric_ids else "failed"
                            )
                        )
                        for metric_id, outcome in latest.metric_results
                    }
                    if failed_metric_count == 0:
                        status = "passed"
                    elif unwaived_failed_metric_count == 0:
                        status = "waived"
                    else:
                        status = "failed"

        if status == "inconsistent":
            counts["inconsistent"] += 1
        elif status == "skipped":
            counts["skipped"] += 1
            counts["completed"] += 1
        elif status == "passed":
            counts["passed"] += 1
            counts["completed"] += 1
        elif status == "waived":
            counts["waived"] += 1
            counts["completed"] += 1
        elif status == "failed":
            counts["failed"] += 1
            counts["completed"] += 1
            counts[f"{item.enforcement}_failed"] += 1
        else:
            counts["pending"] += 1
            counts[f"{item.enforcement}_pending"] += 1
        counts["failed_metrics"] += failed_metric_count
        counts["waived_metrics"] += waived_metric_count
        counts["unwaived_failed_metrics"] += unwaived_failed_metric_count
        applicable_bindings.append(
            {
                "binding_id": item.binding_id,
                "guideline_id": item.guideline_id,
                "revision_id": item.revision_id,
                "title": authority.title,
                "enforcement": item.enforcement,
                "minimum_confidence": authority.minimum_confidence,
                "status": status,
                "failed_metric_count": failed_metric_count,
                "waived_metric_count": waived_metric_count,
                "unwaived_failed_metric_count": (unwaived_failed_metric_count),
                "metrics": [
                    {
                        **metric,
                        "assessment_outcome": metric_outcomes[str(metric["metric_id"])],
                    }
                    for metric in authority.metrics
                ],
            }
        )

    applicable = counts["applicable"]
    total_inconsistent = counts["inconsistent"] + counts["scope_inconsistent"]
    if total_inconsistent:
        status = "needs_attention"
        summary = (
            f"{total_inconsistent} policy scope item"
            f"{'s' if total_inconsistent != 1 else ''} could not be verified"
        )
    elif applicable == 0:
        status, summary = "off", "No applicable policies"
    elif counts["blocking_failed"]:
        status = "needs_attention"
        summary = (
            f"{counts['blocking_failed']} blocking polic"
            f"{'ies' if counts['blocking_failed'] != 1 else 'y'} failed"
        )
    elif counts["blocking_pending"]:
        status = "in_progress"
        summary = (
            f"{counts['completed']} of {applicable} applicable policies completed; "
            f"{counts['blocking_pending']} blocking pending"
        )
    elif counts["advisory_failed"] or counts["advisory_pending"]:
        status = "advisory"
        phrases = []
        if counts["advisory_pending"]:
            phrases.append(f"{counts['advisory_pending']} pending")
        if counts["advisory_failed"]:
            phrases.append(
                f"{counts['advisory_failed']} need"
                f"{'s' if counts['advisory_failed'] == 1 else ''} attention"
            )
        summary = "Non-blocking advisory coverage: " + ", ".join(phrases)
    elif counts["skipped"] or counts["waived"]:
        status = "passed"
        parts = [
            f"{counts[label]} {label}"
            for label in ("passed", "waived", "skipped")
            if counts[label]
        ]
        summary = ", ".join(parts)
    else:
        status = "passed"
        summary = f"All {applicable} applicable policies passed"
    return (
        status,
        summary,
        {
            "counts": counts,
            "applicable_bindings": applicable_bindings,
        },
    )


async def _count_session_selects(
    session: AsyncSession,
) -> tuple[list[int], object, object]:
    """Attach a connection-local SELECT counter for one summary read."""

    connection = await session.connection()
    sync_connection = connection.sync_connection
    counter = [0]

    def before_cursor_execute(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lstrip().upper()
        if normalized.startswith("SELECT") or normalized.startswith("WITH"):
            counter[0] += 1

    event.listen(sync_connection, "before_cursor_execute", before_cursor_execute)
    return counter, sync_connection, before_cursor_execute


def _quality_result(row: QualityAssessmentReceiptRow) -> ValidationCycleResultSummary:
    return ValidationCycleResultSummary(
        result_id=row.id,
        result_type=ValidationCycleResultType.AMBIGUITY_ASSESSMENT,
        subject_edition=(
            None if row.subject_edition is None else int(row.subject_edition)
        ),
        status="completed",
        summary={
            "score": row.score,
            "scale_minimum": row.scale_minimum,
            "scale_maximum": row.scale_maximum,
            "scale_direction": row.scale_direction,
            "justification": row.justification,
            "created_at": _aware(row.created_at).isoformat(),
        },
    )


def _current_quality_result(
    row: QualityAssessmentReceiptRow,
    *,
    subject_type: AssessmentSubjectType,
    subject: object,
) -> ValidationCycleResultSummary:
    """Project the human gate outcome for the subject's current edition."""

    configuration = resolve_ambiguity_gate_configuration(
        subject_type,
        getattr(getattr(subject, "board"), "settings", None),
    )
    skipped = bool(getattr(subject, "skip_ambiguity_gate", False)) and (
        is_current_edition(
            getattr(subject, "skip_ambiguity_gate_edition", None),
            getattr(subject, "edition", None),
        )
    )
    if not configuration.required:
        allowed = True
        reason = AmbiguityGateReason.DISABLED
        headline = "Ambiguity gate is disabled"
    elif skipped:
        allowed = True
        reason = AmbiguityGateReason.SKIPPED
        headline = "Ambiguity gate skipped by override"
    elif row.score > configuration.maximum_score:
        allowed = False
        reason = AmbiguityGateReason.SCORE_EXCEEDS_THRESHOLD
        headline = "Ambiguity exceeds the allowed limit"
    else:
        allowed = True
        reason = AmbiguityGateReason.READY
        headline = "Ambiguity within the allowed limit"

    historical = _quality_result(row)
    return ValidationCycleResultSummary(
        result_id=historical.result_id,
        result_type=historical.result_type,
        subject_edition=historical.subject_edition,
        status="passed" if allowed else "failed",
        summary={
            **historical.summary,
            "enabled": configuration.required,
            "allowed": allowed,
            "reason_code": reason.value,
            "threshold": configuration.maximum_score,
            "skipped": skipped,
            "headline": headline,
            "created_by": row.created_by,
        },
    )


def _spec_result(record: Mapping[str, Any]) -> ValidationCycleResultSummary:
    return ValidationCycleResultSummary(
        result_id=str(record["id"]),
        result_type=ValidationCycleResultType.SPEC_VALIDATION,
        subject_edition=(
            None if record.get("edition") is None else int(record["edition"])
        ),
        status=str(record.get("outcome") or "completed"),
        summary={
            key: record.get(key)
            for key in (
                "score",
                "summary",
                "completeness",
                "assertiveness",
                "ambiguity",
                "recommendation",
                "outcome",
                "general_justification",
                "created_at",
            )
            if record.get(key) is not None
        },
    )


def _current_spec_validation_record(subject: object) -> Mapping[str, Any] | None:
    """Resolve the pointer only when it names evidence for this edition."""

    pointer_id = getattr(subject, "current_validation_id", None)
    if not isinstance(pointer_id, str) or not pointer_id:
        return None
    edition = getattr(subject, "edition", None)
    return next(
        (
            item
            for item in (getattr(subject, "validations", None) or ())
            if isinstance(item, dict)
            and item.get("id") == pointer_id
            and is_current_edition(item.get("edition"), edition)
        ),
        None,
    )


def _spec_remaining_actions(
    *,
    lint_status: str | None,
    checklist_status: str | None,
    policy_status: str | None,
    has_current_validation: bool,
    validation_visible: bool = True,
) -> tuple[str, ...]:
    """Human actions derived only from sections visible to the caller."""

    actions: list[str] = []
    if lint_status == "not_started":
        actions.append("record_requirement_lint")
    if checklist_status is not None and checklist_status not in {"passed", "off"}:
        actions.append("complete_curated_checklist")
    if policy_status is not None and policy_status not in {
        "passed",
        "off",
        "advisory",
        "waived",
    }:
        actions.append("complete_policy_compliance")
    all_checks_visible = all(
        status is not None for status in (lint_status, checklist_status, policy_status)
    )
    if (
        not actions
        and validation_visible
        and all_checks_visible
        and not has_current_validation
    ):
        actions.append("submit_spec_validation")
    return tuple(actions)


class CommunitySqlAlchemyValidationCycleReader:
    """Read only human lifecycle state; technical evidence is lazy."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        if not callable(session_factory):
            raise TypeError("validation_cycle_session_factory_invalid")
        self._session_factory = session_factory

    @staticmethod
    def _require_local(realm_scope: RealmScope) -> None:
        if (
            not isinstance(realm_scope, RealmScope)
            or realm_scope.realm_id != LOCAL_REALM_ID
        ):
            raise ValidationCycleReadAccessDenied()

    async def _subject(
        self,
        session: AsyncSession,
        *,
        subject_type: AssessmentSubjectType,
        subject_id: str,
        actor_id: str,
    ) -> tuple[object, _ValidationCycleAccess]:
        model = _SUBJECT_MODEL[subject_type]
        subject = (
            await session.execute(
                select(model)
                .options(joinedload(model.board))
                .where(model.id == subject_id)
            )
        ).scalar_one_or_none()
        if subject is None:
            raise ValidationCycleSubjectNotFound()
        permissions, access_level = await _quality_actor_permissions(
            session,
            actor_id=actor_id,
            board_id=str(getattr(subject, "board_id")),
        )
        entity_read = f"{subject_type.value}.entity.read"
        if (
            permissions is None
            or access_level is None
            or not permissions.has(entity_read)
        ):
            # A caller without entity visibility must not be able to
            # distinguish an existing subject from a random identifier.
            raise ValidationCycleSubjectNotFound()
        if subject_type is AssessmentSubjectType.SPEC:
            visible_sections = tuple(
                section
                for section, permission in _SPEC_SECTION_PERMISSIONS
                if permissions.has(permission)
            )
        else:
            visible_sections = (
                (ValidationCycleResultType.AMBIGUITY_ASSESSMENT,)
                if permissions.has(f"{subject_type.value}.quality.read")
                else ()
            )
        if not visible_sections:
            raise ValidationCycleReadAccessDenied()
        return subject, _ValidationCycleAccess(permissions, visible_sections)

    async def get_validation_cycle(
        self,
        *,
        subject_type: AssessmentSubjectType,
        subject_id: str,
        include_previous: bool,
        offset: int,
        limit: int,
        actor_id: str,
        realm_scope: RealmScope,
    ) -> ValidationCycleSummary:
        self._require_local(realm_scope)
        async with self._session_factory() as session:
            counter, connection, listener = await _count_session_selects(session)
            outcome = "error"
            try:
                subject, access = await self._subject(
                    session,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    actor_id=actor_id,
                )
                if subject_type is AssessmentSubjectType.SPEC:
                    result = await self._spec_cycle(
                        session,
                        subject,
                        access=access,
                        include_previous=include_previous,
                        offset=offset,
                        limit=limit,
                    )
                else:
                    result = await self._ambiguity_cycle(
                        session,
                        subject_type=subject_type,
                        subject=subject,
                        include_previous=include_previous,
                        offset=offset,
                        limit=limit,
                    )
                outcome = "success"
                return result
            finally:
                event.remove(connection, "before_cursor_execute", listener)
                observe_validation_cycle_summary_selects(
                    subject_type=subject_type.value,
                    query_mode="single",
                    outcome=outcome,
                    select_count=counter[0],
                )

    async def get_validation_cycles(
        self,
        *,
        subjects: tuple[ValidationCycleSubjectRef, ...],
        actor_id: str,
        realm_scope: RealmScope,
    ) -> tuple[ValidationCycleSummary, ...]:
        """Return bounded current-edition summaries with set-based reads.

        Subject and validation rows are loaded once per entity family, never
        once per requested subject.  Permission resolution is cached per board
        because a batch may legitimately span multiple boards.
        """

        self._require_local(realm_scope)
        refs = tuple(subjects)
        if not 1 <= len(refs) <= 50:
            raise ValueError("validation_cycle_batch_size_invalid")
        if len({(item.subject_type, item.subject_id) for item in refs}) != len(refs):
            raise ValueError("validation_cycle_batch_subject_duplicate")

        async with self._session_factory() as session:
            counter, connection, listener = await _count_session_selects(session)
            outcome = "error"
            try:
                result = await self._get_validation_cycles_in_session(
                    session,
                    refs=refs,
                    actor_id=actor_id,
                )
                outcome = "success"
                return result
            finally:
                event.remove(connection, "before_cursor_execute", listener)
                subject_types = {item.subject_type for item in refs}
                observe_validation_cycle_summary_selects(
                    subject_type=(
                        next(iter(subject_types)).value
                        if len(subject_types) == 1
                        else "mixed"
                    ),
                    query_mode="batch",
                    outcome=outcome,
                    select_count=counter[0],
                )

    async def _get_validation_cycles_in_session(
        self,
        session: AsyncSession,
        *,
        refs: tuple[ValidationCycleSubjectRef, ...],
        actor_id: str,
    ) -> tuple[ValidationCycleSummary, ...]:
        loaded: dict[tuple[AssessmentSubjectType, str], object] = {}
        for subject_type, model in _SUBJECT_MODEL.items():
            ids = tuple(
                item.subject_id for item in refs if item.subject_type is subject_type
            )
            if not ids:
                continue
            rows = (
                (
                    await session.execute(
                        select(model)
                        .options(joinedload(model.board))
                        .where(model.id.in_(ids))
                    )
                )
                .scalars()
                .all()
            )
            loaded.update(
                ((subject_type, str(getattr(row, "id"))), row) for row in rows
            )

        if len(loaded) != len(refs):
            raise ValidationCycleSubjectNotFound()

        permission_cache: dict[str, object] = {}
        access_by_ref: dict[
            tuple[AssessmentSubjectType, str],
            _ValidationCycleAccess,
        ] = {}
        for ref in refs:
            subject = loaded[(ref.subject_type, ref.subject_id)]
            board_id = str(getattr(subject, "board_id"))
            if board_id not in permission_cache:
                permissions, access_level = await _quality_actor_permissions(
                    session,
                    actor_id=actor_id,
                    board_id=board_id,
                )
                permission_cache[board_id] = (
                    permissions if access_level is not None else None
                )
            permissions = permission_cache[board_id]
            if permissions is None or not permissions.has(
                f"{ref.subject_type.value}.entity.read"
            ):
                raise ValidationCycleSubjectNotFound()
            if ref.subject_type is AssessmentSubjectType.SPEC:
                visible_sections = tuple(
                    section
                    for section, permission in _SPEC_SECTION_PERMISSIONS
                    if permissions.has(permission)
                )
            else:
                visible_sections = (
                    (ValidationCycleResultType.AMBIGUITY_ASSESSMENT,)
                    if permissions.has(f"{ref.subject_type.value}.quality.read")
                    else ()
                )
            if not visible_sections:
                raise ValidationCycleReadAccessDenied()
            access_by_ref[(ref.subject_type, ref.subject_id)] = _ValidationCycleAccess(
                permissions, visible_sections
            )

        ambiguity_keys = tuple(
            (item.subject_type.value, item.subject_id)
            for item in refs
            if item.subject_type is not AssessmentSubjectType.SPEC
        )
        ambiguity_heads: dict[
            tuple[str, str],
            tuple[QualityAssessmentHeadRow, QualityAssessmentReceiptRow],
        ] = {}
        ambiguity_counts: dict[tuple[str, str], int] = {}
        if ambiguity_keys:
            pairs = (
                await session.execute(
                    select(QualityAssessmentHeadRow, QualityAssessmentReceiptRow)
                    .join(
                        QualityAssessmentReceiptRow,
                        QualityAssessmentReceiptRow.id
                        == QualityAssessmentHeadRow.receipt_id,
                    )
                    .where(
                        tuple_(
                            QualityAssessmentHeadRow.subject_type,
                            QualityAssessmentHeadRow.subject_id,
                        ).in_(ambiguity_keys),
                        QualityAssessmentHeadRow.assessment_kind
                        == AssessmentKind.AMBIGUITY.value,
                    )
                )
            ).all()
            for head, receipt in pairs:
                ref_type = AssessmentSubjectType(receipt.subject_type)
                subject = loaded[(ref_type, receipt.subject_id)]
                if receipt.subject_edition == int(getattr(subject, "edition")):
                    ambiguity_heads[(receipt.subject_type, receipt.subject_id)] = (
                        head,
                        receipt,
                    )
            count_rows = (
                await session.execute(
                    select(
                        QualityAssessmentReceiptRow.subject_type,
                        QualityAssessmentReceiptRow.subject_id,
                        func.count(),
                    )
                    .where(
                        tuple_(
                            QualityAssessmentReceiptRow.subject_type,
                            QualityAssessmentReceiptRow.subject_id,
                        ).in_(ambiguity_keys),
                        QualityAssessmentReceiptRow.assessment_kind
                        == AssessmentKind.AMBIGUITY.value,
                    )
                    .group_by(
                        QualityAssessmentReceiptRow.subject_type,
                        QualityAssessmentReceiptRow.subject_id,
                    )
                )
            ).all()
            ambiguity_counts = {
                (str(subject_type), str(subject_id)): int(count)
                for subject_type, subject_id, count in count_rows
            }

        spec_rows = tuple(
            loaded[(AssessmentSubjectType.SPEC, item.subject_id)]
            for item in refs
            if item.subject_type is AssessmentSubjectType.SPEC
        )
        spec_access = {
            str(getattr(subject, "id")): access_by_ref[
                (AssessmentSubjectType.SPEC, str(getattr(subject, "id")))
            ]
            for subject in spec_rows
        }
        spec_checks = await self._batch_spec_checks(
            session,
            spec_rows,
            access_by_spec=spec_access,
        )

        results: list[ValidationCycleSummary] = []
        for ref in refs:
            subject = loaded[(ref.subject_type, ref.subject_id)]
            edition = int(getattr(subject, "edition"))
            subject_status = _value(getattr(subject, "status"))
            if ref.subject_type is AssessmentSubjectType.SPEC:
                access = access_by_ref[(ref.subject_type, ref.subject_id)]
                validation_visible = access.can(
                    ValidationCycleResultType.SPEC_VALIDATION
                )
                validations = tuple(
                    item
                    for item in (getattr(subject, "validations", None) or ())
                    if isinstance(item, dict)
                )
                pointer = (
                    _current_spec_validation_record(subject)
                    if validation_visible
                    else None
                )
                current = None if pointer is None else _spec_result(pointer)
                navigable_previous = (
                    tuple(item for item in validations if item is not pointer)
                    if validation_visible
                    else ()
                )
                checks, actions = spec_checks[str(getattr(subject, "id"))]
                any_started = any(
                    item.status not in {"not_started", "off"} for item in checks
                )
                cycle_state = (
                    (
                        ValidationCycleState.COMPLETED
                        if current is not None
                        else (
                            ValidationCycleState.IN_PROGRESS
                            if subject_status == "approved" and any_started
                            else (
                                ValidationCycleState.PENDING
                                if subject_status == "approved"
                                else ValidationCycleState.NOT_STARTED
                            )
                        )
                    )
                    if validation_visible
                    else None
                )
                head_revision = (
                    0 if pointer is None else int(pointer.get("head_revision", 1))
                )
                results.append(
                    ValidationCycleSummary(
                        subject_type=ref.subject_type,
                        subject_id=ref.subject_id,
                        edition=edition,
                        status=subject_status,
                        cycle_state=cycle_state,
                        current_result=current,
                        previous_result_count=(
                            len(navigable_previous) if validation_visible else None
                        ),
                        submission_fence=(
                            ValidationSubmissionFence(
                                edition,
                                int(getattr(subject, "version")),
                                head_revision,
                            )
                            if validation_visible
                            else None
                        ),
                        checks=checks,
                        remaining_actions=(
                            actions if subject_status == "approved" else ()
                        ),
                        visible_sections=access.visible_sections,
                    )
                )
                continue

            key = (ref.subject_type.value, ref.subject_id)
            current_pair = ambiguity_heads.get(key)
            current = (
                None
                if current_pair is None
                else _current_quality_result(
                    current_pair[1],
                    subject_type=ref.subject_type,
                    subject=subject,
                )
            )
            total = ambiguity_counts.get(key, 0)
            results.append(
                ValidationCycleSummary(
                    subject_type=ref.subject_type,
                    subject_id=ref.subject_id,
                    edition=edition,
                    status=subject_status,
                    cycle_state=(
                        ValidationCycleState.COMPLETED
                        if current is not None
                        else (
                            ValidationCycleState.PENDING
                            if subject_status == _VALIDATION_STATUS[ref.subject_type]
                            else ValidationCycleState.NOT_STARTED
                        )
                    ),
                    current_result=current,
                    previous_result_count=max(
                        0,
                        total - (1 if current is not None else 0),
                    ),
                    submission_fence=ValidationSubmissionFence(
                        edition,
                        int(getattr(subject, "version")),
                        0 if current_pair is None else int(current_pair[0].revision),
                    ),
                    visible_sections=(ValidationCycleResultType.AMBIGUITY_ASSESSMENT,),
                )
            )
        return tuple(results)

    async def _ambiguity_cycle(
        self,
        session: AsyncSession,
        *,
        subject_type: AssessmentSubjectType,
        subject: object,
        include_previous: bool,
        offset: int,
        limit: int,
    ) -> ValidationCycleSummary:
        board_id = str(getattr(subject, "board_id"))
        subject_id = str(getattr(subject, "id"))
        edition = int(getattr(subject, "edition"))
        current_pair = (
            await session.execute(
                select(QualityAssessmentHeadRow, QualityAssessmentReceiptRow)
                .join(
                    QualityAssessmentReceiptRow,
                    QualityAssessmentReceiptRow.id
                    == QualityAssessmentHeadRow.receipt_id,
                )
                .where(
                    QualityAssessmentHeadRow.board_id == board_id,
                    QualityAssessmentHeadRow.subject_type == subject_type.value,
                    QualityAssessmentHeadRow.subject_id == subject_id,
                    QualityAssessmentHeadRow.assessment_kind
                    == AssessmentKind.AMBIGUITY.value,
                    QualityAssessmentReceiptRow.subject_edition == edition,
                )
            )
        ).one_or_none()
        current_row = None if current_pair is None else current_pair[1]
        previous_filter = (
            QualityAssessmentReceiptRow.board_id == board_id,
            QualityAssessmentReceiptRow.subject_type == subject_type.value,
            QualityAssessmentReceiptRow.subject_id == subject_id,
            QualityAssessmentReceiptRow.assessment_kind
            == AssessmentKind.AMBIGUITY.value,
            (
                QualityAssessmentReceiptRow.id
                != ("" if current_row is None else current_row.id)
            ),
        )
        previous_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(QualityAssessmentReceiptRow)
                    .where(*previous_filter)
                )
            ).scalar_one()
        )
        previous: tuple[ValidationCycleResultSummary, ...] = ()
        if include_previous:
            rows = (
                (
                    await session.execute(
                        select(QualityAssessmentReceiptRow)
                        .where(
                            *previous_filter,
                        )
                        .order_by(
                            QualityAssessmentReceiptRow.created_at.desc(),
                            QualityAssessmentReceiptRow.id.desc(),
                        )
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            previous = tuple(_quality_result(row) for row in rows)
        status = _value(getattr(subject, "status"))
        current = (
            None
            if current_row is None
            else _current_quality_result(
                current_row,
                subject_type=subject_type,
                subject=subject,
            )
        )
        return ValidationCycleSummary(
            subject_type=subject_type,
            subject_id=subject_id,
            edition=edition,
            status=status,
            cycle_state=(
                ValidationCycleState.COMPLETED
                if current is not None
                else (
                    ValidationCycleState.PENDING
                    if status == _VALIDATION_STATUS[subject_type]
                    else ValidationCycleState.NOT_STARTED
                )
            ),
            current_result=current,
            previous_result_count=previous_count,
            previous_results=previous,
            submission_fence=ValidationSubmissionFence(
                expected_validation_edition=edition,
                expected_subject_version=int(getattr(subject, "version")),
                expected_head_revision=(
                    0 if current_pair is None else int(current_pair[0].revision)
                ),
            ),
            visible_sections=(ValidationCycleResultType.AMBIGUITY_ASSESSMENT,),
        )

    async def _spec_cycle(
        self,
        session: AsyncSession,
        subject: Spec,
        *,
        access: _ValidationCycleAccess,
        include_previous: bool,
        offset: int,
        limit: int,
    ) -> ValidationCycleSummary:
        edition = int(subject.edition)
        validation_visible = access.can(ValidationCycleResultType.SPEC_VALIDATION)
        validations = (
            [item for item in (subject.validations or ()) if isinstance(item, dict)]
            if validation_visible
            else []
        )
        pointer = (
            _current_spec_validation_record(subject) if validation_visible else None
        )
        historical = (
            [item for item in reversed(validations) if item is not pointer]
            if validation_visible
            else []
        )
        previous = tuple(
            _spec_result(item)
            for item in historical[offset : offset + limit]
            if include_previous
        )
        current = None if pointer is None else _spec_result(pointer)
        checks, actions = await self._spec_checks(
            session,
            subject,
            access=access,
        )
        status = _value(subject.status)
        any_started = any(item.status not in {"not_started", "off"} for item in checks)
        return ValidationCycleSummary(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id=subject.id,
            edition=edition,
            status=status,
            cycle_state=(
                (
                    ValidationCycleState.COMPLETED
                    if current is not None
                    else (
                        ValidationCycleState.IN_PROGRESS
                        if status == "approved" and any_started
                        else (
                            ValidationCycleState.PENDING
                            if status == "approved"
                            else ValidationCycleState.NOT_STARTED
                        )
                    )
                )
                if validation_visible
                else None
            ),
            current_result=current,
            previous_result_count=(len(historical) if validation_visible else None),
            previous_results=previous,
            submission_fence=(
                ValidationSubmissionFence(
                    expected_validation_edition=edition,
                    expected_subject_version=int(subject.version),
                    expected_head_revision=(
                        0 if pointer is None else int(pointer.get("head_revision", 1))
                    ),
                )
                if validation_visible
                else None
            ),
            checks=checks,
            remaining_actions=actions if status == "approved" else (),
            visible_sections=access.visible_sections,
        )

    async def _batch_spec_checks(
        self,
        session: AsyncSession,
        subjects: tuple[Spec, ...],
        *,
        access_by_spec: Mapping[str, _ValidationCycleAccess],
    ) -> dict[
        str,
        tuple[tuple[ValidationCycleCheckSummary, ...], tuple[str, ...]],
    ]:
        """Resolve all Spec check badges with a fixed number of SELECTs."""

        if not subjects:
            return {}
        by_id = {str(subject.id): subject for subject in subjects}
        lint_spec_ids = tuple(
            spec_id
            for spec_id in by_id
            if access_by_spec[spec_id].can(ValidationCycleResultType.REQUIREMENT_LINT)
        )
        checklist_spec_ids = tuple(
            spec_id
            for spec_id in by_id
            if access_by_spec[spec_id].can(ValidationCycleResultType.CURATED_CHECKLIST)
        )
        policy_spec_ids = tuple(
            spec_id
            for spec_id in by_id
            if access_by_spec[spec_id].can(ValidationCycleResultType.POLICY_COMPLIANCE)
        )

        lint_by_spec: dict[str, QualityAssessmentReceiptRow] = {}
        if lint_spec_ids:
            for _head, receipt in (
                await session.execute(
                    select(QualityAssessmentHeadRow, QualityAssessmentReceiptRow)
                    .join(
                        QualityAssessmentReceiptRow,
                        QualityAssessmentReceiptRow.id
                        == QualityAssessmentHeadRow.receipt_id,
                    )
                    .where(
                        QualityAssessmentHeadRow.subject_type == "spec",
                        QualityAssessmentHeadRow.subject_id.in_(lint_spec_ids),
                        QualityAssessmentHeadRow.assessment_kind
                        == AssessmentKind.REQUIREMENT_LINT.value,
                    )
                )
            ).all():
                subject = by_id.get(str(receipt.subject_id))
                if subject is not None and receipt.subject_edition == int(
                    subject.edition
                ):
                    lint_by_spec[str(receipt.subject_id)] = receipt

        bindings: dict[str, ChecklistValidationBindingSnapshotRow] = {}
        if checklist_spec_ids:
            for row in (
                (
                    await session.execute(
                        select(ChecklistValidationBindingSnapshotRow).where(
                            ChecklistValidationBindingSnapshotRow.spec_id.in_(
                                checklist_spec_ids
                            ),
                            ChecklistValidationBindingSnapshotRow.target_type == "spec",
                            ChecklistValidationBindingSnapshotRow.phase
                            == "spec_validation",
                        )
                    )
                )
                .scalars()
                .all()
            ):
                subject = by_id.get(str(row.spec_id))
                if subject is not None and row.spec_edition == int(subject.edition):
                    bindings[str(row.spec_id)] = row

        checklist_by_spec: dict[
            str,
            tuple[ChecklistExecutionHeadRow, ChecklistReceiptRow],
        ] = {}
        if checklist_spec_ids:
            for head, receipt in (
                await session.execute(
                    select(ChecklistExecutionHeadRow, ChecklistReceiptRow)
                    .join(
                        ChecklistReceiptRow,
                        ChecklistReceiptRow.id == ChecklistExecutionHeadRow.receipt_id,
                    )
                    .where(
                        ChecklistExecutionHeadRow.spec_id.in_(checklist_spec_ids),
                        ChecklistExecutionHeadRow.phase == "spec_validation",
                    )
                )
            ).all():
                subject = by_id.get(str(receipt.spec_id))
                if subject is not None and receipt.spec_edition == int(subject.edition):
                    checklist_by_spec[str(receipt.spec_id)] = (head, receipt)

        receipt_ids = tuple(pair[1].id for pair in checklist_by_spec.values())
        checklist_counts: dict[str, dict[str, int]] = {}
        if receipt_ids:
            for receipt_id, outcome, count in (
                await session.execute(
                    select(
                        ChecklistItemResultRow.receipt_id,
                        ChecklistItemResultRow.outcome,
                        func.count(),
                    )
                    .where(ChecklistItemResultRow.receipt_id.in_(receipt_ids))
                    .group_by(
                        ChecklistItemResultRow.receipt_id,
                        ChecklistItemResultRow.outcome,
                    )
                )
            ).all():
                checklist_counts.setdefault(str(receipt_id), {})[str(outcome)] = int(
                    count
                )

        scopes: dict[str, SemanticGuidelineValidationScopeRow] = {}
        if policy_spec_ids:
            for row in (
                (
                    await session.execute(
                        select(SemanticGuidelineValidationScopeRow).where(
                            SemanticGuidelineValidationScopeRow.subject_type == "spec",
                            SemanticGuidelineValidationScopeRow.subject_id.in_(
                                policy_spec_ids
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            ):
                subject = by_id.get(str(row.subject_id))
                if subject is not None and row.validation_edition == int(
                    subject.edition
                ):
                    scopes[str(row.subject_id)] = row

        policy_scope_items: dict[str, tuple[_PolicyScopeItem, ...]] = {}
        policy_scope_inconsistent: dict[str, int] = {}
        flattened_policy_scope: list[_PolicyScopeItem] = []
        if policy_spec_ids:
            for spec_id in policy_spec_ids:
                items, inconsistent = _policy_scope_items(scopes.get(spec_id))
                policy_scope_items[spec_id] = items
                policy_scope_inconsistent[spec_id] = inconsistent
                flattened_policy_scope.extend(items)
            policy_snapshot = await _load_policy_snapshot_data(
                session,
                scope_items=tuple(flattened_policy_scope),
                board_ids=tuple(
                    sorted(
                        {str(by_id[spec_id].board_id) for spec_id in policy_spec_ids}
                    )
                ),
                subject_ids=tuple(sorted(policy_spec_ids)),
            )
        else:
            policy_snapshot = _PolicySnapshotData(
                authorities={},
                receipts={},
                skipped=frozenset(),
                inconsistent_authorities=frozenset(),
                inconsistent_evidence=frozenset(),
                waived_metrics=frozenset(),
                scope_required=frozenset(),
            )

        result: dict[
            str,
            tuple[tuple[ValidationCycleCheckSummary, ...], tuple[str, ...]],
        ] = {}
        for spec_id, subject in by_id.items():
            access = access_by_spec[spec_id]
            checks: list[ValidationCycleCheckSummary] = []
            lint_status: str | None = None
            checklist_status: str | None = None
            policy_status: str | None = None

            if access.can(ValidationCycleResultType.REQUIREMENT_LINT):
                lint = lint_by_spec.get(spec_id)
                if lint is None:
                    lint_status, lint_summary = "not_started", "Not started"
                else:
                    finding_count = int(lint.score)
                    lint_status = "passed" if finding_count == 0 else "needs_attention"
                    lint_summary = (
                        "No findings"
                        if finding_count == 0
                        else f"{finding_count} finding{'s' if finding_count != 1 else ''}"
                    )
                checks.append(
                    ValidationCycleCheckSummary(
                        ValidationCycleResultType.REQUIREMENT_LINT,
                        lint_status,
                        lint_summary,
                    )
                )

            if access.can(ValidationCycleResultType.CURATED_CHECKLIST):
                binding = bindings.get(spec_id)
                checklist_pair = checklist_by_spec.get(spec_id)
                if binding is not None and binding.mode == "off":
                    checklist_status, checklist_summary = "off", "Not required"
                elif checklist_pair is None:
                    checklist_status, checklist_summary = (
                        "not_started",
                        "Not started",
                    )
                else:
                    counts = checklist_counts.get(str(checklist_pair[1].id), {})
                    failed = int(counts.get("fail", 0))
                    checklist_status = "passed" if failed == 0 else "needs_attention"
                    checklist_summary = (
                        "All items passed"
                        if failed == 0
                        else f"{failed} item{'s' if failed != 1 else ''} failed"
                    )
                checks.append(
                    ValidationCycleCheckSummary(
                        ValidationCycleResultType.CURATED_CHECKLIST,
                        checklist_status,
                        checklist_summary,
                    )
                )

            if access.can(ValidationCycleResultType.POLICY_COMPLIANCE):
                policy_status, policy_summary, policy_details = _policy_check(
                    scope_items=policy_scope_items.get(spec_id, ()),
                    scope_inconsistent=policy_scope_inconsistent.get(spec_id, 0),
                    scope_missing=spec_id not in scopes,
                    scope_required=(
                        str(subject.board_id),
                        spec_id,
                        int(subject.edition),
                    )
                    in policy_snapshot.scope_required,
                    snapshot=policy_snapshot,
                )
                checks.append(
                    ValidationCycleCheckSummary(
                        ValidationCycleResultType.POLICY_COMPLIANCE,
                        policy_status,
                        policy_summary,
                        details=policy_details,
                    )
                )

            actions = _spec_remaining_actions(
                lint_status=lint_status,
                checklist_status=checklist_status,
                policy_status=policy_status,
                has_current_validation=(
                    _current_spec_validation_record(subject) is not None
                ),
                validation_visible=access.can(
                    ValidationCycleResultType.SPEC_VALIDATION
                ),
            )
            result[spec_id] = tuple(checks), actions
        return result

    async def _spec_checks(
        self,
        session: AsyncSession,
        subject: Spec,
        *,
        access: _ValidationCycleAccess,
    ) -> tuple[tuple[ValidationCycleCheckSummary, ...], tuple[str, ...]]:
        edition = int(subject.edition)
        checks: list[ValidationCycleCheckSummary] = []
        lint_status: str | None = None
        checklist_status: str | None = None
        policy_status: str | None = None

        if access.can(ValidationCycleResultType.REQUIREMENT_LINT):
            lint_pair = (
                await session.execute(
                    select(QualityAssessmentHeadRow, QualityAssessmentReceiptRow)
                    .join(
                        QualityAssessmentReceiptRow,
                        QualityAssessmentReceiptRow.id
                        == QualityAssessmentHeadRow.receipt_id,
                    )
                    .where(
                        QualityAssessmentHeadRow.board_id == subject.board_id,
                        QualityAssessmentHeadRow.subject_type == "spec",
                        QualityAssessmentHeadRow.subject_id == subject.id,
                        QualityAssessmentHeadRow.assessment_kind == "requirement_lint",
                        QualityAssessmentReceiptRow.subject_edition == edition,
                    )
                )
            ).one_or_none()
            if lint_pair is None:
                lint_status, lint_summary = "not_started", "Not started"
            else:
                lint_score = int(lint_pair[1].score)
                lint_status = "passed" if lint_score == 0 else "needs_attention"
                lint_summary = (
                    "No findings"
                    if lint_score == 0
                    else f"{lint_score} finding{'s' if lint_score != 1 else ''}"
                )
            checks.append(
                ValidationCycleCheckSummary(
                    ValidationCycleResultType.REQUIREMENT_LINT,
                    lint_status,
                    lint_summary,
                )
            )

        if access.can(ValidationCycleResultType.CURATED_CHECKLIST):
            binding = await session.get(
                ChecklistValidationBindingSnapshotRow,
                (
                    subject.board_id,
                    subject.id,
                    edition,
                    "spec",
                    "spec_validation",
                ),
            )
            checklist_pair = (
                await session.execute(
                    select(ChecklistExecutionHeadRow, ChecklistReceiptRow)
                    .join(
                        ChecklistReceiptRow,
                        ChecklistReceiptRow.id == ChecklistExecutionHeadRow.receipt_id,
                    )
                    .where(
                        ChecklistExecutionHeadRow.board_id == subject.board_id,
                        ChecklistExecutionHeadRow.spec_id == subject.id,
                        ChecklistExecutionHeadRow.phase == "spec_validation",
                        ChecklistReceiptRow.spec_edition == edition,
                    )
                )
            ).one_or_none()
            if binding is not None and binding.mode == "off":
                checklist_status, checklist_summary = "off", "Not required"
            elif checklist_pair is None:
                checklist_status, checklist_summary = "not_started", "Not started"
            else:
                counts = dict(
                    (
                        await session.execute(
                            select(
                                ChecklistItemResultRow.outcome,
                                func.count(),
                            )
                            .where(
                                ChecklistItemResultRow.receipt_id
                                == checklist_pair[1].id
                            )
                            .group_by(ChecklistItemResultRow.outcome)
                        )
                    ).all()
                )
                failed = int(counts.get("fail", 0))
                checklist_status = "passed" if failed == 0 else "needs_attention"
                checklist_summary = (
                    "All items passed"
                    if failed == 0
                    else f"{failed} item{'s' if failed != 1 else ''} failed"
                )
            checks.append(
                ValidationCycleCheckSummary(
                    ValidationCycleResultType.CURATED_CHECKLIST,
                    checklist_status,
                    checklist_summary,
                )
            )

        if access.can(ValidationCycleResultType.POLICY_COMPLIANCE):
            scope = await session.get(
                SemanticGuidelineValidationScopeRow,
                (subject.board_id, "spec", subject.id, edition),
            )
            scope_items, scope_inconsistent = _policy_scope_items(scope)
            policy_snapshot = await _load_policy_snapshot_data(
                session,
                scope_items=scope_items,
                board_ids=(str(subject.board_id),),
                subject_ids=(str(subject.id),),
            )
            policy_status, policy_summary, policy_details = _policy_check(
                scope_items=scope_items,
                scope_inconsistent=scope_inconsistent,
                scope_missing=scope is None,
                scope_required=(str(subject.board_id), str(subject.id), edition)
                in policy_snapshot.scope_required,
                snapshot=policy_snapshot,
            )
            checks.append(
                ValidationCycleCheckSummary(
                    ValidationCycleResultType.POLICY_COMPLIANCE,
                    policy_status,
                    policy_summary,
                    details=policy_details,
                )
            )

        actions = _spec_remaining_actions(
            lint_status=lint_status,
            checklist_status=checklist_status,
            policy_status=policy_status,
            has_current_validation=(
                _current_spec_validation_record(subject) is not None
            ),
            validation_visible=access.can(ValidationCycleResultType.SPEC_VALIDATION),
        )
        return tuple(checks), actions

    async def get_result_technical_audit(
        self,
        *,
        subject_type: AssessmentSubjectType,
        subject_id: str,
        result_id: str,
        result_type: ValidationCycleResultType,
        actor_id: str,
        realm_scope: RealmScope,
    ) -> ValidationTechnicalAudit:
        self._require_local(realm_scope)
        async with self._session_factory() as session:
            subject, access = await self._subject(
                session,
                subject_type=subject_type,
                subject_id=subject_id,
                actor_id=actor_id,
            )
            if subject_type is AssessmentSubjectType.SPEC:
                if result_type is ValidationCycleResultType.SPEC_VALIDATION:
                    if not access.can(ValidationCycleResultType.SPEC_VALIDATION):
                        raise ValidationCycleResultNotFound()
                    record = next(
                        (
                            item
                            for item in (getattr(subject, "validations", None) or ())
                            if isinstance(item, dict) and item.get("id") == result_id
                        ),
                        None,
                    )
                    if record is None:
                        raise ValidationCycleResultNotFound()
                    if (
                        record.get("receipt_id") != result_id
                        or not isinstance(record.get("subject_version"), int)
                        or not isinstance(record.get("head_revision"), int)
                        or not isinstance(record.get("digests"), dict)
                    ):
                        raise ValidationCycleResultNotFound()
                    edition = (
                        None
                        if record.get("edition") is None
                        else int(record["edition"])
                    )
                    details = ValidationTechnicalAuditDetails(
                        receipt_id=result_id,
                        subject_version=int(record["subject_version"]),
                        head_revision=int(record["head_revision"]),
                        digests=record["digests"],
                        exceptions=(
                            ()
                            if edition is None
                            else await self._exceptions(
                                session,
                                subject_type=subject_type,
                                subject=subject,
                                edition=edition,
                                access=access,
                            )
                        ),
                        visible_exception_types=access.visible_exception_types,
                    )
                    return ValidationTechnicalAudit(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        result_id=result_id,
                        result_type=result_type,
                        subject_edition=edition,
                        technical_audit=details,
                    )

                if (
                    result_type is not ValidationCycleResultType.REQUIREMENT_LINT
                    or not access.can(ValidationCycleResultType.REQUIREMENT_LINT)
                ):
                    raise ValidationCycleResultNotFound()
                receipt = (
                    await session.execute(
                        select(QualityAssessmentReceiptRow).where(
                            QualityAssessmentReceiptRow.id == result_id,
                            QualityAssessmentReceiptRow.board_id
                            == getattr(subject, "board_id"),
                            QualityAssessmentReceiptRow.subject_type == "spec",
                            QualityAssessmentReceiptRow.subject_id == subject_id,
                            QualityAssessmentReceiptRow.assessment_kind
                            == AssessmentKind.REQUIREMENT_LINT.value,
                        )
                    )
                ).scalar_one_or_none()
                if receipt is None:
                    raise ValidationCycleResultNotFound()
                edition = (
                    None
                    if receipt.subject_edition is None
                    else int(receipt.subject_edition)
                )
                return ValidationTechnicalAudit(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    result_id=result_id,
                    result_type=result_type,
                    subject_edition=edition,
                    technical_audit=ValidationTechnicalAuditDetails(
                        receipt_id=receipt.id,
                        subject_version=int(receipt.subject_version),
                        head_revision=int(receipt.head_revision),
                        digests={
                            "content": receipt.content_digest,
                            "clarification": receipt.clarification_digest,
                            "ruleset": receipt.ruleset_digest,
                            "taxonomy": receipt.taxonomy_digest,
                            "policy": receipt.policy_digest,
                            "input": receipt.input_digest,
                        },
                        exceptions=(
                            ()
                            if edition is None
                            else await self._exceptions(
                                session,
                                subject_type=subject_type,
                                subject=subject,
                                edition=edition,
                                access=access,
                            )
                        ),
                        visible_exception_types=access.visible_exception_types,
                    ),
                )

            if result_type is not ValidationCycleResultType.AMBIGUITY_ASSESSMENT:
                raise ValidationCycleResultNotFound()
            receipt = (
                await session.execute(
                    select(QualityAssessmentReceiptRow).where(
                        QualityAssessmentReceiptRow.id == result_id,
                        QualityAssessmentReceiptRow.board_id
                        == getattr(subject, "board_id"),
                        QualityAssessmentReceiptRow.subject_type == subject_type.value,
                        QualityAssessmentReceiptRow.subject_id == subject_id,
                        QualityAssessmentReceiptRow.assessment_kind == "ambiguity",
                    )
                )
            ).scalar_one_or_none()
            if receipt is None:
                raise ValidationCycleResultNotFound()
            edition = (
                None
                if receipt.subject_edition is None
                else int(receipt.subject_edition)
            )
            return ValidationTechnicalAudit(
                subject_type=subject_type,
                subject_id=subject_id,
                result_id=result_id,
                result_type=result_type,
                subject_edition=edition,
                technical_audit=ValidationTechnicalAuditDetails(
                    receipt_id=receipt.id,
                    subject_version=int(receipt.subject_version),
                    head_revision=int(receipt.head_revision),
                    digests={
                        "content": receipt.content_digest,
                        "clarification": receipt.clarification_digest,
                        "ruleset": receipt.ruleset_digest,
                        "taxonomy": receipt.taxonomy_digest,
                        "policy": receipt.policy_digest,
                        "input": receipt.input_digest,
                    },
                    exceptions=(
                        ()
                        if edition is None
                        else await self._exceptions(
                            session,
                            subject_type=subject_type,
                            subject=subject,
                            edition=edition,
                            access=access,
                        )
                    ),
                    visible_exception_types=access.visible_exception_types,
                ),
            )

    async def _exceptions(
        self,
        session: AsyncSession,
        *,
        subject_type: AssessmentSubjectType,
        subject: object,
        edition: int,
        access: _ValidationCycleAccess,
    ) -> tuple[ValidationEditionExceptionAudit, ...]:
        board_id = str(getattr(subject, "board_id"))
        subject_id = str(getattr(subject, "id"))
        items: list[ValidationEditionExceptionAudit] = []
        action = f"{subject_type.value}.ambiguity_gate_skip_updated"
        if (
            subject_type is not AssessmentSubjectType.SPEC
            and ValidationEditionExceptionType.AMBIGUITY_GATE_SKIP
            in access.visible_exception_types
        ):
            activity_rows = list(
                (
                    (
                        await session.execute(
                            select(ActivityLog).where(
                                ActivityLog.board_id == board_id,
                                ActivityLog.action == action,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            id_key = f"{subject_type.value}_id"
            for row in activity_rows:
                details = row.details if isinstance(row.details, dict) else {}
                if (
                    details.get(id_key) != subject_id
                    or details.get("edition") != edition
                ):
                    continue
                items.append(
                    ValidationEditionExceptionAudit(
                        exception_id=row.id,
                        exception_type=ValidationEditionExceptionType.AMBIGUITY_GATE_SKIP,
                        subject_edition=edition,
                        status="active" if details.get("new_value") else "revoked",
                        reason=str(details.get("reason") or "Recorded by a human"),
                        actor_id=row.actor_id,
                        recorded_at=_aware(row.created_at),
                    )
                )

        if ValidationEditionExceptionType.POLICY_SKIP in access.visible_exception_types:
            skip_rows = list(
                (
                    (
                        await session.execute(
                            select(SemanticGuidelineSkipRow).where(
                                SemanticGuidelineSkipRow.board_id == board_id,
                                SemanticGuidelineSkipRow.subject_type
                                == subject_type.value,
                                SemanticGuidelineSkipRow.subject_id == subject_id,
                                SemanticGuidelineSkipRow.validation_edition == edition,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            items.extend(
                ValidationEditionExceptionAudit(
                    exception_id=row.event_id,
                    exception_type=ValidationEditionExceptionType.POLICY_SKIP,
                    subject_edition=edition,
                    status=row.status,
                    reason=row.reason,
                    actor_id=row.actor_id,
                    recorded_at=_aware(row.occurred_at),
                )
                for row in skip_rows
            )

        if (
            ValidationEditionExceptionType.POLICY_WAIVER
            in access.visible_exception_types
        ):
            waiver_rows = list(
                (
                    (
                        await session.execute(
                            select(SemanticGuidelineWaiverEventRow)
                            .join(
                                SemanticGuidelineWaiverRow,
                                (
                                    SemanticGuidelineWaiverRow.waiver_id
                                    == SemanticGuidelineWaiverEventRow.waiver_id
                                )
                                & (
                                    SemanticGuidelineWaiverRow.board_id
                                    == SemanticGuidelineWaiverEventRow.board_id
                                ),
                            )
                            .where(
                                SemanticGuidelineWaiverRow.board_id == board_id,
                                SemanticGuidelineWaiverRow.subject_type
                                == subject_type.value,
                                SemanticGuidelineWaiverRow.subject_id == subject_id,
                                SemanticGuidelineWaiverEventRow.validation_edition
                                == edition,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            items.extend(
                ValidationEditionExceptionAudit(
                    exception_id=row.event_id,
                    exception_type=ValidationEditionExceptionType.POLICY_WAIVER,
                    subject_edition=edition,
                    status=row.to_status,
                    reason=row.reason,
                    actor_id=row.actor_id,
                    recorded_at=_aware(row.occurred_at),
                )
                for row in waiver_rows
            )
        return tuple(
            sorted(items, key=lambda item: (item.recorded_at, item.exception_id))
        )


__all__ = ["CommunitySqlAlchemyValidationCycleReader"]
