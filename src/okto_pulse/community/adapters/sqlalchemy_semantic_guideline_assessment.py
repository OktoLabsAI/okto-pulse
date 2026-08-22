"""SQLAlchemy persistence for semantic guideline assessments.

This adapter is intentionally separate from the retired predicate evaluator.
Agents provide cognitive scores and evidence; Core validates and seals the
deterministic result, while this module owns authoritative re-resolution,
append-only storage, idempotency, and live currentness fences.

The caller owns the transaction.  Methods flush when relational invariants
must be proven and never commit, roll back, close, or create a nested unit of
work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy import false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    GuidelineBindingProvenance,
    GuidelineBindingState,
    GuidelineEnforcement,
    GuidelineMetric,
    GuidelineMetricDirection,
    GuidelineRevision,
    PolicyCurrentness,
    PolicyEntityType,
    PolicySubjectRef,
    PolicySubjectSnapshot,
)
from okto_pulse.core.domain.guideline_semantic_assessment import (
    LEGACY_UNKNOWN_SEMANTIC_EDITOR_ID,
    SemanticAssessmentAssessor,
    SemanticAssessmentPinpoint,
    SemanticAssessmentState,
    SemanticGuidelineAssessmentContext,
    SemanticGuidelineAssessmentReceipt,
    SemanticGuidelineAssessmentResult,
    SemanticMetricOutcome,
    SemanticMetricResult,
    SemanticThresholdSource,
    semantic_binding_head_digest_v1,
    semantic_policy_set_digest_v1,
)
from okto_pulse.core.domain.guideline_semantic_currentness import (
    SemanticAssessmentCurrentnessReason,
    SemanticAssessmentCurrentSnapshot,
    semantic_assessment_current_snapshot_from_context,
)
from okto_pulse.core.domain.guideline_semantic_exceptions import (
    SemanticExceptionActorKind,
    SemanticMetricWaiver,
    SemanticMetricWaiverAnchor,
    SemanticMetricWaiverEvent,
    SemanticMetricWaiverEventType,
    SemanticMetricWaiverExpireReason,
    SemanticMetricWaiverMutation,
    SemanticMetricWaiverRevalidationReason,
    SemanticMetricWaiverRevalidationStatus,
    SemanticMetricWaiverStatus,
    SemanticPolicySkip,
    SemanticPolicySkipEvent,
    SemanticPolicySkipEventType,
    SemanticPolicySkipMutation,
    SemanticPolicySkipScope,
    SemanticPolicySkipStatus,
)
from okto_pulse.core.domain.guideline_semantic_findings import (
    SemanticMetricFinding,
    project_semantic_metric_findings,
    semantic_metric_result_digest_v1,
)
from okto_pulse.core.domain.guideline_semantic_snapshot import (
    SemanticPolicySubjectSnapshotError,
    semantic_policy_subject_content_digest_v1,
)
from okto_pulse.core.domain.guideline_semantic_transition import (
    PolicyTransitionSnapshot,
    SemanticBindingComplianceSnapshot,
)
from okto_pulse.core.domain.quality_assessment import (
    EvidenceRef,
    FindingAnchorType,
)
from okto_pulse.core.domain.quality_canonicalization import (
    canonical_sha256,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyDigestConflict,
    GuidelinePolicyEditionConflict,
    GuidelinePolicyIdempotencyConflict,
    GuidelinePolicySubjectConflict,
)

from .sqlalchemy_models import (
    ArchitectureDesign,
    ArchitectureDiagramPayload,
    Card,
    GuidelineBoardBindingRow,
    GuidelineRevisionRow,
    Ideation,
    IdeationKnowledgeBase,
    IdeationQAItem,
    QAItem,
    Refinement,
    RefinementKnowledgeBase,
    RefinementQAItem,
    SemanticGuidelineAssessmentReceiptRow,
    SemanticGuidelineBindingConfigurationRow,
    SemanticGuidelineFindingRow,
    SemanticGuidelineMetricResultRow,
    SemanticGuidelineRevisionRow,
    SemanticGuidelineSkipRow,
    SemanticGuidelineValidationScopeRow,
    SemanticGuidelineWaiverEventRow,
    SemanticGuidelineWaiverRow,
    SemanticSubjectVersionEventRow,
    SemanticSubjectVersionRow,
    Spec,
    SpecKnowledgeBase,
    SpecQAItem,
    Sprint,
    SprintQAItem,
)
from .sqlalchemy_policy_subject_versioning import lock_policy_board
from .semantic_guideline_kg_events import (
    SemanticGuidelineProjectionFact,
    stage_semantic_guideline_projection_events,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class _SemanticSubjectMaterial:
    subject_version: int
    subject_edition: int | None
    artifact: dict[str, object]
    q_and_a: tuple[dict[str, object], ...]
    resource_refs: tuple[dict[str, object], ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


def _require_sha256(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise GuidelinePolicyDigestConflict(code)
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise GuidelinePolicyDigestConflict(code)
    return normalized


def _evidence_payload(value: EvidenceRef) -> dict[str, object]:
    return {
        "source_type": value.source_type,
        "source_id": value.source_id,
        "source_version": value.source_version,
        "content_hash": value.content_hash,
    }


def _evidence_from_payload(value: object) -> EvidenceRef:
    if not isinstance(value, dict):
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_evidence_snapshot_invalid"
        )
    try:
        return EvidenceRef(
            source_type=value["source_type"],
            source_id=value["source_id"],
            source_version=value["source_version"],
            content_hash=value["content_hash"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_evidence_snapshot_invalid"
        ) from exc


def _pinpoint_payload(value: SemanticAssessmentPinpoint) -> dict[str, object]:
    return {
        "subject": {
            "board_id": value.subject.board_id,
            "subject_type": value.subject.entity_type.value,
            "subject_id": value.subject.subject_id,
            "subject_version": value.subject.subject_version,
            "subject_edition": value.subject.subject_edition,
        },
        "input_digest": value.input_digest,
        "anchor_type": value.anchor_type.value,
        "anchor_ref": value.anchor_ref,
        "excerpt_hash": value.excerpt_hash,
    }


def _pinpoint_from_payload(value: object) -> SemanticAssessmentPinpoint:
    if not isinstance(value, dict):
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_pinpoint_snapshot_invalid"
        )
    try:
        raw_subject = value["subject"]
        if not isinstance(raw_subject, dict):
            raise TypeError
        subject = PolicySubjectRef(
            board_id=raw_subject["board_id"],
            entity_type=PolicyEntityType(raw_subject["subject_type"]),
            subject_id=raw_subject["subject_id"],
            subject_version=raw_subject["subject_version"],
            subject_edition=raw_subject.get("subject_edition"),
        )
        return SemanticAssessmentPinpoint(
            subject=subject,
            input_digest=value["input_digest"],
            anchor_type=FindingAnchorType(value["anchor_type"]),
            anchor_ref=value.get("anchor_ref"),
            excerpt_hash=value.get("excerpt_hash"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_pinpoint_snapshot_invalid"
        ) from exc

def _semantic_subject_payload(
    *,
    entity_type: PolicyEntityType,
    row: Any,
) -> dict[str, object]:
    if entity_type is PolicyEntityType.IDEATION:
        fields = (
            "title",
            "description",
            "problem_statement",
            "proposed_approach",
        )
    elif entity_type is PolicyEntityType.REFINEMENT:
        fields = (
            "title",
            "description",
            "in_scope",
            "out_of_scope",
            "analysis",
            "decisions",
        )
    elif entity_type is PolicyEntityType.SPEC:
        fields = (
            "title",
            "description",
            "context",
            "functional_requirements",
            "technical_requirements",
            "acceptance_criteria",
            "test_scenarios",
            "business_rules",
            "api_contracts",
            "integration_requirements",
            "observability_requirements",
            "decisions",
        )
    elif entity_type is PolicyEntityType.CARD:
        fields = (
            "title",
            "description",
            "details",
            "card_type",
            "severity",
            "expected_behavior",
            "observed_behavior",
            "steps_to_reproduce",
            "action_plan",
            "test_scenario_ids",
            "linked_test_task_ids",
        )
    elif entity_type is PolicyEntityType.SPRINT:
        fields = (
            "title",
            "description",
            "lane_type",
            "origin_sprint_id",
            "origin_bug_id",
            "objective",
            "expected_outcome",
            "test_scenario_ids",
            "business_rule_ids",
        )
    else:
        raise GuidelinePolicySubjectConflict(
            "semantic_assessment_subject_type_invalid"
        )
    return {field: getattr(row, field, None) for field in fields}


def _qa_payload(row: Any) -> dict[str, object]:
    return {
        "id": row.id,
        "revision": int(getattr(row, "revision", 1)),
        "question": row.question,
        "question_type": getattr(row, "question_type", "text"),
        "choices": list(getattr(row, "choices", None) or ()),
        "allow_free_text": bool(
            getattr(row, "allow_free_text", False)
        ),
        "answer": row.answer,
        "selected": list(getattr(row, "selected", None) or ()),
        "lifecycle": getattr(row, "lifecycle", "active"),
        "tombstoned": bool(getattr(row, "tombstoned", False)),
    }


_RESOURCE_VOLATILE_KEYS = frozenset(
    {
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
        "asked_by",
        "answered_by",
        "answered_at",
        "stale",
        "breaking_change_flag",
        "requires_arch_review",
        "quality",
        "quality_score",
        "quality_findings",
        "finding_count",
        "findings",
        "warning_acknowledgements",
    }
)


def _resource_value(value: object) -> object:
    """Project authored nested resource content without volatile metadata."""

    if isinstance(value, dict):
        return {
            str(key): _resource_value(item)
            for key, item in value.items()
            if str(key) not in _RESOURCE_VOLATILE_KEYS
        }
    if isinstance(value, tuple | list):
        return [_resource_value(item) for item in value]
    return value


def _knowledge_payload(row: Any) -> dict[str, object]:
    return {
        "id": row.id,
        "title": row.title,
        "description": row.description,
        "content": row.content,
        "mime_type": row.mime_type,
        "source_type": getattr(row, "source_type", None),
        "source_id": getattr(row, "source_id", None),
        "source_title": getattr(row, "source_title", None),
        "source_version": getattr(row, "source_version", None),
        "source_kb_id": getattr(row, "source_kb_id", None),
        "root_source_kb_id": getattr(row, "root_source_kb_id", None),
        "immediate_parent_kb_id": getattr(
            row,
            "immediate_parent_kb_id",
            None,
        ),
        "governance_metadata": _resource_value(
            getattr(row, "governance_metadata", None)
        ),
    }


def _embedded_knowledge_payload(value: dict[str, object]) -> dict[str, object]:
    return {
        key: _resource_value(value.get(key))
        for key in (
            "id",
            "title",
            "description",
            "content",
            "mime_type",
            "source_type",
            "source_id",
            "source_title",
            "source_version",
            "source_kb_id",
            "root_source_kb_id",
            "immediate_parent_kb_id",
            "governance_metadata",
        )
    }


def _mockup_payload(value: dict[str, object]) -> dict[str, object]:
    """Closed authored mockup projection including lineage and stable order."""

    return {
        key: _resource_value(value.get(key))
        for key in (
            "id",
            "title",
            "description",
            "screen_type",
            "html_content",
            "annotations",
            "order",
            "version",
            "source_ref",
            "source_id",
            "source_version",
            "source_mockup_id",
            "root_source_mockup_id",
            "immediate_parent_mockup_id",
            "origin",
        )
    }


def _resource_ref(
    *,
    resource_type: str,
    resource_id: str,
    resource_version: int,
    semantic_payload: dict[str, object],
    source_ref: str | None,
) -> dict[str, object]:
    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "resource_version": resource_version,
        "content_digest": canonical_sha256(semantic_payload),
        "source_ref": source_ref,
    }


def _required_resource_id(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GuidelinePolicyDigestConflict(code)
    return value.strip()


def _semantic_digest(
    entity_type: PolicyEntityType,
    material: _SemanticSubjectMaterial,
) -> str:
    """Hash the complete cognitive input through Core's closed TR-07 contract."""

    try:
        return semantic_policy_subject_content_digest_v1(
            subject_type=entity_type,
            artifact=material.artifact,
            q_and_a=material.q_and_a,
            resource_refs=material.resource_refs,
        )
    except SemanticPolicySubjectSnapshotError as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_subject_digest_invalid"
        ) from exc


def _metric_from_row(
    row: SemanticGuidelineMetricResultRow,
    *,
    subject_edition: int | None = None,
) -> SemanticMetricResult:
    try:
        subject = PolicySubjectRef(
            board_id=row.board_id,
            entity_type=PolicyEntityType(row.subject_type),
            subject_id=row.subject_id,
            subject_version=row.subject_version,
            subject_edition=subject_edition,
        )
        result = SemanticMetricResult(
            metric_result_id=row.result_id,
            receipt_id=row.receipt_id,
            subject=subject,
            binding_id=row.binding_id,
            guideline_id=row.guideline_id,
            revision_id=row.revision_id,
            metric_id=row.metric_id,
            metric_code=row.metric_code,
            metric_definition_digest=row.metric_definition_digest,
            score=row.score,
            direction=GuidelineMetricDirection(row.direction),
            default_threshold=row.default_threshold,
            effective_threshold=row.effective_threshold,
            threshold_source=SemanticThresholdSource(row.threshold_source),
            outcome=SemanticMetricOutcome(row.outcome),
            rationale=row.rationale,
            evidence_refs=tuple(
                _evidence_from_payload(item) for item in row.evidence_refs
            ),
            pinpoints=tuple(
                _pinpoint_from_payload(item) for item in row.pinpoints
            ),
        )
    except (TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_metric_result_snapshot_invalid"
        ) from exc
    if semantic_metric_result_digest_v1(result) != row.result_digest:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_metric_result_digest_mismatch"
        )
    return result


def _finding_from_row(
    row: SemanticGuidelineFindingRow,
    *,
    subject_edition: int | None = None,
) -> SemanticMetricFinding:
    try:
        return SemanticMetricFinding(
            finding_id=row.finding_id,
            metric_result_id=row.metric_result_id,
            metric_result_digest=row.metric_result_digest,
            receipt_id=row.receipt_id,
            receipt_digest=row.receipt_digest,
            subject=PolicySubjectRef(
                board_id=row.board_id,
                entity_type=PolicyEntityType(row.subject_type),
                subject_id=row.subject_id,
                subject_version=row.subject_version,
                subject_edition=subject_edition,
            ),
            subject_content_digest=row.subject_content_digest,
            guideline_id=row.guideline_id,
            guideline_revision_id=row.revision_id,
            guideline_revision_digest=row.revision_digest,
            binding_id=row.binding_id,
            binding_revision=row.binding_revision,
            binding_configuration_digest=row.configuration_digest,
            metric_id=row.metric_id,
            metric_code=row.metric_code,
            rationale=row.rationale,
            evidence_refs=tuple(
                _evidence_from_payload(item) for item in row.evidence_refs
            ),
            pinpoints=tuple(
                _pinpoint_from_payload(item) for item in row.pinpoints
            ),
            created_at=_utc(row.created_at),
            finding_digest=row.finding_digest,
        )
    except (TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_finding_snapshot_invalid"
        ) from exc


def _waiver_from_row(row: SemanticGuidelineWaiverRow) -> SemanticMetricWaiver:
    try:
        anchor = SemanticMetricWaiverAnchor(
            metric_result_id=row.metric_result_id,
            metric_result_digest=row.metric_result_digest,
            finding_id=row.finding_id,
            finding_digest=row.finding_digest,
            receipt_id=row.receipt_id,
            receipt_digest=row.receipt_digest,
            subject=PolicySubjectRef(
                board_id=row.board_id,
                entity_type=PolicyEntityType(row.subject_type),
                subject_id=row.subject_id,
                subject_version=row.subject_version,
                subject_edition=row.validation_edition,
            ),
            subject_content_digest=row.subject_content_digest,
            guideline_id=row.guideline_id,
            guideline_revision_id=row.revision_id,
            guideline_revision_digest=row.revision_digest,
            binding_id=row.binding_id,
            binding_revision=row.binding_revision,
            binding_configuration_digest=row.configuration_digest,
            metric_id=row.metric_id,
            metric_code=row.metric_code,
            assessment_assessor_id=row.assessment_assessor_id,
        )
        return SemanticMetricWaiver(
            waiver_id=row.waiver_id,
            anchor=anchor,
            scope_digest=row.scope_digest,
            justification=row.justification,
            evidence_refs=tuple(
                _evidence_from_payload(item) for item in row.evidence_refs
            ),
            requested_by=row.requested_by,
            requested_at=_utc(row.requested_at),
            original_expires_at=_utc_optional(row.original_expires_at),
            status=SemanticMetricWaiverStatus(row.status),
            waiver_revision=row.waiver_revision,
            expires_at=_utc_optional(row.expires_at),
            last_event_id=row.last_event_id,
            last_event_type=SemanticMetricWaiverEventType(
                row.last_event_type
            ),
            last_event_at=_utc(row.last_event_at),
            last_event_idempotency_key=(
                row.last_event_idempotency_key
            ),
            reviewed_by=row.reviewed_by,
            reviewed_at=_utc_optional(row.reviewed_at),
            review_reason=row.review_reason,
            revoked_by=row.revoked_by,
            revoked_at=_utc_optional(row.revoked_at),
            expire_reason=(
                SemanticMetricWaiverExpireReason(row.expire_reason_code)
                if row.expire_reason_code is not None
                else None
            ),
            last_revalidation_status=(
                SemanticMetricWaiverRevalidationStatus(
                    row.last_revalidation_status
                )
                if row.last_revalidation_status is not None
                else None
            ),
            last_revalidation_current=row.last_revalidation_current,
            last_revalidation_reason_code=(
                SemanticMetricWaiverRevalidationReason(
                    row.last_revalidation_reason_code
                )
                if row.last_revalidation_reason_code is not None
                else None
            ),
            last_revalidation_evaluated_at=_utc_optional(
                row.last_revalidation_evaluated_at
            ),
            last_revalidation_currentness_reasons=tuple(
                SemanticAssessmentCurrentnessReason(item)
                for item in row.last_revalidation_currentness_reasons
            ),
            last_revalidation_scheduled_expiry_observed=(
                row.last_revalidation_scheduled_expiry_observed
            ),
            head_digest=row.head_digest,
        )
    except (TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_waiver_snapshot_invalid"
        ) from exc


def _waiver_event_from_row(
    row: SemanticGuidelineWaiverEventRow,
) -> SemanticMetricWaiverEvent:
    try:
        is_revalidation = (
            row.event_type
            == SemanticMetricWaiverEventType.REVALIDATE.value
        )
        return SemanticMetricWaiverEvent(
            event_id=row.event_id,
            predecessor_event_id=row.predecessor_event_id,
            waiver_id=row.waiver_id,
            waiver_revision=row.waiver_revision,
            event_type=SemanticMetricWaiverEventType(row.event_type),
            from_status=(
                SemanticMetricWaiverStatus(row.from_status)
                if row.from_status is not None
                else None
            ),
            to_status=SemanticMetricWaiverStatus(row.to_status),
            actor_id=row.actor_id,
            occurred_at=_utc(row.occurred_at),
            reason=row.reason,
            evidence_refs=tuple(
                _evidence_from_payload(item) for item in row.evidence_refs
            ),
            expires_at=_utc_optional(row.expires_at),
            scope_digest=row.scope_digest,
            waiver_digest=row.waiver_digest,
            idempotency_key=row.idempotency_key,
            request_digest=row.request_digest,
            expire_reason=(
                SemanticMetricWaiverExpireReason(row.expire_reason_code)
                if row.expire_reason_code is not None
                else None
            ),
            evaluated_at=(
                _utc_optional(row.evaluated_at)
                if is_revalidation
                else None
            ),
            revalidation_status=(
                SemanticMetricWaiverRevalidationStatus(
                    row.revalidation_status
                )
                if is_revalidation
                and row.revalidation_status is not None
                else None
            ),
            revalidation_current=(
                row.revalidation_current
                if is_revalidation
                else None
            ),
            revalidation_reason_code=(
                SemanticMetricWaiverRevalidationReason(
                    row.revalidation_reason_code
                )
                if is_revalidation
                and row.revalidation_reason_code is not None
                else None
            ),
            currentness_reasons=(
                tuple(
                    SemanticAssessmentCurrentnessReason(item)
                    for item in row.currentness_reasons
                )
                if is_revalidation
                else ()
            ),
            scheduled_expiry_observed=(
                row.scheduled_expiry_observed
                if is_revalidation
                else False
            ),
        )
    except (TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_waiver_event_snapshot_invalid"
        ) from exc


def _waiver_mutation_from_rows(
    head: SemanticGuidelineWaiverRow,
    event: SemanticGuidelineWaiverEventRow,
    *,
    revalidation_snapshot: SemanticGuidelineWaiverEventRow | None = None,
) -> SemanticMetricWaiverMutation:
    try:
        current = _waiver_from_row(head)
        snapshot = (
            event
            if event.event_type
            == SemanticMetricWaiverEventType.REVALIDATE.value
            else revalidation_snapshot
        )
        historical = SemanticMetricWaiver(
            waiver_id=current.waiver_id,
            anchor=current.anchor,
            scope_digest=current.scope_digest,
            justification=current.justification,
            evidence_refs=current.evidence_refs,
            requested_by=current.requested_by,
            requested_at=current.requested_at,
            original_expires_at=current.original_expires_at,
            status=SemanticMetricWaiverStatus(event.to_status),
            waiver_revision=event.waiver_revision,
            expires_at=_utc_optional(event.expires_at),
            last_event_id=event.event_id,
            last_event_type=SemanticMetricWaiverEventType(event.event_type),
            last_event_at=_utc(event.occurred_at),
            last_event_idempotency_key=event.idempotency_key,
            reviewed_by=event.reviewed_by,
            reviewed_at=_utc_optional(event.reviewed_at),
            review_reason=event.review_reason,
            revoked_by=event.revoked_by,
            revoked_at=_utc_optional(event.revoked_at),
            expire_reason=(
                SemanticMetricWaiverExpireReason(event.expire_reason_code)
                if event.expire_reason_code is not None
                else None
            ),
            last_revalidation_status=(
                SemanticMetricWaiverRevalidationStatus(
                    snapshot.revalidation_status
                )
                if snapshot is not None
                and snapshot.revalidation_status is not None
                else None
            ),
            last_revalidation_current=(
                snapshot.revalidation_current
                if snapshot is not None
                else None
            ),
            last_revalidation_reason_code=(
                SemanticMetricWaiverRevalidationReason(
                    snapshot.revalidation_reason_code
                )
                if snapshot is not None
                and snapshot.revalidation_reason_code is not None
                else None
            ),
            last_revalidation_evaluated_at=_utc_optional(
                snapshot.evaluated_at
                if snapshot is not None
                else None
            ),
            last_revalidation_currentness_reasons=tuple(
                SemanticAssessmentCurrentnessReason(item)
                for item in (
                    snapshot.currentness_reasons
                    if snapshot is not None
                    else ()
                )
            ),
            last_revalidation_scheduled_expiry_observed=(
                snapshot.scheduled_expiry_observed
                if snapshot is not None
                else False
            ),
            head_digest=event.waiver_digest,
        )
        return SemanticMetricWaiverMutation(
            waiver=historical,
            event=_waiver_event_from_row(event),
        )
    except (TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_waiver_mutation_snapshot_invalid"
        ) from exc


def _skip_mutation_from_row(
    row: SemanticGuidelineSkipRow,
) -> SemanticPolicySkipMutation:
    try:
        scope = SemanticPolicySkipScope(
            subject=PolicySubjectRef(
                board_id=row.board_id,
                entity_type=PolicyEntityType(row.subject_type),
                subject_id=row.subject_id,
                subject_version=row.subject_version,
                subject_edition=row.validation_edition,
            ),
            subject_content_digest=row.subject_content_digest,
            guideline_id=row.guideline_id,
            guideline_revision_id=row.revision_id,
            guideline_revision_digest=row.revision_digest,
            binding_id=row.binding_id,
            binding_revision=row.binding_revision,
            binding_configuration_digest=row.configuration_digest,
        )
        skip = SemanticPolicySkip(
            skip_id=row.skip_id,
            skip_revision=row.skip_revision,
            scope=scope,
            scope_digest=row.scope_digest,
            status=SemanticPolicySkipStatus(row.status),
            reason=row.reason,
            created_by=row.created_by,
            created_at=_utc(row.created_at),
            last_event_id=row.event_id,
            last_event_type=SemanticPolicySkipEventType(row.event_type),
            last_event_at=_utc(row.occurred_at),
            idempotency_key=row.idempotency_key,
            request_digest=row.request_digest,
            revoked_by=row.revoked_by,
            revoked_at=_utc_optional(row.revoked_at),
            revocation_reason=row.revocation_reason,
            skip_digest=row.skip_digest,
        )
        event = SemanticPolicySkipEvent(
            event_id=row.event_id,
            predecessor_event_id=row.predecessor_event_id,
            skip_id=row.skip_id,
            skip_revision=row.skip_revision,
            event_type=SemanticPolicySkipEventType(row.event_type),
            from_status=(
                SemanticPolicySkipStatus(row.from_status)
                if row.from_status is not None
                else None
            ),
            to_status=SemanticPolicySkipStatus(row.status),
            actor_id=row.actor_id,
            actor_kind=SemanticExceptionActorKind(row.actor_kind),
            occurred_at=_utc(row.occurred_at),
            reason=(
                row.revocation_reason
                if row.event_type == SemanticPolicySkipEventType.REVOKE.value
                else row.reason
            ),
            scope_digest=row.scope_digest,
            skip_digest=row.skip_digest,
            idempotency_key=row.idempotency_key,
            request_digest=row.request_digest,
        )
        return SemanticPolicySkipMutation(skip=skip, event=event)
    except (TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_skip_snapshot_invalid"
        ) from exc


def _new_waiver_row(
    mutation: SemanticMetricWaiverMutation,
) -> SemanticGuidelineWaiverRow:
    waiver = mutation.waiver
    anchor = waiver.anchor
    return SemanticGuidelineWaiverRow(
        waiver_id=waiver.waiver_id,
        board_id=anchor.subject.board_id,
        metric_result_id=anchor.metric_result_id,
        finding_id=anchor.finding_id,
        receipt_id=anchor.receipt_id,
        receipt_digest=anchor.receipt_digest,
        assessment_assessor_id=anchor.assessment_assessor_id,
        subject_type=anchor.subject.entity_type.value,
        subject_id=anchor.subject.subject_id,
        subject_version=anchor.subject.subject_version,
        validation_edition=anchor.subject.subject_edition,
        subject_content_digest=anchor.subject_content_digest,
        guideline_id=anchor.guideline_id,
        revision_id=anchor.guideline_revision_id,
        revision_digest=anchor.guideline_revision_digest,
        binding_id=anchor.binding_id,
        binding_revision=anchor.binding_revision,
        configuration_digest=anchor.binding_configuration_digest,
        metric_id=anchor.metric_id,
        metric_code=anchor.metric_code,
        metric_result_digest=anchor.metric_result_digest,
        finding_digest=anchor.finding_digest,
        scope_digest=waiver.scope_digest,
        justification=waiver.justification,
        evidence_refs=[
            _evidence_payload(item) for item in waiver.evidence_refs
        ],
        requested_by=waiver.requested_by,
        requested_at=waiver.requested_at,
        original_expires_at=waiver.original_expires_at,
        status=waiver.status.value,
        waiver_revision=waiver.waiver_revision,
        expires_at=waiver.expires_at,
        last_event_id=waiver.last_event_id,
        last_event_type=waiver.last_event_type.value,
        last_event_at=waiver.last_event_at,
        last_event_idempotency_key=(
            waiver.last_event_idempotency_key
        ),
        reviewed_by=waiver.reviewed_by,
        reviewed_at=waiver.reviewed_at,
        review_reason=waiver.review_reason,
        revoked_by=waiver.revoked_by,
        revoked_at=waiver.revoked_at,
        expire_reason_code=(
            waiver.expire_reason.value
            if waiver.expire_reason is not None
            else None
        ),
        last_revalidation_status=(
            waiver.last_revalidation_status.value
            if waiver.last_revalidation_status is not None
            else None
        ),
        last_revalidation_current=waiver.last_revalidation_current,
        last_revalidation_reason_code=(
            waiver.last_revalidation_reason_code.value
            if waiver.last_revalidation_reason_code is not None
            else None
        ),
        last_revalidation_evaluated_at=(
            waiver.last_revalidation_evaluated_at
        ),
        last_revalidation_currentness_reasons=[
            item.value
            for item in waiver.last_revalidation_currentness_reasons
        ],
        last_revalidation_scheduled_expiry_observed=(
            waiver.last_revalidation_scheduled_expiry_observed
        ),
        head_digest=waiver.head_digest,
        idempotency_key=mutation.event.idempotency_key,
        request_digest=mutation.event.request_digest,
    )


def _new_waiver_event_row(
    mutation: SemanticMetricWaiverMutation,
    *,
    board_id: str,
) -> SemanticGuidelineWaiverEventRow:
    waiver = mutation.waiver
    event = mutation.event
    return SemanticGuidelineWaiverEventRow(
        event_id=event.event_id,
        predecessor_event_id=event.predecessor_event_id,
        waiver_id=event.waiver_id,
        board_id=board_id,
        validation_edition=waiver.anchor.subject.subject_edition,
        waiver_revision=event.waiver_revision,
        event_type=event.event_type.value,
        from_status=(
            event.from_status.value
            if event.from_status is not None
            else None
        ),
        to_status=event.to_status.value,
        actor_id=event.actor_id,
        occurred_at=event.occurred_at,
        reason=event.reason,
        evidence_refs=[
            _evidence_payload(item) for item in event.evidence_refs
        ],
        expires_at=event.expires_at,
        scope_digest=event.scope_digest,
        waiver_digest=event.waiver_digest,
        reviewed_by=waiver.reviewed_by,
        reviewed_at=waiver.reviewed_at,
        review_reason=waiver.review_reason,
        revoked_by=waiver.revoked_by,
        revoked_at=waiver.revoked_at,
        expire_reason_code=(
            event.expire_reason.value
            if event.expire_reason is not None
            else None
        ),
        evaluated_at=event.evaluated_at,
        revalidation_status=(
            event.revalidation_status.value
            if event.revalidation_status is not None
            else None
        ),
        revalidation_current=event.revalidation_current,
        revalidation_reason_code=(
            event.revalidation_reason_code.value
            if event.revalidation_reason_code is not None
            else None
        ),
        currentness_reasons=[
            item.value
            for item in event.currentness_reasons
        ],
        scheduled_expiry_observed=(
            event.scheduled_expiry_observed
        ),
        idempotency_key=event.idempotency_key,
        request_digest=event.request_digest,
    )


def _new_skip_row(
    mutation: SemanticPolicySkipMutation,
) -> SemanticGuidelineSkipRow:
    skip = mutation.skip
    event = mutation.event
    scope = skip.scope
    return SemanticGuidelineSkipRow(
        event_id=event.event_id,
        predecessor_event_id=event.predecessor_event_id,
        skip_id=skip.skip_id,
        skip_revision=skip.skip_revision,
        event_type=event.event_type.value,
        from_status=(
            event.from_status.value
            if event.from_status is not None
            else None
        ),
        status=skip.status.value,
        board_id=scope.subject.board_id,
        subject_type=scope.subject.entity_type.value,
        subject_id=scope.subject.subject_id,
        subject_version=scope.subject.subject_version,
        validation_edition=scope.subject.subject_edition,
        subject_content_digest=scope.subject_content_digest,
        guideline_id=scope.guideline_id,
        revision_id=scope.guideline_revision_id,
        revision_digest=scope.guideline_revision_digest,
        binding_id=scope.binding_id,
        binding_revision=scope.binding_revision,
        configuration_digest=scope.binding_configuration_digest,
        scope_digest=skip.scope_digest,
        reason=skip.reason,
        created_by=skip.created_by,
        created_at=skip.created_at,
        actor_id=event.actor_id,
        actor_kind=event.actor_kind.value,
        occurred_at=event.occurred_at,
        revoked_by=skip.revoked_by,
        revoked_at=skip.revoked_at,
        revocation_reason=skip.revocation_reason,
        skip_digest=skip.skip_digest,
        idempotency_key=event.idempotency_key,
        request_digest=event.request_digest,
    )


def _receipt_from_rows(
    row: SemanticGuidelineAssessmentReceiptRow,
    metric_rows: tuple[SemanticGuidelineMetricResultRow, ...],
) -> SemanticGuidelineAssessmentReceipt:
    if not row.sealed:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_receipt_unsealed"
        )
    if row.recorded_currentness != PolicyCurrentness.CURRENT.value:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_receipt_recorded_currentness_invalid"
        )
    by_metric_id = {item.metric_id: item for item in metric_rows}
    if len(by_metric_id) != len(metric_rows):
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_metric_result_duplicate"
        )
    if any(
        item.revision_digest != row.revision_digest
        or item.subject_content_digest != row.subject_content_digest
        or item.receipt_digest != row.receipt_digest
        for item in metric_rows
    ):
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_metric_result_fence_mismatch"
        )
    # Receipt digests preserve authorial metric order.  The revision snapshot
    # is consulted by the adapter before this helper and rows arrive ordered by
    # that immutable metric sequence.
    metric_results = tuple(
        _metric_from_row(item, subject_edition=row.validation_edition)
        for item in metric_rows
    )
    try:
        receipt = SemanticGuidelineAssessmentReceipt(
            receipt_id=row.receipt_id,
            subject=PolicySubjectRef(
                board_id=row.board_id,
                entity_type=PolicyEntityType(row.subject_type),
                subject_id=row.subject_id,
                subject_version=row.subject_version,
                subject_edition=row.validation_edition,
            ),
            subject_content_digest=row.subject_content_digest,
            last_semantic_editor_id=row.last_semantic_editor_id,
            binding_id=row.binding_id,
            binding_revision=row.binding_revision,
            guideline_id=row.guideline_id,
            guideline_revision_id=row.revision_id,
            guideline_revision_digest=row.revision_digest,
            binding_configuration_digest=row.configuration_digest,
            policy_set_digest=row.policy_set_digest,
            binding_head_digest=row.binding_head_digest,
            input_digest=row.input_digest,
            request_digest=row.request_digest,
            idempotency_key=row.idempotency_key,
            enforcement=GuidelineEnforcement(row.enforcement),
            assessor=SemanticAssessmentAssessor(
                agent_id=row.assessor_agent_id,
                model_id=row.assessor_model_id,
            ),
            assessor_independent=row.assessor_independent,
            confidence=row.confidence,
            minimum_confidence=row.minimum_confidence,
            confidence_admissible=row.confidence_admissible,
            state=SemanticAssessmentState(row.state),
            currentness=PolicyCurrentness.CURRENT,
            metric_results=metric_results,
            recorded_at=_utc(row.assessed_at),
            receipt_digest=row.receipt_digest,
        )
    except (TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_receipt_snapshot_invalid"
        ) from exc
    if (
        receipt.metric_count != row.metric_result_count
        or receipt.failed_metric_count != row.failed_metric_count
    ):
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_receipt_count_mismatch"
        )
    return receipt


class CommunitySqlAlchemySemanticGuidelineAssessment:
    """Transaction-bound semantic assessment authority."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _q_and_a(
        self,
        *,
        entity_type: PolicyEntityType,
        subject_id: str,
        lock: bool,
    ) -> tuple[dict[str, object], ...]:
        model_and_owner = {
            PolicyEntityType.IDEATION: (
                IdeationQAItem,
                IdeationQAItem.ideation_id,
            ),
            PolicyEntityType.REFINEMENT: (
                RefinementQAItem,
                RefinementQAItem.refinement_id,
            ),
            PolicyEntityType.SPEC: (
                SpecQAItem,
                SpecQAItem.spec_id,
            ),
            PolicyEntityType.SPRINT: (
                SprintQAItem,
                SprintQAItem.sprint_id,
            ),
            PolicyEntityType.CARD: (
                QAItem,
                QAItem.card_id,
            ),
        }.get(entity_type)
        if model_and_owner is None:
            return ()
        model, owner_column = model_and_owner
        statement = (
            select(model)
            .where(owner_column == subject_id)
            .order_by(model.id.asc())
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return tuple(
            _qa_payload(row)
            for row in (
                await self._session.execute(statement)
            ).scalars().all()
        )

    async def _resource_refs(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        row: Any,
        lock: bool,
    ) -> tuple[dict[str, object], ...]:
        refs: list[dict[str, object]] = []
        knowledge_model_and_owner = {
            PolicyEntityType.IDEATION: (
                IdeationKnowledgeBase,
                IdeationKnowledgeBase.ideation_id,
            ),
            PolicyEntityType.REFINEMENT: (
                RefinementKnowledgeBase,
                RefinementKnowledgeBase.refinement_id,
            ),
            PolicyEntityType.SPEC: (
                SpecKnowledgeBase,
                SpecKnowledgeBase.spec_id,
            ),
        }.get(entity_type)
        if knowledge_model_and_owner is not None:
            model, owner_column = knowledge_model_and_owner
            statement = (
                select(model)
                .where(owner_column == subject_id)
                .order_by(model.id.asc())
                .execution_options(populate_existing=True)
            )
            if lock:
                statement = statement.with_for_update()
            for knowledge in (
                await self._session.execute(statement)
            ).scalars().all():
                source_ref = (
                    knowledge.source_kb_id
                    or knowledge.source_id
                    or knowledge.root_source_kb_id
                )
                refs.append(
                    _resource_ref(
                        resource_type="knowledge",
                        resource_id=knowledge.id,
                        resource_version=max(
                            1,
                            int(knowledge.source_version or 1),
                        ),
                        semantic_payload=_knowledge_payload(knowledge),
                        source_ref=source_ref,
                    )
                )
        elif entity_type is PolicyEntityType.CARD:
            for value in getattr(row, "knowledge_bases", None) or ():
                if not isinstance(value, dict):
                    raise GuidelinePolicyDigestConflict(
                        "semantic_subject_knowledge_snapshot_invalid"
                    )
                resource_id = _required_resource_id(
                    value.get("id"),
                    "semantic_subject_knowledge_id_required",
                )
                raw_version = value.get("version") or value.get(
                    "source_version"
                )
                resource_version = (
                    int(raw_version)
                    if isinstance(raw_version, int)
                    and not isinstance(raw_version, bool)
                    and raw_version > 0
                    else 1
                )
                source_ref_value = (
                    value.get("source_kb_id")
                    or value.get("source_id")
                    or value.get("root_source_kb_id")
                )
                refs.append(
                    _resource_ref(
                        resource_type="knowledge",
                        resource_id=resource_id,
                        resource_version=resource_version,
                        semantic_payload=_embedded_knowledge_payload(value),
                        source_ref=(
                            str(source_ref_value)
                            if source_ref_value is not None
                            else None
                        ),
                    )
                )

        architecture_owner_column = {
            PolicyEntityType.IDEATION: ArchitectureDesign.ideation_id,
            PolicyEntityType.REFINEMENT: ArchitectureDesign.refinement_id,
            PolicyEntityType.SPEC: ArchitectureDesign.spec_id,
            PolicyEntityType.CARD: ArchitectureDesign.card_id,
        }.get(entity_type)
        if architecture_owner_column is not None:
            statement = (
                select(ArchitectureDesign)
                .where(
                    ArchitectureDesign.board_id == board_id,
                    ArchitectureDesign.parent_type == entity_type.value,
                    architecture_owner_column == subject_id,
                )
                .order_by(ArchitectureDesign.id.asc())
                .execution_options(populate_existing=True)
            )
            if lock:
                statement = statement.with_for_update()
            for architecture in (
                await self._session.execute(statement)
            ).scalars().all():
                payload_statement = (
                    select(ArchitectureDiagramPayload)
                    .where(
                        ArchitectureDiagramPayload.design_id
                        == architecture.id
                    )
                    .order_by(
                        ArchitectureDiagramPayload.diagram_id.asc(),
                        ArchitectureDiagramPayload.id.asc(),
                    )
                    .execution_options(populate_existing=True)
                )
                if lock:
                    payload_statement = payload_statement.with_for_update()
                payload_hashes = [
                    {
                        "diagram_id": payload.diagram_id,
                        "format": payload.format,
                        "content_hash": payload.content_hash,
                    }
                    for payload in (
                        await self._session.execute(payload_statement)
                    ).scalars().all()
                ]
                semantic_payload = {
                    "id": architecture.id,
                    "title": architecture.title,
                    "global_description": architecture.global_description,
                    "entities": _resource_value(
                        architecture.entities or []
                    ),
                    "interfaces": _resource_value(
                        architecture.interfaces or []
                    ),
                    "diagrams": _resource_value(
                        architecture.diagrams or []
                    ),
                    "version": architecture.version,
                    "source_ref": architecture.source_ref,
                    "source_version": architecture.source_version,
                    "source_design_id": architecture.source_design_id,
                    "diagram_payload_content_hashes": payload_hashes,
                }
                refs.append(
                    _resource_ref(
                        resource_type="architecture",
                        resource_id=architecture.id,
                        resource_version=max(1, int(architecture.version)),
                        semantic_payload=semantic_payload,
                        source_ref=(
                            architecture.source_ref
                            or architecture.source_design_id
                        ),
                    )
                )

        for value in getattr(row, "screen_mockups", None) or ():
            if not isinstance(value, dict):
                raise GuidelinePolicyDigestConflict(
                    "semantic_subject_mockup_snapshot_invalid"
                )
            resource_id = _required_resource_id(
                value.get("id"),
                "semantic_subject_mockup_id_required",
            )
            raw_version = value.get("version") or value.get(
                "source_version"
            )
            resource_version = (
                int(raw_version)
                if isinstance(raw_version, int)
                and not isinstance(raw_version, bool)
                and raw_version > 0
                else 1
            )
            source_ref_value = (
                value.get("source_ref")
                or value.get("source_id")
                or value.get("source_mockup_id")
            )
            refs.append(
                _resource_ref(
                    resource_type="mockup",
                    resource_id=resource_id,
                    resource_version=resource_version,
                    semantic_payload=_mockup_payload(value),
                    source_ref=(
                        str(source_ref_value)
                        if source_ref_value is not None
                        else None
                    ),
                )
            )
        identities = tuple(
            (str(item["resource_type"]), str(item["resource_id"]))
            for item in refs
        )
        if len(set(identities)) != len(identities):
            raise GuidelinePolicyDigestConflict(
                "semantic_subject_resource_identity_duplicate"
            )
        return tuple(
            sorted(
                refs,
                key=lambda item: (
                    str(item["resource_type"]),
                    str(item["resource_id"]),
                    int(item["resource_version"]),
                ),
            )
        )

    @staticmethod
    def _revision_from_rows(
        legacy: GuidelineRevisionRow,
        semantic: SemanticGuidelineRevisionRow,
    ) -> GuidelineRevision:
        if (
            semantic.authority_state
            not in {"native", "legacy_context_only"}
            or not isinstance(semantic.metrics, list)
            or not isinstance(legacy.tags, list)
            or (
                semantic.authority_state == "legacy_context_only"
                and semantic.metrics
            )
        ):
            raise GuidelinePolicyDigestConflict(
                "semantic_guideline_revision_not_executable"
            )
        try:
            metrics = tuple(
                GuidelineMetric(
                    metric_id=item["metric_id"],
                    code=item["code"],
                    title=item["title"],
                    description=item["description"],
                    evaluation_rubric=item["evaluation_rubric"],
                    target_entity_types=tuple(
                        PolicyEntityType(target)
                        for target in item["target_entity_types"]
                    ),
                    direction=GuidelineMetricDirection(item["direction"]),
                    default_threshold=item["default_threshold"],
                )
                for item in semantic.metrics
            )
            return GuidelineRevision(
                revision_id=legacy.revision_id,
                guideline_id=legacy.guideline_id,
                revision_number=legacy.revision_number,
                semantic_version=legacy.semantic_version,
                title=legacy.title,
                content=legacy.content,
                metrics=metrics,
                created_by=legacy.created_by,
                created_at=_utc(legacy.created_at),
                revision_digest=semantic.revision_digest,
                parent_revision_id=legacy.parent_revision_id,
                tags=tuple(legacy.tags),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_guideline_revision_snapshot_invalid"
            ) from exc

    @staticmethod
    def _binding_from_rows(
        legacy: GuidelineBoardBindingRow,
        semantic: SemanticGuidelineBindingConfigurationRow,
    ) -> BoardGuidelineBinding:
        if (
            semantic.binding_id != legacy.binding_id
            or semantic.binding_revision != legacy.binding_revision
            or semantic.board_id != legacy.board_id
            or semantic.guideline_id != legacy.guideline_id
            or semantic.revision_id != legacy.revision_id
        ):
            raise GuidelinePolicyDigestConflict(
                "semantic_guideline_binding_authority_mismatch"
            )
        try:
            return BoardGuidelineBinding(
                binding_id=legacy.binding_id,
                board_id=legacy.board_id,
                guideline_id=legacy.guideline_id,
                revision_id=legacy.revision_id,
                semantic_version=legacy.semantic_version,
                revision_digest=semantic.revision_digest,
                priority=legacy.priority,
                binding_revision=legacy.binding_revision,
                adopted_by=legacy.adopted_by,
                adopted_at=_utc(legacy.adopted_at),
                enforcement=GuidelineEnforcement(semantic.enforcement),
                minimum_confidence=semantic.minimum_confidence,
                metric_threshold_overrides=dict(
                    semantic.metric_threshold_overrides
                ),
                configuration_digest=semantic.configuration_digest,
                state=GuidelineBindingState(legacy.state),
                source_kind=GuidelineBindingProvenance(
                    legacy.binding_origin
                ),
            )
        except (TypeError, ValueError) as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_guideline_binding_snapshot_invalid"
            ) from exc

    async def _authority_bundle(
        self,
        *,
        board_id: str,
        lock: bool,
    ) -> tuple[
        tuple[BoardGuidelineBinding, ...],
        tuple[GuidelineRevision, ...],
    ]:
        """Rebuild the exact semantic binding heads from relational authority.

        Legacy bindings without a semantic configuration are deliberately
        ignored: migration classifies them as inert and never synthesizes an
        executable configuration.
        """

        statement = (
            select(GuidelineBoardBindingRow)
            .where(GuidelineBoardBindingRow.board_id == board_id)
            .order_by(
                GuidelineBoardBindingRow.guideline_id.asc(),
                GuidelineBoardBindingRow.binding_revision.desc(),
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        legacy_rows = tuple(
            (await self._session.execute(statement)).scalars().all()
        )
        latest_by_guideline: dict[str, GuidelineBoardBindingRow] = {}
        for row in legacy_rows:
            latest_by_guideline.setdefault(row.guideline_id, row)

        bindings: list[BoardGuidelineBinding] = []
        revisions: list[GuidelineRevision] = []
        for legacy_binding in latest_by_guideline.values():
            semantic_statement = select(
                SemanticGuidelineBindingConfigurationRow
            ).where(
                SemanticGuidelineBindingConfigurationRow.binding_id
                == legacy_binding.binding_id,
                SemanticGuidelineBindingConfigurationRow.binding_revision
                == legacy_binding.binding_revision,
            )
            if lock:
                semantic_statement = semantic_statement.with_for_update()
            semantic_binding = (
                await self._session.execute(semantic_statement)
            ).scalar_one_or_none()
            if semantic_binding is None:
                continue
            binding = self._binding_from_rows(
                legacy_binding,
                semantic_binding,
            )
            bindings.append(binding)
            if binding.state is GuidelineBindingState.UNLINKED:
                continue
            semantic_revision_statement = select(
                SemanticGuidelineRevisionRow
            ).where(
                SemanticGuidelineRevisionRow.guideline_id
                == binding.guideline_id,
                SemanticGuidelineRevisionRow.revision_id
                == binding.revision_id,
                SemanticGuidelineRevisionRow.revision_digest
                == binding.revision_digest,
            )
            legacy_revision_statement = select(GuidelineRevisionRow).where(
                GuidelineRevisionRow.guideline_id == binding.guideline_id,
                GuidelineRevisionRow.revision_id == binding.revision_id,
            )
            if lock:
                semantic_revision_statement = (
                    semantic_revision_statement.with_for_update()
                )
                legacy_revision_statement = (
                    legacy_revision_statement.with_for_update()
                )
            semantic_revision = (
                await self._session.execute(semantic_revision_statement)
            ).scalar_one_or_none()
            legacy_revision = (
                await self._session.execute(legacy_revision_statement)
            ).scalar_one_or_none()
            if semantic_revision is None or legacy_revision is None:
                raise GuidelinePolicyDigestConflict(
                    "semantic_guideline_bound_revision_missing"
                )
            revisions.append(
                self._revision_from_rows(
                    legacy_revision,
                    semantic_revision,
                )
            )
        return tuple(bindings), tuple(revisions)

    @staticmethod
    def _validation_scope_payload(
        bindings: tuple[BoardGuidelineBinding, ...],
    ) -> list[dict[str, object]]:
        return [
            {
                "binding_id": binding.binding_id,
                "binding_revision": binding.binding_revision,
                "guideline_id": binding.guideline_id,
                "revision_id": binding.revision_id,
                "revision_digest": binding.revision_digest,
                "configuration_digest": binding.configuration_digest,
                "state": binding.state.value,
                "enforcement": binding.enforcement.value,
            }
            for binding in bindings
        ]

    async def _authority_bundle_from_validation_scope(
        self,
        scope: SemanticGuidelineValidationScopeRow,
        *,
        lock: bool,
    ) -> tuple[
        tuple[BoardGuidelineBinding, ...],
        tuple[GuidelineRevision, ...],
    ]:
        if not isinstance(scope.scope_json, list):
            raise GuidelinePolicyDigestConflict(
                "semantic_validation_scope_corrupt"
            )
        bindings: list[BoardGuidelineBinding] = []
        revisions: list[GuidelineRevision] = []
        seen_bindings: set[tuple[str, int]] = set()
        for raw in scope.scope_json:
            if not isinstance(raw, dict):
                raise GuidelinePolicyDigestConflict(
                    "semantic_validation_scope_corrupt"
                )
            try:
                binding_id = str(raw["binding_id"])
                binding_revision = int(raw["binding_revision"])
                guideline_id = str(raw["guideline_id"])
                revision_id = str(raw["revision_id"])
                revision_digest = str(raw["revision_digest"])
                configuration_digest = str(raw["configuration_digest"])
            except (KeyError, TypeError, ValueError) as exc:
                raise GuidelinePolicyDigestConflict(
                    "semantic_validation_scope_corrupt"
                ) from exc
            identity = (binding_id, binding_revision)
            if identity in seen_bindings:
                raise GuidelinePolicyDigestConflict(
                    "semantic_validation_scope_corrupt"
                )
            seen_bindings.add(identity)
            legacy_statement = select(GuidelineBoardBindingRow).where(
                GuidelineBoardBindingRow.board_id == scope.board_id,
                GuidelineBoardBindingRow.binding_id == binding_id,
                GuidelineBoardBindingRow.binding_revision
                == binding_revision,
            )
            semantic_statement = select(
                SemanticGuidelineBindingConfigurationRow
            ).where(
                SemanticGuidelineBindingConfigurationRow.board_id
                == scope.board_id,
                SemanticGuidelineBindingConfigurationRow.binding_id
                == binding_id,
                SemanticGuidelineBindingConfigurationRow.binding_revision
                == binding_revision,
                SemanticGuidelineBindingConfigurationRow.configuration_digest
                == configuration_digest,
            )
            if lock:
                legacy_statement = legacy_statement.with_for_update()
                semantic_statement = semantic_statement.with_for_update()
            legacy = (
                await self._session.execute(legacy_statement)
            ).scalar_one_or_none()
            semantic = (
                await self._session.execute(semantic_statement)
            ).scalar_one_or_none()
            if legacy is None or semantic is None:
                raise GuidelinePolicyDigestConflict(
                    "semantic_validation_scope_authority_missing"
                )
            binding = self._binding_from_rows(legacy, semantic)
            if (
                binding.guideline_id != guideline_id
                or binding.revision_id != revision_id
                or binding.revision_digest != revision_digest
                or binding.configuration_digest != configuration_digest
            ):
                raise GuidelinePolicyDigestConflict(
                    "semantic_validation_scope_authority_mismatch"
                )
            bindings.append(binding)
            if binding.state is GuidelineBindingState.UNLINKED:
                continue
            semantic_revision_statement = select(
                SemanticGuidelineRevisionRow
            ).where(
                SemanticGuidelineRevisionRow.guideline_id == guideline_id,
                SemanticGuidelineRevisionRow.revision_id == revision_id,
                SemanticGuidelineRevisionRow.revision_digest
                == revision_digest,
            )
            legacy_revision_statement = select(GuidelineRevisionRow).where(
                GuidelineRevisionRow.guideline_id == guideline_id,
                GuidelineRevisionRow.revision_id == revision_id,
            )
            if lock:
                semantic_revision_statement = (
                    semantic_revision_statement.with_for_update()
                )
                legacy_revision_statement = (
                    legacy_revision_statement.with_for_update()
                )
            semantic_revision = (
                await self._session.execute(semantic_revision_statement)
            ).scalar_one_or_none()
            legacy_revision = (
                await self._session.execute(legacy_revision_statement)
            ).scalar_one_or_none()
            if semantic_revision is None or legacy_revision is None:
                raise GuidelinePolicyDigestConflict(
                    "semantic_validation_scope_revision_missing"
                )
            revisions.append(
                self._revision_from_rows(
                    legacy_revision,
                    semantic_revision,
                )
            )
        resolved = (tuple(bindings), tuple(revisions))
        if (
            semantic_policy_set_digest_v1(*resolved)
            != scope.policy_set_digest
            or semantic_binding_head_digest_v1(resolved[0])
            != scope.binding_head_digest
        ):
            raise GuidelinePolicyDigestConflict(
                "semantic_validation_scope_digest_mismatch"
            )
        return resolved

    async def freeze_validation_policy_scope(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        subject_edition: int,
        lock: bool = True,
    ) -> tuple[
        tuple[BoardGuidelineBinding, ...],
        tuple[GuidelineRevision, ...],
    ]:
        """Resolve the immutable governance set for one validation edition."""

        if entity_type not in {
            PolicyEntityType.IDEATION,
            PolicyEntityType.REFINEMENT,
            PolicyEntityType.SPEC,
        }:
            raise GuidelinePolicySubjectConflict(
                "semantic_validation_scope_subject_type_invalid"
            )
        if not isinstance(subject_edition, int) or subject_edition < 1:
            raise GuidelinePolicyEditionConflict(
                "guideline_policy_edition_conflict"
            )
        if lock:
            await lock_policy_board(self._session, board_id=board_id)
        identity = (
            board_id,
            entity_type.value,
            subject_id,
            subject_edition,
        )
        scope = await self._session.get(
            SemanticGuidelineValidationScopeRow,
            identity,
        )
        if scope is not None:
            return await self._authority_bundle_from_validation_scope(
                scope,
                lock=lock,
            )
        live_subject_edition = await self._subject_edition(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
        )
        if live_subject_edition != subject_edition:
            raise GuidelinePolicyEditionConflict(
                "guideline_policy_edition_conflict"
            )
        bindings, revisions = await self._authority_bundle(
            board_id=board_id,
            lock=lock,
        )
        scope = SemanticGuidelineValidationScopeRow(
            board_id=board_id,
            subject_type=entity_type.value,
            subject_id=subject_id,
            validation_edition=subject_edition,
            scope_json=self._validation_scope_payload(bindings),
            policy_set_digest=semantic_policy_set_digest_v1(
                bindings,
                revisions,
            ),
            binding_head_digest=semantic_binding_head_digest_v1(bindings),
            captured_at=datetime.now(timezone.utc),
        )
        self._session.add(scope)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_validation_scope_insert_conflict"
            ) from exc
        return bindings, revisions

    async def _authority_bundle_for_subject(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        subject_edition: int | None,
        lock: bool,
    ) -> tuple[
        tuple[BoardGuidelineBinding, ...],
        tuple[GuidelineRevision, ...],
    ]:
        if (
            subject_edition is None
            or entity_type
            not in {
                PolicyEntityType.IDEATION,
                PolicyEntityType.REFINEMENT,
                PolicyEntityType.SPEC,
            }
        ):
            return await self._authority_bundle(
                board_id=board_id,
                lock=lock,
            )
        return await self.freeze_validation_policy_scope(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            subject_edition=subject_edition,
            lock=lock,
        )

    async def semantic_current_fences(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType | None = None,
        subject_id: str | None = None,
        subject_edition: int | None = None,
        lock: bool = False,
    ) -> tuple[str, str]:
        """Return ``(policy_set_digest, binding_head_digest)`` from DB heads."""

        if entity_type is None or subject_id is None:
            bindings, revisions = await self._authority_bundle(
                board_id=board_id,
                lock=lock,
            )
        else:
            bindings, revisions = await self._authority_bundle_for_subject(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                subject_edition=subject_edition,
                lock=lock,
            )
        return (
            semantic_policy_set_digest_v1(bindings, revisions),
            semantic_binding_head_digest_v1(bindings),
        )

    async def _raw_subject(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        lock: bool,
    ) -> _SemanticSubjectMaterial | None:
        if not isinstance(entity_type, PolicyEntityType):
            raise GuidelinePolicySubjectConflict(
                "semantic_assessment_subject_type_invalid"
            )
        if lock:
            await lock_policy_board(self._session, board_id=board_id)

        if entity_type is PolicyEntityType.TEST_SCENARIO:
            statement = (
                select(Spec)
                .where(Spec.board_id == board_id)
                .order_by(Spec.id.asc())
                .execution_options(populate_existing=True)
            )
            if lock:
                statement = statement.with_for_update()
            specs = tuple(
                (await self._session.execute(statement)).scalars().all()
            )
            matches: list[tuple[Spec, dict[str, object]]] = []
            for spec in specs:
                for scenario in spec.test_scenarios or ():
                    if (
                        isinstance(scenario, dict)
                        and str(scenario.get("id") or "").strip()
                        == subject_id
                    ):
                        matches.append((spec, scenario))
            if len(matches) > 1:
                raise GuidelinePolicySubjectConflict(
                    "semantic_assessment_test_scenario_duplicate"
                )
            if not matches:
                return None
            spec, scenario = matches[0]
            payload = {
                key: value
                for key, value in scenario.items()
                if key
                not in {
                    "status",
                    "linked_task_ids",
                    "evidence",
                    "latest_evidence",
                    "execution_attestation",
                    "execution_receipt",
                }
            }
            return _SemanticSubjectMaterial(
                subject_version=int(spec.test_scenario_policy_epoch),
                subject_edition=None,
                artifact=payload,
                q_and_a=(),
                resource_refs=(),
            )

        model_by_type = {
            PolicyEntityType.IDEATION: Ideation,
            PolicyEntityType.REFINEMENT: Refinement,
            PolicyEntityType.SPEC: Spec,
            PolicyEntityType.SPRINT: Sprint,
            PolicyEntityType.CARD: Card,
        }
        model = model_by_type[entity_type]
        statement = (
            select(model)
            .where(model.id == subject_id, model.board_id == board_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None
        version_field = (
            "policy_version"
            if entity_type is PolicyEntityType.CARD
            else "version"
        )
        return _SemanticSubjectMaterial(
            subject_version=int(getattr(row, version_field)),
            subject_edition=(
                int(row.edition)
                if entity_type
                in {
                    PolicyEntityType.IDEATION,
                    PolicyEntityType.REFINEMENT,
                    PolicyEntityType.SPEC,
                }
                else None
            ),
            artifact=_semantic_subject_payload(
                entity_type=entity_type,
                row=row,
            ),
            q_and_a=await self._q_and_a(
                entity_type=entity_type,
                subject_id=subject_id,
                lock=lock,
            ),
            resource_refs=await self._resource_refs(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                row=row,
                lock=lock,
            ),
        )

    async def _subject_edition(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ) -> int | None:
        """Read the human lifecycle identity without loading cognitive inputs."""

        model = {
            PolicyEntityType.IDEATION: Ideation,
            PolicyEntityType.REFINEMENT: Refinement,
            PolicyEntityType.SPEC: Spec,
        }.get(entity_type)
        if model is None:
            return None
        edition = (
            await self._session.execute(
                select(model.edition).where(
                    model.id == subject_id,
                    model.board_id == board_id,
                )
            )
        ).scalar_one_or_none()
        return None if edition is None else int(edition)

    async def resolve_policy_subject_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        lock: bool = False,
    ) -> PolicySubjectSnapshot | None:
        raw = await self._raw_subject(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=lock,
        )
        if raw is None:
            return None
        subject_version = raw.subject_version
        content_digest = _semantic_digest(entity_type, raw)
        head_statement = select(SemanticSubjectVersionRow).where(
            SemanticSubjectVersionRow.board_id == board_id,
            SemanticSubjectVersionRow.subject_type == entity_type.value,
            SemanticSubjectVersionRow.subject_id == subject_id,
        )
        if lock:
            head_statement = head_statement.with_for_update()
        head = (
            await self._session.execute(head_statement)
        ).scalar_one_or_none()
        last_editor = LEGACY_UNKNOWN_SEMANTIC_EDITOR_ID
        if (
            head is not None
            and head.subject_version == subject_version
            and head.content_digest == content_digest
        ):
            last_editor = head.last_semantic_editor_id
        return PolicySubjectSnapshot(
            subject=PolicySubjectRef(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                subject_version=subject_version,
                subject_edition=raw.subject_edition,
            ),
            content_digest=content_digest,
            last_semantic_editor_id=last_editor,
            captured_at=datetime.now(timezone.utc),
        )

    async def _resolve_policy_subject_status(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ) -> str | None:
        """Read the lifecycle fence without adding status to semantic content."""

        if entity_type is PolicyEntityType.TEST_SCENARIO:
            statement = (
                select(Spec)
                .where(Spec.board_id == board_id)
                .order_by(Spec.id.asc())
                .with_for_update()
            )
            specs = tuple(
                (await self._session.execute(statement)).scalars().all()
            )
            matches = tuple(
                scenario
                for spec in specs
                for scenario in (spec.test_scenarios or ())
                if (
                    isinstance(scenario, dict)
                    and str(scenario.get("id") or "").strip() == subject_id
                )
            )
            if len(matches) > 1:
                raise GuidelinePolicySubjectConflict(
                    "semantic_assessment_test_scenario_duplicate"
                )
            if not matches:
                return None
            raw_status = matches[0].get("status")
        else:
            model_by_type = {
                PolicyEntityType.IDEATION: Ideation,
                PolicyEntityType.REFINEMENT: Refinement,
                PolicyEntityType.SPEC: Spec,
                PolicyEntityType.SPRINT: Sprint,
                PolicyEntityType.CARD: Card,
            }
            model = model_by_type.get(entity_type)
            if model is None:
                raise GuidelinePolicySubjectConflict(
                    "semantic_assessment_subject_type_invalid"
                )
            row = (
                await self._session.execute(
                    select(model)
                    .where(
                        model.id == subject_id,
                        model.board_id == board_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            raw_status = row.status
        normalized = getattr(raw_status, "value", raw_status)
        if not isinstance(normalized, str) or not normalized.strip():
            raise GuidelinePolicySubjectConflict(
                "policy_transition_subject_status_invalid"
            )
        return normalized.strip().lower()

    async def record_semantic_subject_mutation(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        changed_at: datetime,
    ) -> PolicySubjectSnapshot:
        """Record the authoritative editor after an entity semantic mutation.

        Entity application services call this in the same transaction as the
        semantic edit.  Historical subjects without such an event deliberately
        resolve to ``legacy_unknown`` and therefore cannot satisfy blocking
        reviewer separation.
        """

        request_digest = _require_sha256(
            request_digest,
            "semantic_subject_mutation_request_digest_invalid",
        )
        changed_at = _utc(changed_at)
        replay = (
            await self._session.execute(
                select(SemanticSubjectVersionEventRow).where(
                    SemanticSubjectVersionEventRow.board_id == board_id,
                    SemanticSubjectVersionEventRow.idempotency_key
                    == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if (
                replay.request_digest != request_digest
                or replay.subject_type != entity_type.value
                or replay.subject_id != subject_id
                or replay.last_semantic_editor_id != actor_id
            ):
                raise GuidelinePolicyIdempotencyConflict(
                    "semantic_subject_mutation_idempotency_conflict"
                )
            return PolicySubjectSnapshot(
                subject=PolicySubjectRef(
                    board_id=board_id,
                    entity_type=entity_type,
                    subject_id=subject_id,
                    subject_version=replay.subject_version,
                    subject_edition=await self._subject_edition(
                        board_id=board_id,
                        entity_type=entity_type,
                        subject_id=subject_id,
                    ),
                ),
                content_digest=replay.content_digest,
                last_semantic_editor_id=replay.last_semantic_editor_id,
                captured_at=_utc(replay.changed_at),
            )

        raw = await self._raw_subject(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=True,
        )
        if raw is None:
            raise GuidelinePolicySubjectConflict(
                "semantic_subject_mutation_subject_not_found"
            )
        subject_version = raw.subject_version
        content_digest = _semantic_digest(entity_type, raw)
        head = (
            await self._session.execute(
                select(SemanticSubjectVersionRow)
                .where(
                    SemanticSubjectVersionRow.board_id == board_id,
                    SemanticSubjectVersionRow.subject_type
                    == entity_type.value,
                    SemanticSubjectVersionRow.subject_id == subject_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        head_revision = 1 if head is None else head.head_revision + 1
        predecessor_event_id = None if head is None else head.last_event_id
        event_id = canonical_sha256(
            {
                "contract": "semantic-subject-version-event/v1",
                "board_id": board_id,
                "subject_type": entity_type.value,
                "subject_id": subject_id,
                "subject_version": subject_version,
                "content_digest": content_digest,
                "actor_id": actor_id,
                "head_revision": head_revision,
                "predecessor_event_id": predecessor_event_id,
                "idempotency_key": idempotency_key,
                "request_digest": request_digest,
            }
        )
        if head is None:
            head = SemanticSubjectVersionRow(
                board_id=board_id,
                subject_type=entity_type.value,
                subject_id=subject_id,
                subject_version=subject_version,
                content_digest=content_digest,
                last_semantic_editor_id=actor_id,
                editor_source="authoritative",
                head_revision=head_revision,
                last_event_id=event_id,
                updated_at=changed_at,
            )
            self._session.add(head)
        else:
            head.subject_version = subject_version
            head.content_digest = content_digest
            head.last_semantic_editor_id = actor_id
            head.editor_source = "authoritative"
            head.head_revision = head_revision
            head.last_event_id = event_id
            head.updated_at = changed_at
        event = SemanticSubjectVersionEventRow(
                event_id=event_id,
                predecessor_event_id=predecessor_event_id,
                board_id=board_id,
                subject_type=entity_type.value,
                subject_id=subject_id,
                subject_version=subject_version,
                content_digest=content_digest,
                last_semantic_editor_id=actor_id,
                editor_source="authoritative",
                event_type="semantic_mutation",
                head_revision=head_revision,
                changed_at=changed_at,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        try:
            # The head FK is deferred and the event guard deliberately verifies
            # the already-staged head.  Flush in that order instead of relying
            # on SQLAlchemy's circular-FK scheduling.
            await self._session.flush((head,))
            self._session.add(event)
            await self._session.flush((event,))
        except IntegrityError as exc:
            raise GuidelinePolicySubjectConflict(
                "semantic_subject_mutation_conflict"
            ) from exc
        return PolicySubjectSnapshot(
            subject=PolicySubjectRef(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                subject_version=subject_version,
                subject_edition=raw.subject_edition,
            ),
            content_digest=content_digest,
            last_semantic_editor_id=actor_id,
            captured_at=changed_at,
        )

    async def _metric_rows(
        self,
        row: SemanticGuidelineAssessmentReceiptRow,
    ) -> tuple[SemanticGuidelineMetricResultRow, ...]:
        revision = (
            await self._session.execute(
                select(SemanticGuidelineRevisionRow).where(
                    SemanticGuidelineRevisionRow.guideline_id
                    == row.guideline_id,
                    SemanticGuidelineRevisionRow.revision_id
                    == row.revision_id,
                    SemanticGuidelineRevisionRow.revision_digest
                    == row.revision_digest,
                )
            )
        ).scalar_one_or_none()
        if revision is None or not isinstance(revision.metrics, list):
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_revision_snapshot_missing"
            )
        metric_order = {
            str(metric.get("metric_id")): index
            for index, metric in enumerate(revision.metrics)
            if isinstance(metric, dict)
        }
        rows = tuple(
            (
                await self._session.execute(
                    select(SemanticGuidelineMetricResultRow).where(
                        SemanticGuidelineMetricResultRow.receipt_id
                        == row.receipt_id
                    )
                )
            )
            .scalars()
            .all()
        )
        try:
            return tuple(
                sorted(rows, key=lambda item: metric_order[item.metric_id])
            )
        except KeyError as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_metric_result_unknown"
            ) from exc

    async def _validate_finding_rows(
        self,
        receipt: SemanticGuidelineAssessmentReceipt,
    ) -> None:
        rows = tuple(
            (
                await self._session.execute(
                    select(SemanticGuidelineFindingRow).where(
                        SemanticGuidelineFindingRow.receipt_id
                        == receipt.receipt_id
                    )
                )
            )
            .scalars()
            .all()
        )
        expected = {
            finding.metric_result_id: finding
            for finding in project_semantic_metric_findings(receipt)
        }
        if len(rows) != len(expected):
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_finding_count_mismatch"
            )
        seen_results: set[str] = set()
        for row in rows:
            finding = expected.get(row.metric_result_id)
            if finding is None or row.metric_result_id in seen_results:
                raise GuidelinePolicyDigestConflict(
                    "semantic_assessment_finding_result_invalid"
                )
            seen_results.add(row.metric_result_id)
            if (
                _finding_from_row(
                    row,
                    subject_edition=receipt.subject.subject_edition,
                )
                != finding
            ):
                raise GuidelinePolicyDigestConflict(
                    "semantic_assessment_finding_snapshot_invalid"
                )

    async def _result_from_row(
        self,
        row: SemanticGuidelineAssessmentReceiptRow,
        *,
        replayed: bool,
    ) -> SemanticGuidelineAssessmentResult:
        receipt = _receipt_from_rows(
            row,
            await self._metric_rows(row),
        )
        await self._validate_finding_rows(receipt)
        return SemanticGuidelineAssessmentResult(
            input_digest=receipt.input_digest,
            request_digest=receipt.request_digest,
            receipt=receipt,
            replayed=replayed,
        )

    async def get_semantic_assessment_result_by_idempotency(
        self,
        *,
        board_id: str,
        binding_id: str,
        idempotency_key: str,
    ) -> SemanticGuidelineAssessmentResult | None:
        row = (
            await self._session.execute(
                select(SemanticGuidelineAssessmentReceiptRow).where(
                    SemanticGuidelineAssessmentReceiptRow.board_id == board_id,
                    SemanticGuidelineAssessmentReceiptRow.binding_id
                    == binding_id,
                    SemanticGuidelineAssessmentReceiptRow.idempotency_key
                    == idempotency_key,
                    SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                )
            )
        ).scalar_one_or_none()
        return (
            None
            if row is None
            else await self._result_from_row(row, replayed=True)
        )

    async def save_semantic_assessment_result(
        self,
        *,
        result: SemanticGuidelineAssessmentResult,
        request_digest: str,
    ) -> SemanticGuidelineAssessmentResult:
        if not isinstance(result, SemanticGuidelineAssessmentResult):
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_result_invalid"
            )
        request_digest = _require_sha256(
            request_digest,
            "semantic_assessment_request_digest_invalid",
        )
        receipt = result.receipt
        if (
            request_digest != result.request_digest
            or request_digest != receipt.request_digest
        ):
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_request_digest_mismatch"
            )
        replay_row = (
            await self._session.execute(
                select(SemanticGuidelineAssessmentReceiptRow).where(
                    SemanticGuidelineAssessmentReceiptRow.board_id
                    == receipt.subject.board_id,
                    SemanticGuidelineAssessmentReceiptRow.idempotency_key
                    == receipt.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if replay_row is not None:
            if (
                replay_row.request_digest != request_digest
                or replay_row.input_digest != result.input_digest
                or replay_row.binding_id != receipt.binding_id
            ):
                raise GuidelinePolicyIdempotencyConflict(
                    "semantic_assessment_idempotency_conflict"
                )
            return await self._result_from_row(replay_row, replayed=True)

        current_subject = await self.resolve_policy_subject_snapshot(
            board_id=receipt.subject.board_id,
            entity_type=receipt.subject.entity_type,
            subject_id=receipt.subject.subject_id,
            lock=True,
        )
        if (
            current_subject is not None
            and current_subject.subject.subject_edition
            != receipt.subject.subject_edition
        ):
            raise GuidelinePolicyEditionConflict(
                "guideline_policy_edition_conflict"
            )
        if (
            current_subject is None
            or current_subject.subject != receipt.subject
            or current_subject.content_digest
            != receipt.subject_content_digest
            or current_subject.last_semantic_editor_id
            != receipt.last_semantic_editor_id
        ):
            raise GuidelinePolicySubjectConflict(
                "semantic_assessment_subject_stale"
            )
        policy_set_digest, binding_head_digest = (
            await self.semantic_current_fences(
                board_id=receipt.subject.board_id,
                entity_type=receipt.subject.entity_type,
                subject_id=receipt.subject.subject_id,
                subject_edition=receipt.subject.subject_edition,
                lock=True,
            )
        )
        if (
            policy_set_digest != receipt.policy_set_digest
            or binding_head_digest != receipt.binding_head_digest
        ):
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_policy_set_stale"
            )

        binding = (
            await self._session.execute(
                select(SemanticGuidelineBindingConfigurationRow)
                .where(
                    SemanticGuidelineBindingConfigurationRow.binding_id
                    == receipt.binding_id,
                    SemanticGuidelineBindingConfigurationRow.binding_revision
                    == receipt.binding_revision,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        legacy_binding = (
            await self._session.execute(
                select(GuidelineBoardBindingRow)
                .where(
                    GuidelineBoardBindingRow.binding_id == receipt.binding_id,
                    GuidelineBoardBindingRow.binding_revision
                    == receipt.binding_revision,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        revision = (
            await self._session.execute(
                select(SemanticGuidelineRevisionRow)
                .where(
                    SemanticGuidelineRevisionRow.guideline_id
                    == receipt.guideline_id,
                    SemanticGuidelineRevisionRow.revision_id
                    == receipt.guideline_revision_id,
                    SemanticGuidelineRevisionRow.revision_digest
                    == receipt.guideline_revision_digest,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            binding is None
            or legacy_binding is None
            or revision is None
            or revision.authority_state
            not in {"native", "legacy_context_only"}
            or legacy_binding.state != "active"
            or binding.board_id != receipt.subject.board_id
            or binding.guideline_id != receipt.guideline_id
            or binding.revision_id != receipt.guideline_revision_id
            or binding.revision_digest
            != receipt.guideline_revision_digest
            or binding.configuration_digest
            != receipt.binding_configuration_digest
            or binding.enforcement != receipt.enforcement.value
            or binding.minimum_confidence != receipt.minimum_confidence
        ):
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_authority_stale"
            )

        newest_binding_revision = (
            await self._session.execute(
                select(GuidelineBoardBindingRow.binding_revision)
                .where(
                    GuidelineBoardBindingRow.binding_id == receipt.binding_id
                )
                .order_by(GuidelineBoardBindingRow.binding_revision.desc())
                .limit(1)
            )
        ).scalar_one()
        if newest_binding_revision != receipt.binding_revision:
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_binding_stale"
            )

        # The optimistic lookup above is intentionally repeated after the
        # board, subject, binding, and revision authorities are locked. This
        # closes the concurrent same-key race before attempting the append.
        replay_row = (
            await self._session.execute(
                select(SemanticGuidelineAssessmentReceiptRow)
                .where(
                    SemanticGuidelineAssessmentReceiptRow.board_id
                    == receipt.subject.board_id,
                    SemanticGuidelineAssessmentReceiptRow.idempotency_key
                    == receipt.idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if replay_row is not None:
            if (
                replay_row.request_digest != request_digest
                or replay_row.input_digest != result.input_digest
                or replay_row.binding_id != receipt.binding_id
            ):
                raise GuidelinePolicyIdempotencyConflict(
                    "semantic_assessment_idempotency_conflict"
                )
            return await self._result_from_row(replay_row, replayed=True)

        receipt_row = SemanticGuidelineAssessmentReceiptRow(
            receipt_id=receipt.receipt_id,
            board_id=receipt.subject.board_id,
            subject_type=receipt.subject.entity_type.value,
            subject_id=receipt.subject.subject_id,
            subject_version=receipt.subject.subject_version,
            validation_edition=receipt.subject.subject_edition,
            subject_content_digest=receipt.subject_content_digest,
            last_semantic_editor_id=receipt.last_semantic_editor_id,
            guideline_id=receipt.guideline_id,
            revision_id=receipt.guideline_revision_id,
            revision_digest=receipt.guideline_revision_digest,
            binding_id=receipt.binding_id,
            binding_revision=receipt.binding_revision,
            configuration_digest=receipt.binding_configuration_digest,
            policy_set_digest=receipt.policy_set_digest,
            binding_head_digest=receipt.binding_head_digest,
            enforcement=receipt.enforcement.value,
            minimum_confidence=receipt.minimum_confidence,
            confidence=receipt.confidence,
            confidence_admissible=receipt.confidence_admissible,
            assessor_agent_id=receipt.assessor.agent_id,
            assessor_model_id=receipt.assessor.model_id,
            assessor_independent=receipt.assessor_independent,
            state=receipt.state.value,
            recorded_currentness=receipt.currentness.value,
            input_digest=receipt.input_digest,
            receipt_digest=receipt.receipt_digest,
            metric_result_count=receipt.metric_count,
            failed_metric_count=receipt.failed_metric_count,
            idempotency_key=receipt.idempotency_key,
            request_digest=request_digest,
            assessed_at=receipt.recorded_at,
            sealed=False,
        )
        self._session.add(receipt_row)
        try:
            await self._session.flush((receipt_row,))
            metric_rows = []
            for metric in receipt.metric_results:
                metric_rows.append(
                    SemanticGuidelineMetricResultRow(
                        result_id=metric.metric_result_id,
                        receipt_id=metric.receipt_id,
                        board_id=metric.subject.board_id,
                        subject_type=metric.subject.entity_type.value,
                        subject_id=metric.subject.subject_id,
                        subject_version=metric.subject.subject_version,
                        subject_content_digest=(
                            receipt.subject_content_digest
                        ),
                        receipt_digest=receipt.receipt_digest,
                        guideline_id=metric.guideline_id,
                        revision_id=metric.revision_id,
                        revision_digest=(
                            receipt.guideline_revision_digest
                        ),
                        binding_id=metric.binding_id,
                        binding_revision=receipt.binding_revision,
                        configuration_digest=(
                            receipt.binding_configuration_digest
                        ),
                        metric_id=metric.metric_id,
                        metric_code=metric.metric_code,
                        metric_definition_digest=(
                            metric.metric_definition_digest
                        ),
                        direction=metric.direction.value,
                        default_threshold=metric.default_threshold,
                        effective_threshold=metric.effective_threshold,
                        threshold_source=metric.threshold_source.value,
                        score=metric.score,
                        outcome=metric.outcome.value,
                        rationale=metric.rationale,
                        evidence_refs=[
                            _evidence_payload(item)
                            for item in metric.evidence_refs
                        ],
                        pinpoints=[
                            _pinpoint_payload(item)
                            for item in metric.pinpoints
                        ],
                        result_digest=semantic_metric_result_digest_v1(metric),
                        created_at=receipt.recorded_at,
                    )
                )
            self._session.add_all(metric_rows)
            await self._session.flush(tuple(metric_rows))
            finding_rows = []
            for finding in project_semantic_metric_findings(receipt):
                finding_rows.append(
                    SemanticGuidelineFindingRow(
                        finding_id=finding.finding_id,
                        metric_result_id=finding.metric_result_id,
                        receipt_id=finding.receipt_id,
                        board_id=finding.subject.board_id,
                        subject_type=finding.subject.entity_type.value,
                        subject_id=finding.subject.subject_id,
                        subject_version=finding.subject.subject_version,
                        subject_content_digest=finding.subject_content_digest,
                        receipt_digest=finding.receipt_digest,
                        guideline_id=finding.guideline_id,
                        revision_id=finding.guideline_revision_id,
                        revision_digest=finding.guideline_revision_digest,
                        binding_id=finding.binding_id,
                        binding_revision=finding.binding_revision,
                        configuration_digest=(
                            finding.binding_configuration_digest
                        ),
                        metric_id=finding.metric_id,
                        metric_code=finding.metric_code,
                        metric_result_digest=finding.metric_result_digest,
                        rationale=finding.rationale,
                        evidence_refs=[
                            _evidence_payload(item)
                            for item in finding.evidence_refs
                        ],
                        pinpoints=[
                            _pinpoint_payload(item)
                            for item in finding.pinpoints
                        ],
                        finding_digest=finding.finding_digest,
                        created_at=finding.created_at,
                    )
                )
            self._session.add_all(finding_rows)
            if finding_rows:
                await self._session.flush(tuple(finding_rows))
            receipt_row.sealed = True
            await self._session.flush((receipt_row,))
        except IntegrityError as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_persistence_conflict"
            ) from exc
        await stage_semantic_guideline_projection_events(
            self._session,
            board_id=receipt.subject.board_id,
            actor_id=receipt.assessor.agent_id,
            actor_type="agent",
            occurred_at=receipt.recorded_at,
            causation_id=receipt.receipt_id,
            facts=(
                SemanticGuidelineProjectionFact(
                    entity_kind="assessment_receipt",
                    entity_id=receipt.receipt_id,
                    entity_digest=receipt.receipt_digest,
                ),
                *(
                    SemanticGuidelineProjectionFact(
                        entity_kind="metric_result",
                        entity_id=metric.metric_result_id,
                        entity_digest=semantic_metric_result_digest_v1(
                            metric
                        ),
                    )
                    for metric in receipt.metric_results
                ),
            ),
        )
        return result

    async def get_semantic_assessment_receipt(
        self,
        *,
        board_id: str,
        receipt_id: str,
    ) -> SemanticGuidelineAssessmentReceipt | None:
        row = (
            await self._session.execute(
                select(SemanticGuidelineAssessmentReceiptRow).where(
                    SemanticGuidelineAssessmentReceiptRow.board_id == board_id,
                    SemanticGuidelineAssessmentReceiptRow.receipt_id
                    == receipt_id,
                    SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                )
            )
        ).scalar_one_or_none()
        return (
            None
            if row is None
            else (await self._result_from_row(row, replayed=False)).receipt
        )

    async def get_semantic_metric_result(
        self,
        *,
        board_id: str,
        metric_result_id: str,
    ) -> SemanticMetricResult | None:
        """Return one exact result only when its aggregate receipt is sealed."""

        resolved = (
            await self._session.execute(
                select(
                    SemanticGuidelineMetricResultRow,
                    SemanticGuidelineAssessmentReceiptRow.validation_edition,
                )
                .join(
                    SemanticGuidelineAssessmentReceiptRow,
                    SemanticGuidelineAssessmentReceiptRow.receipt_id
                    == SemanticGuidelineMetricResultRow.receipt_id,
                )
                .where(
                    SemanticGuidelineMetricResultRow.board_id == board_id,
                    SemanticGuidelineMetricResultRow.result_id
                    == metric_result_id,
                    SemanticGuidelineAssessmentReceiptRow.board_id
                    == board_id,
                    SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                )
            )
        ).one_or_none()
        if resolved is None:
            return None
        row, subject_edition = resolved
        return _metric_from_row(row, subject_edition=subject_edition)

    async def get_semantic_guideline_finding(
        self,
        *,
        board_id: str,
        finding_id: str,
    ) -> SemanticMetricFinding | None:
        """Return one immutable pinpoint finding by its board-scoped identity."""

        resolved = (
            await self._session.execute(
                select(
                    SemanticGuidelineFindingRow,
                    SemanticGuidelineAssessmentReceiptRow.validation_edition,
                )
                .join(
                    SemanticGuidelineAssessmentReceiptRow,
                    SemanticGuidelineAssessmentReceiptRow.receipt_id
                    == SemanticGuidelineFindingRow.receipt_id,
                )
                .where(
                    SemanticGuidelineFindingRow.board_id == board_id,
                    SemanticGuidelineFindingRow.finding_id == finding_id,
                    SemanticGuidelineAssessmentReceiptRow.board_id
                    == board_id,
                    SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                )
            )
        ).one_or_none()
        if resolved is None:
            return None
        row, subject_edition = resolved
        return _finding_from_row(row, subject_edition=subject_edition)

    async def list_semantic_assessment_receipts(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType | None = None,
        subject_id: str | None = None,
        subject_edition: int | None = None,
        guideline_id: str | None = None,
        binding_id: str | None = None,
        outcome: SemanticAssessmentState | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 50,
    ) -> tuple[
        tuple[SemanticGuidelineAssessmentReceipt, ...],
        tuple[datetime, str] | None,
    ]:
        """List sealed receipts using a stable assessed-at/id keyset."""

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 200
        ):
            raise ValueError("semantic_assessment_receipt_limit_invalid")
        statement = select(SemanticGuidelineAssessmentReceiptRow).where(
            SemanticGuidelineAssessmentReceiptRow.board_id == board_id,
            SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
        )
        if entity_type is not None:
            statement = statement.where(
                SemanticGuidelineAssessmentReceiptRow.subject_type
                == entity_type.value
            )
        if subject_id is not None:
            statement = statement.where(
                SemanticGuidelineAssessmentReceiptRow.subject_id == subject_id
            )
        if subject_edition is not None:
            statement = statement.where(
                SemanticGuidelineAssessmentReceiptRow.validation_edition
                == subject_edition
            )
        if guideline_id is not None:
            statement = statement.where(
                SemanticGuidelineAssessmentReceiptRow.guideline_id
                == guideline_id
            )
        if binding_id is not None:
            statement = statement.where(
                SemanticGuidelineAssessmentReceiptRow.binding_id == binding_id
            )
        if outcome is not None:
            statement = statement.where(
                SemanticGuidelineAssessmentReceiptRow.state == outcome.value
            )
        if after is not None:
            after_time, after_id = after
            after_time = _utc(after_time)
            statement = statement.where(
                or_(
                    SemanticGuidelineAssessmentReceiptRow.assessed_at
                    < after_time,
                    (
                        SemanticGuidelineAssessmentReceiptRow.assessed_at
                        == after_time
                    )
                    & (
                        SemanticGuidelineAssessmentReceiptRow.receipt_id
                        < after_id
                    ),
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        SemanticGuidelineAssessmentReceiptRow.assessed_at.desc(),
                        SemanticGuidelineAssessmentReceiptRow.receipt_id.desc(),
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page = rows[:limit]
        receipt_items: list[SemanticGuidelineAssessmentReceipt] = []
        for row in page:
            receipt_items.append(
                (
                    await self._result_from_row(
                        row,
                        replayed=False,
                    )
                ).receipt
            )
        receipts = tuple(receipt_items)
        next_cursor = (
            None
            if len(rows) <= limit
            else (_utc(page[-1].assessed_at), page[-1].receipt_id)
        )
        return receipts, next_cursor

    async def list_semantic_guideline_findings(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType | None = None,
        subject_id: str | None = None,
        subject_edition: int | None = None,
        receipt_id: str | None = None,
        guideline_id: str | None = None,
        binding_id: str | None = None,
        metric_id: str | None = None,
        outcome: SemanticMetricOutcome | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 50,
    ) -> tuple[
        tuple[SemanticMetricFinding, ...],
        tuple[datetime, str] | None,
    ]:
        """Read the append-only finding queue with a stable keyset cursor."""

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 200
        ):
            raise ValueError("semantic_guideline_finding_limit_invalid")
        statement = (
            select(
                SemanticGuidelineFindingRow,
                SemanticGuidelineAssessmentReceiptRow.validation_edition,
            )
            .join(
                SemanticGuidelineAssessmentReceiptRow,
                SemanticGuidelineAssessmentReceiptRow.receipt_id
                == SemanticGuidelineFindingRow.receipt_id,
            )
            .where(
                SemanticGuidelineFindingRow.board_id == board_id,
                SemanticGuidelineAssessmentReceiptRow.board_id == board_id,
                SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
            )
        )
        if entity_type is not None:
            statement = statement.where(
                SemanticGuidelineFindingRow.subject_type
                == entity_type.value
            )
        if subject_id is not None:
            statement = statement.where(
                SemanticGuidelineFindingRow.subject_id == subject_id
            )
        if subject_edition is not None:
            statement = statement.where(
                SemanticGuidelineAssessmentReceiptRow.validation_edition
                == subject_edition
            )
        if receipt_id is not None:
            statement = statement.where(
                SemanticGuidelineFindingRow.receipt_id == receipt_id
            )
        if guideline_id is not None:
            statement = statement.where(
                SemanticGuidelineFindingRow.guideline_id == guideline_id
            )
        if binding_id is not None:
            statement = statement.where(
                SemanticGuidelineFindingRow.binding_id == binding_id
            )
        if metric_id is not None:
            statement = statement.where(
                SemanticGuidelineFindingRow.metric_id == metric_id
            )
        # Findings are the immutable fail-only subset projected from metric
        # results. A caller asking for passing findings therefore receives the
        # closed empty set rather than an invented positive finding.
        if outcome is SemanticMetricOutcome.PASS:
            statement = statement.where(false())
        if after is not None:
            after_time, after_id = after
            after_time = _utc(after_time)
            statement = statement.where(
                or_(
                    SemanticGuidelineFindingRow.created_at < after_time,
                    (
                        SemanticGuidelineFindingRow.created_at == after_time
                    )
                    & (
                        SemanticGuidelineFindingRow.finding_id < after_id
                    ),
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        SemanticGuidelineFindingRow.created_at.desc(),
                        SemanticGuidelineFindingRow.finding_id.desc(),
                    ).limit(limit + 1)
                )
            ).all()
        )
        page = rows[:limit]
        next_cursor = (
            None
            if len(rows) <= limit
            else (_utc(page[-1][0].created_at), page[-1][0].finding_id)
        )
        return (
            tuple(
                _finding_from_row(row, subject_edition=subject_edition)
                for row, subject_edition in page
            ),
            next_cursor,
        )

    async def _waiver_mutation_for_rows(
        self,
        *,
        head: SemanticGuidelineWaiverRow,
        event: SemanticGuidelineWaiverEventRow,
    ) -> SemanticMetricWaiverMutation:
        revalidation_snapshot = None
        if (
            event.event_type
            != SemanticMetricWaiverEventType.REVALIDATE.value
        ):
            revalidation_snapshot = (
                await self._session.execute(
                    select(SemanticGuidelineWaiverEventRow)
                    .where(
                        SemanticGuidelineWaiverEventRow.board_id
                        == event.board_id,
                        SemanticGuidelineWaiverEventRow.waiver_id
                        == event.waiver_id,
                        SemanticGuidelineWaiverEventRow.waiver_revision
                        <= event.waiver_revision,
                        SemanticGuidelineWaiverEventRow.event_type
                        == SemanticMetricWaiverEventType.REVALIDATE.value,
                    )
                    .order_by(
                        SemanticGuidelineWaiverEventRow
                        .waiver_revision.desc()
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        return _waiver_mutation_from_rows(
            head,
            event,
            revalidation_snapshot=revalidation_snapshot,
        )

    async def get_semantic_waiver_by_idempotency(
        self,
        *,
        board_id: str,
        idempotency_key: str,
    ) -> SemanticMetricWaiverMutation | None:
        event = (
            await self._session.execute(
                select(SemanticGuidelineWaiverEventRow).where(
                    SemanticGuidelineWaiverEventRow.board_id == board_id,
                    SemanticGuidelineWaiverEventRow.idempotency_key
                    == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if event is None:
            return None
        head = await self._session.get(
            SemanticGuidelineWaiverRow,
            event.waiver_id,
        )
        if head is None or head.board_id != board_id:
            raise GuidelinePolicyDigestConflict(
                "semantic_waiver_head_missing"
            )
        return await self._waiver_mutation_for_rows(
            head=head,
            event=event,
        )

    async def get_semantic_waiver_event(
        self,
        *,
        board_id: str,
        event_id: str,
    ) -> SemanticMetricWaiverEvent | None:
        row = (
            await self._session.execute(
                select(SemanticGuidelineWaiverEventRow).where(
                    SemanticGuidelineWaiverEventRow.board_id == board_id,
                    SemanticGuidelineWaiverEventRow.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _waiver_event_from_row(row)

    async def list_semantic_waiver_events(
        self,
        *,
        board_id: str,
        waiver_id: str | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 50,
    ) -> tuple[
        tuple[SemanticMetricWaiverEvent, ...],
        tuple[datetime, str] | None,
    ]:
        """List append-only waiver events with a bounded stable keyset."""

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 200
        ):
            raise ValueError("semantic_waiver_event_limit_invalid")
        statement = select(SemanticGuidelineWaiverEventRow).where(
            SemanticGuidelineWaiverEventRow.board_id == board_id
        )
        if waiver_id is not None:
            statement = statement.where(
                SemanticGuidelineWaiverEventRow.waiver_id == waiver_id
            )
        if after is not None:
            after_time, after_id = after
            after_time = _utc(after_time)
            statement = statement.where(
                or_(
                    SemanticGuidelineWaiverEventRow.occurred_at
                    < after_time,
                    (
                        SemanticGuidelineWaiverEventRow.occurred_at
                        == after_time
                    )
                    & (
                        SemanticGuidelineWaiverEventRow.event_id
                        < after_id
                    ),
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        SemanticGuidelineWaiverEventRow.occurred_at.desc(),
                        SemanticGuidelineWaiverEventRow.event_id.desc(),
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page = rows[:limit]
        next_cursor = (
            None
            if len(rows) <= limit
            else (_utc(page[-1].occurred_at), page[-1].event_id)
        )
        return (
            tuple(_waiver_event_from_row(row) for row in page),
            next_cursor,
        )

    async def get_semantic_waiver(
        self,
        *,
        board_id: str,
        waiver_id: str,
    ) -> SemanticMetricWaiver | None:
        row = (
            await self._session.execute(
                select(SemanticGuidelineWaiverRow).where(
                    SemanticGuidelineWaiverRow.board_id == board_id,
                    SemanticGuidelineWaiverRow.waiver_id == waiver_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _waiver_from_row(row)

    async def list_board_semantic_waivers(
        self,
        *,
        board_id: str,
        evaluated_at: datetime,
        finding_id: str | None = None,
        metric_result_id: str | None = None,
        receipt_id: str | None = None,
        guideline_id: str | None = None,
        binding_id: str | None = None,
        metric_id: str | None = None,
        entity_type: PolicyEntityType | None = None,
        subject_id: str | None = None,
        subject_edition: int | None = None,
        status: SemanticMetricWaiverStatus | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 50,
    ) -> tuple[
        tuple[SemanticMetricWaiver, ...],
        tuple[datetime, str] | None,
    ]:
        """List current semantic waiver heads with a stable bounded keyset."""

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 200
        ):
            raise ValueError("semantic_waiver_limit_invalid")
        _utc(evaluated_at)
        statement = select(SemanticGuidelineWaiverRow).where(
            SemanticGuidelineWaiverRow.board_id == board_id
        )
        if finding_id is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.finding_id == finding_id
            )
        if metric_result_id is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.metric_result_id
                == metric_result_id
            )
        if receipt_id is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.receipt_id == receipt_id
            )
        if guideline_id is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.guideline_id == guideline_id
            )
        if binding_id is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.binding_id == binding_id
            )
        if metric_id is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.metric_id == metric_id
            )
        if entity_type is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.subject_type
                == entity_type.value
            )
        if subject_id is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.subject_id == subject_id
            )
        if subject_edition is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.validation_edition
                == subject_edition
            )
        if status is not None:
            statement = statement.where(
                SemanticGuidelineWaiverRow.status == status.value
            )
        if after is not None:
            after_time, after_id = after
            after_time = _utc(after_time)
            statement = statement.where(
                or_(
                    SemanticGuidelineWaiverRow.requested_at < after_time,
                    (
                        SemanticGuidelineWaiverRow.requested_at == after_time
                    )
                    & (SemanticGuidelineWaiverRow.waiver_id < after_id),
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        SemanticGuidelineWaiverRow.requested_at.desc(),
                        SemanticGuidelineWaiverRow.waiver_id.desc(),
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page = rows[:limit]
        next_cursor = (
            None
            if len(rows) <= limit
            else (_utc(page[-1].requested_at), page[-1].waiver_id)
        )
        return tuple(_waiver_from_row(row) for row in page), next_cursor

    async def save_semantic_metric_waiver_mutation(
        self,
        *,
        mutation: SemanticMetricWaiverMutation,
    ) -> SemanticMetricWaiverMutation:
        """Persist one Core-built waiver request/transition with CAS replay."""

        if not isinstance(mutation, SemanticMetricWaiverMutation):
            raise GuidelinePolicyDigestConflict(
                "semantic_waiver_mutation_invalid"
            )
        waiver = mutation.waiver
        event = mutation.event
        board_id = waiver.anchor.subject.board_id
        await lock_policy_board(self._session, board_id=board_id)

        replay_event = (
            await self._session.execute(
                select(SemanticGuidelineWaiverEventRow)
                .where(
                    SemanticGuidelineWaiverEventRow.board_id == board_id,
                    SemanticGuidelineWaiverEventRow.idempotency_key
                    == event.idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if replay_event is not None:
            if replay_event.request_digest != event.request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "semantic_waiver_idempotency_conflict"
                )
            replay_head = (
                await self._session.execute(
                    select(SemanticGuidelineWaiverRow)
                    .where(
                        SemanticGuidelineWaiverRow.board_id == board_id,
                        SemanticGuidelineWaiverRow.waiver_id
                        == replay_event.waiver_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if replay_head is None:
                raise GuidelinePolicyDigestConflict(
                    "semantic_waiver_head_missing"
                )
            return await self._waiver_mutation_for_rows(
                head=replay_head,
                event=replay_event,
            )

        anchor = waiver.anchor
        if event.event_type is SemanticMetricWaiverEventType.REQUEST:
            finding_row = (
                await self._session.execute(
                    select(SemanticGuidelineFindingRow)
                    .where(
                        SemanticGuidelineFindingRow.finding_id
                        == anchor.finding_id,
                        SemanticGuidelineFindingRow.metric_result_id
                        == anchor.metric_result_id,
                        SemanticGuidelineFindingRow.receipt_id
                        == anchor.receipt_id,
                        SemanticGuidelineFindingRow.receipt_digest
                        == anchor.receipt_digest,
                        SemanticGuidelineFindingRow.board_id == board_id,
                        SemanticGuidelineFindingRow.subject_type
                        == anchor.subject.entity_type.value,
                        SemanticGuidelineFindingRow.subject_id
                        == anchor.subject.subject_id,
                        SemanticGuidelineFindingRow.subject_version
                        == anchor.subject.subject_version,
                        SemanticGuidelineFindingRow.subject_content_digest
                        == anchor.subject_content_digest,
                        SemanticGuidelineFindingRow.guideline_id
                        == anchor.guideline_id,
                        SemanticGuidelineFindingRow.revision_id
                        == anchor.guideline_revision_id,
                        SemanticGuidelineFindingRow.revision_digest
                        == anchor.guideline_revision_digest,
                        SemanticGuidelineFindingRow.binding_id
                        == anchor.binding_id,
                        SemanticGuidelineFindingRow.binding_revision
                        == anchor.binding_revision,
                        SemanticGuidelineFindingRow.configuration_digest
                        == anchor.binding_configuration_digest,
                        SemanticGuidelineFindingRow.metric_id
                        == anchor.metric_id,
                        SemanticGuidelineFindingRow.metric_result_digest
                        == anchor.metric_result_digest,
                        SemanticGuidelineFindingRow.finding_digest
                        == anchor.finding_digest,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            assessor_identity = (
                await self._session.execute(
                    select(
                        SemanticGuidelineAssessmentReceiptRow.assessor_agent_id,
                        SemanticGuidelineAssessmentReceiptRow.validation_edition,
                    )
                    .where(
                        SemanticGuidelineAssessmentReceiptRow.receipt_id
                        == anchor.receipt_id,
                        SemanticGuidelineAssessmentReceiptRow.board_id
                        == board_id,
                        SemanticGuidelineAssessmentReceiptRow.receipt_digest
                        == anchor.receipt_digest,
                        SemanticGuidelineAssessmentReceiptRow.sealed.is_(
                            True
                        ),
                    )
                    .with_for_update()
                )
            ).one_or_none()
            current_subject = await self.resolve_policy_subject_snapshot(
                board_id=board_id,
                entity_type=anchor.subject.entity_type,
                subject_id=anchor.subject.subject_id,
                lock=True,
            )
            if (
                current_subject is not None
                and current_subject.subject.subject_edition
                != anchor.subject.subject_edition
            ) or (
                assessor_identity is not None
                and assessor_identity[1] != anchor.subject.subject_edition
            ):
                raise GuidelinePolicyEditionConflict(
                    "guideline_policy_edition_conflict"
                )
            assessor_id = (
                None if assessor_identity is None else assessor_identity[0]
            )
            if (
                finding_row is None
                or assessor_id is None
                or SemanticMetricWaiverAnchor.from_finding(
                    _finding_from_row(
                        finding_row,
                        subject_edition=anchor.subject.subject_edition,
                    ),
                    assessment_assessor_id=assessor_id,
                )
                != anchor
            ):
                raise GuidelinePolicyDigestConflict(
                    "semantic_waiver_anchor_stale"
                )
            if waiver.waiver_revision != 1:
                raise GuidelinePolicyDigestConflict(
                    "semantic_waiver_initial_revision_invalid"
                )
            duplicate = (
                await self._session.execute(
                    select(SemanticGuidelineWaiverRow.waiver_id)
                    .where(
                        SemanticGuidelineWaiverRow.scope_digest
                        == waiver.scope_digest,
                        SemanticGuidelineWaiverRow.metric_result_id
                        == anchor.metric_result_id,
                        SemanticGuidelineWaiverRow.metric_result_digest
                        == anchor.metric_result_digest,
                        SemanticGuidelineWaiverRow.finding_id
                        == anchor.finding_id,
                        SemanticGuidelineWaiverRow.finding_digest
                        == anchor.finding_digest,
                        SemanticGuidelineWaiverRow.receipt_id
                        == anchor.receipt_id,
                        SemanticGuidelineWaiverRow.receipt_digest
                        == anchor.receipt_digest,
                        SemanticGuidelineWaiverRow.status.in_(
                            ("requested", "approved")
                        ),
                        or_(
                            SemanticGuidelineWaiverRow.expires_at.is_(None),
                            SemanticGuidelineWaiverRow.expires_at
                            > waiver.requested_at,
                        ),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                raise GuidelinePolicyDigestConflict(
                    "semantic_waiver_scope_conflict"
                )
            head_row = _new_waiver_row(mutation)
            event_row = _new_waiver_event_row(
                mutation,
                board_id=board_id,
            )
            self._session.add(head_row)
            try:
                await self._session.flush((head_row,))
                self._session.add(event_row)
                await self._session.flush((event_row,))
            except IntegrityError as exc:
                raise GuidelinePolicyDigestConflict(
                    "semantic_waiver_persistence_conflict"
                ) from exc
            await stage_semantic_guideline_projection_events(
                self._session,
                board_id=board_id,
                actor_id=event.actor_id,
                # The immutable waiver event contract intentionally records
                # the actor identity but not an asserted actor kind.  Do not
                # invent user/agent provenance in the projection envelope.
                actor_type="system",
                occurred_at=event.occurred_at,
                causation_id=event.event_id,
                facts=(
                    SemanticGuidelineProjectionFact(
                        entity_kind="waiver",
                        entity_id=waiver.waiver_id,
                        entity_digest=waiver.head_digest,
                    ),
                ),
            )
            return await self._waiver_mutation_for_rows(
                head=head_row,
                event=event_row,
            )

        head_row = (
            await self._session.execute(
                select(SemanticGuidelineWaiverRow)
                .where(
                    SemanticGuidelineWaiverRow.board_id == board_id,
                    SemanticGuidelineWaiverRow.waiver_id == waiver.waiver_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if head_row is None:
            raise GuidelinePolicyDigestConflict(
                "semantic_waiver_head_missing"
            )
        current = _waiver_from_row(head_row)
        if (
            current.waiver_revision + 1 != waiver.waiver_revision
            or current.last_event_id != event.predecessor_event_id
            or current.anchor != waiver.anchor
            or current.scope_digest != waiver.scope_digest
        ):
            raise GuidelinePolicyDigestConflict(
                "semantic_waiver_revision_conflict"
            )
        head_row.status = waiver.status.value
        head_row.waiver_revision = waiver.waiver_revision
        head_row.expires_at = waiver.expires_at
        head_row.last_event_id = waiver.last_event_id
        head_row.last_event_type = waiver.last_event_type.value
        head_row.last_event_at = waiver.last_event_at
        head_row.last_event_idempotency_key = (
            waiver.last_event_idempotency_key
        )
        head_row.reviewed_by = waiver.reviewed_by
        head_row.reviewed_at = waiver.reviewed_at
        head_row.review_reason = waiver.review_reason
        head_row.revoked_by = waiver.revoked_by
        head_row.revoked_at = waiver.revoked_at
        head_row.expire_reason_code = (
            waiver.expire_reason.value
            if waiver.expire_reason is not None
            else None
        )
        head_row.last_revalidation_status = (
            waiver.last_revalidation_status.value
            if waiver.last_revalidation_status is not None
            else None
        )
        head_row.last_revalidation_current = (
            waiver.last_revalidation_current
        )
        head_row.last_revalidation_reason_code = (
            waiver.last_revalidation_reason_code.value
            if waiver.last_revalidation_reason_code is not None
            else None
        )
        head_row.last_revalidation_evaluated_at = (
            waiver.last_revalidation_evaluated_at
        )
        head_row.last_revalidation_currentness_reasons = [
            item.value
            for item in waiver.last_revalidation_currentness_reasons
        ]
        head_row.last_revalidation_scheduled_expiry_observed = (
            waiver.last_revalidation_scheduled_expiry_observed
        )
        head_row.head_digest = waiver.head_digest
        event_row = _new_waiver_event_row(mutation, board_id=board_id)
        try:
            await self._session.flush((head_row,))
            self._session.add(event_row)
            await self._session.flush((event_row,))
        except IntegrityError as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_waiver_persistence_conflict"
            ) from exc
        await stage_semantic_guideline_projection_events(
            self._session,
            board_id=board_id,
            actor_id=event.actor_id,
            # See the request branch above: actor kind is absent from this
            # closed event contract, therefore the envelope remains neutral.
            actor_type="system",
            occurred_at=event.occurred_at,
            causation_id=event.event_id,
            facts=(
                SemanticGuidelineProjectionFact(
                    entity_kind="waiver",
                    entity_id=waiver.waiver_id,
                    entity_digest=waiver.head_digest,
                    operation=(
                        "terminate"
                        if waiver.status
                        in {
                            SemanticMetricWaiverStatus.REJECTED,
                            SemanticMetricWaiverStatus.REVOKED,
                            SemanticMetricWaiverStatus.EXPIRED,
                        }
                        else "upsert"
                    ),
                ),
            ),
        )
        return await self._waiver_mutation_for_rows(
            head=head_row,
            event=event_row,
        )

    async def save_semantic_policy_skip_mutation(
        self,
        *,
        mutation: SemanticPolicySkipMutation,
    ) -> SemanticPolicySkipMutation:
        """Persist a complete Core skip head/event snapshot append-only."""

        if not isinstance(mutation, SemanticPolicySkipMutation):
            raise GuidelinePolicyDigestConflict(
                "semantic_skip_mutation_invalid"
            )
        skip = mutation.skip
        event = mutation.event
        scope = skip.scope
        board_id = scope.subject.board_id
        await lock_policy_board(self._session, board_id=board_id)

        replay = (
            await self._session.execute(
                select(SemanticGuidelineSkipRow)
                .where(
                    SemanticGuidelineSkipRow.board_id == board_id,
                    SemanticGuidelineSkipRow.idempotency_key
                    == event.idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if replay is not None:
            if replay.request_digest != event.request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "semantic_skip_idempotency_conflict"
                )
            return _skip_mutation_from_row(replay)

        if event.event_type is SemanticPolicySkipEventType.CREATE:
            subject = await self.resolve_policy_subject_snapshot(
                board_id=board_id,
                entity_type=scope.subject.entity_type,
                subject_id=scope.subject.subject_id,
                lock=True,
            )
            binding = (
                await self._session.execute(
                    select(SemanticGuidelineBindingConfigurationRow)
                    .where(
                        SemanticGuidelineBindingConfigurationRow.binding_id
                        == scope.binding_id,
                        SemanticGuidelineBindingConfigurationRow.binding_revision
                        == scope.binding_revision,
                        SemanticGuidelineBindingConfigurationRow.board_id
                        == board_id,
                        SemanticGuidelineBindingConfigurationRow.guideline_id
                        == scope.guideline_id,
                        SemanticGuidelineBindingConfigurationRow.revision_id
                        == scope.guideline_revision_id,
                        SemanticGuidelineBindingConfigurationRow.revision_digest
                        == scope.guideline_revision_digest,
                        SemanticGuidelineBindingConfigurationRow.configuration_digest
                        == scope.binding_configuration_digest,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            legacy = (
                await self._session.execute(
                    select(GuidelineBoardBindingRow)
                    .where(
                        GuidelineBoardBindingRow.binding_id
                        == scope.binding_id,
                        GuidelineBoardBindingRow.binding_revision
                        == scope.binding_revision,
                        GuidelineBoardBindingRow.board_id == board_id,
                        GuidelineBoardBindingRow.guideline_id
                        == scope.guideline_id,
                        GuidelineBoardBindingRow.revision_id
                        == scope.guideline_revision_id,
                        GuidelineBoardBindingRow.state == "active",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            newest_legacy = (
                await self._session.execute(
                    select(GuidelineBoardBindingRow)
                    .where(
                        GuidelineBoardBindingRow.binding_id
                        == scope.binding_id,
                        GuidelineBoardBindingRow.board_id == board_id,
                    )
                    .order_by(
                        GuidelineBoardBindingRow.binding_revision.desc()
                    )
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            revision = (
                await self._session.execute(
                    select(SemanticGuidelineRevisionRow)
                    .where(
                        SemanticGuidelineRevisionRow.guideline_id
                        == scope.guideline_id,
                        SemanticGuidelineRevisionRow.revision_id
                        == scope.guideline_revision_id,
                        SemanticGuidelineRevisionRow.revision_digest
                        == scope.guideline_revision_digest,
                        SemanticGuidelineRevisionRow.authority_state
                        != "legacy_incompatible",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                subject is not None
                and subject.subject.subject_edition
                != scope.subject.subject_edition
            ):
                raise GuidelinePolicyEditionConflict(
                    "guideline_policy_edition_conflict"
                )
            if (
                subject is None
                or subject.subject != scope.subject
                or subject.content_digest != scope.subject_content_digest
                or binding is None
                or legacy is None
                or newest_legacy is None
                or newest_legacy.binding_revision
                != scope.binding_revision
                or newest_legacy.state != "active"
                or revision is None
            ):
                raise GuidelinePolicyDigestConflict(
                    "semantic_skip_scope_stale"
                )
            rows = tuple(
                (
                    await self._session.execute(
                        select(SemanticGuidelineSkipRow)
                        .where(
                            SemanticGuidelineSkipRow.scope_digest
                            == skip.scope_digest
                        )
                        .order_by(
                            SemanticGuidelineSkipRow.skip_id.asc(),
                            SemanticGuidelineSkipRow.skip_revision.desc(),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            heads: dict[str, SemanticGuidelineSkipRow] = {}
            for row in rows:
                heads.setdefault(row.skip_id, row)
            if any(row.status == "active" for row in heads.values()):
                raise GuidelinePolicyDigestConflict(
                    "semantic_skip_scope_conflict"
                )
        else:
            predecessor = (
                await self._session.execute(
                    select(SemanticGuidelineSkipRow)
                    .where(
                        SemanticGuidelineSkipRow.board_id == board_id,
                        SemanticGuidelineSkipRow.event_id
                        == event.predecessor_event_id,
                        SemanticGuidelineSkipRow.skip_id == skip.skip_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            successor = (
                await self._session.execute(
                    select(SemanticGuidelineSkipRow.event_id)
                    .where(
                        SemanticGuidelineSkipRow.predecessor_event_id
                        == event.predecessor_event_id
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                predecessor is None
                or successor is not None
                or predecessor.status != "active"
                or predecessor.skip_revision + 1 != skip.skip_revision
                or _skip_mutation_from_row(predecessor).skip.scope != scope
            ):
                raise GuidelinePolicyDigestConflict(
                    "semantic_skip_revision_conflict"
                )

        row = _new_skip_row(mutation)
        self._session.add(row)
        try:
            await self._session.flush((row,))
        except IntegrityError as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_skip_persistence_conflict"
            ) from exc
        await stage_semantic_guideline_projection_events(
            self._session,
            board_id=board_id,
            actor_id=event.actor_id,
            actor_type="user",
            occurred_at=event.occurred_at,
            causation_id=event.event_id,
            facts=(
                SemanticGuidelineProjectionFact(
                    entity_kind="skip",
                    entity_id=skip.skip_id,
                    entity_digest=skip.skip_digest,
                    operation=(
                        "terminate"
                        if skip.status is SemanticPolicySkipStatus.REVOKED
                        else "upsert"
                    ),
                ),
            ),
        )
        return _skip_mutation_from_row(row)

    async def get_active_semantic_skip(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        subject_version: int,
        subject_content_digest: str,
        binding_id: str,
        binding_revision: int,
        configuration_digest: str,
        guideline_id: str,
        revision_id: str,
        revision_digest: str,
        subject_edition: int | None = None,
    ) -> SemanticPolicySkip | None:
        """Resolve only an exact active append-only skip lifecycle head."""

        scope_filters: tuple[object, ...] = (
            SemanticGuidelineSkipRow.board_id == board_id,
            SemanticGuidelineSkipRow.subject_type == entity_type.value,
            SemanticGuidelineSkipRow.subject_id == subject_id,
            SemanticGuidelineSkipRow.binding_id == binding_id,
        )
        if subject_edition is not None:
            # Human skips remain effective for their exact lifecycle edition.
            # Technical subject/config/revision drift stays in audit metadata
            # and cannot revoke or create a human decision.
            scope_filters += (
                SemanticGuidelineSkipRow.validation_edition
                == subject_edition,
            )
        else:
            # Legacy/non-lifecycle subjects retain the exact technical-fence
            # contract.
            scope_filters += (
                SemanticGuidelineSkipRow.subject_version == subject_version,
                SemanticGuidelineSkipRow.validation_edition.is_(None),
                SemanticGuidelineSkipRow.subject_content_digest
                == subject_content_digest,
                SemanticGuidelineSkipRow.binding_revision
                == binding_revision,
                SemanticGuidelineSkipRow.configuration_digest
                == configuration_digest,
                SemanticGuidelineSkipRow.guideline_id == guideline_id,
                SemanticGuidelineSkipRow.revision_id == revision_id,
                SemanticGuidelineSkipRow.revision_digest == revision_digest,
            )
        rows = tuple(
            (
                await self._session.execute(
                    select(SemanticGuidelineSkipRow)
                    .where(*scope_filters)
                    .order_by(
                        SemanticGuidelineSkipRow.skip_id.asc(),
                        SemanticGuidelineSkipRow.skip_revision.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        heads: dict[str, SemanticGuidelineSkipRow] = {}
        for row in rows:
            heads.setdefault(row.skip_id, row)
        active = tuple(
            row for row in heads.values() if row.status == "active"
        )
        if len(active) > 1:
            raise GuidelinePolicyDigestConflict(
                "semantic_guideline_skip_active_head_conflict"
            )
        return (
            None
            if not active
            else _skip_mutation_from_row(active[0]).skip
        )

    async def get_semantic_skip(
        self,
        *,
        board_id: str,
        skip_id: str,
    ) -> SemanticPolicySkip | None:
        """Return the latest append-only lifecycle head for one skip."""

        row = (
            await self._session.execute(
                select(SemanticGuidelineSkipRow)
                .where(
                    SemanticGuidelineSkipRow.board_id == board_id,
                    SemanticGuidelineSkipRow.skip_id == skip_id,
                )
                .order_by(
                    SemanticGuidelineSkipRow.skip_revision.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return (
            None
            if row is None
            else _skip_mutation_from_row(row).skip
        )

    async def list_semantic_policy_skips(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType | None = None,
        subject_id: str | None = None,
        subject_edition: int | None = None,
        binding_id: str | None = None,
        status: SemanticPolicySkipStatus | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 50,
    ) -> tuple[
        tuple[SemanticPolicySkip, ...],
        tuple[datetime, str] | None,
    ]:
        """List current skip heads without loading complete lifecycles."""

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 200
        ):
            raise ValueError("semantic_skip_limit_invalid")
        candidate = aliased(SemanticGuidelineSkipRow)
        latest_revision = (
            select(func.max(candidate.skip_revision))
            .where(
                candidate.board_id == SemanticGuidelineSkipRow.board_id,
                candidate.skip_id == SemanticGuidelineSkipRow.skip_id,
            )
            .correlate(SemanticGuidelineSkipRow)
            .scalar_subquery()
        )
        statement = select(SemanticGuidelineSkipRow).where(
            SemanticGuidelineSkipRow.board_id == board_id,
            SemanticGuidelineSkipRow.skip_revision == latest_revision,
        )
        if entity_type is not None:
            statement = statement.where(
                SemanticGuidelineSkipRow.subject_type == entity_type.value
            )
        if subject_id is not None:
            statement = statement.where(
                SemanticGuidelineSkipRow.subject_id == subject_id
            )
        if subject_edition is not None:
            statement = statement.where(
                SemanticGuidelineSkipRow.validation_edition
                == subject_edition
            )
        if binding_id is not None:
            statement = statement.where(
                SemanticGuidelineSkipRow.binding_id == binding_id
            )
        if status is not None:
            statement = statement.where(
                SemanticGuidelineSkipRow.status == status.value
            )
        if after is not None:
            after_time, after_id = after
            after_time = _utc(after_time)
            statement = statement.where(
                or_(
                    SemanticGuidelineSkipRow.created_at < after_time,
                    (
                        SemanticGuidelineSkipRow.created_at == after_time
                    )
                    & (SemanticGuidelineSkipRow.skip_id < after_id),
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        SemanticGuidelineSkipRow.created_at.desc(),
                        SemanticGuidelineSkipRow.skip_id.desc(),
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page = rows[:limit]
        next_cursor = (
            None
            if len(rows) <= limit
            else (_utc(page[-1].created_at), page[-1].skip_id)
        )
        return (
            tuple(_skip_mutation_from_row(row).skip for row in page),
            next_cursor,
        )

    async def list_semantic_skip_events(
        self,
        *,
        board_id: str,
        skip_id: str | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 50,
    ) -> tuple[
        tuple[SemanticPolicySkipEvent, ...],
        tuple[datetime, str] | None,
    ]:
        """List complete skip lifecycle events with a bounded keyset."""

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 200
        ):
            raise ValueError("semantic_skip_event_limit_invalid")
        statement = select(SemanticGuidelineSkipRow).where(
            SemanticGuidelineSkipRow.board_id == board_id
        )
        if skip_id is not None:
            statement = statement.where(
                SemanticGuidelineSkipRow.skip_id == skip_id
            )
        if after is not None:
            after_time, after_id = after
            after_time = _utc(after_time)
            statement = statement.where(
                or_(
                    SemanticGuidelineSkipRow.occurred_at < after_time,
                    (
                        SemanticGuidelineSkipRow.occurred_at == after_time
                    )
                    & (SemanticGuidelineSkipRow.event_id < after_id),
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        SemanticGuidelineSkipRow.occurred_at.desc(),
                        SemanticGuidelineSkipRow.event_id.desc(),
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page = rows[:limit]
        next_cursor = (
            None
            if len(rows) <= limit
            else (_utc(page[-1].occurred_at), page[-1].event_id)
        )
        return (
            tuple(_skip_mutation_from_row(row).event for row in page),
            next_cursor,
        )

    async def get_semantic_skip_event_by_idempotency(
        self,
        *,
        board_id: str,
        idempotency_key: str,
    ) -> SemanticPolicySkipMutation | None:
        """Return the exact create/revoke event used for safe replay."""

        row = (
            await self._session.execute(
                select(SemanticGuidelineSkipRow).where(
                    SemanticGuidelineSkipRow.board_id == board_id,
                    SemanticGuidelineSkipRow.idempotency_key
                    == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _skip_mutation_from_row(row)

    async def get_current_semantic_assessment_receipt(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        binding_id: str,
        subject_edition: int | None = None,
    ) -> SemanticGuidelineAssessmentReceipt | None:
        current = await self.resolve_semantic_assessment_current_snapshot(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            binding_id=binding_id,
        )
        if current is None:
            return None
        live_edition = current.subject.subject_edition
        if subject_edition is not None and subject_edition != live_edition:
            return None

        # Human validation evidence is current for the exact lifecycle edition.
        # Content/configuration digests remain immutable audit facts, but a
        # technical drift inside the same edition does not invalidate the
        # human result.  Legacy subjects without editions retain the old exact
        # technical-fence behavior below.
        if live_edition is not None:
            row = (
                await self._session.execute(
                    select(SemanticGuidelineAssessmentReceiptRow)
                    .where(
                        SemanticGuidelineAssessmentReceiptRow.board_id
                        == board_id,
                        SemanticGuidelineAssessmentReceiptRow.subject_type
                        == entity_type.value,
                        SemanticGuidelineAssessmentReceiptRow.subject_id
                        == subject_id,
                        SemanticGuidelineAssessmentReceiptRow.binding_id
                        == binding_id,
                        SemanticGuidelineAssessmentReceiptRow.validation_edition
                        == live_edition,
                        SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                    )
                    .order_by(
                        SemanticGuidelineAssessmentReceiptRow.assessed_at.desc(),
                        SemanticGuidelineAssessmentReceiptRow.receipt_id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return (
                None
                if row is None
                else (await self._result_from_row(row, replayed=False)).receipt
            )
        row = (
            await self._session.execute(
                select(SemanticGuidelineAssessmentReceiptRow)
                .where(
                    SemanticGuidelineAssessmentReceiptRow.board_id == board_id,
                    SemanticGuidelineAssessmentReceiptRow.subject_type
                    == entity_type.value,
                    SemanticGuidelineAssessmentReceiptRow.subject_id
                    == subject_id,
                    SemanticGuidelineAssessmentReceiptRow.subject_version
                    == current.subject.subject_version,
                    SemanticGuidelineAssessmentReceiptRow.subject_content_digest
                    == current.subject_content_digest,
                    SemanticGuidelineAssessmentReceiptRow.guideline_id
                    == current.guideline_id,
                    SemanticGuidelineAssessmentReceiptRow.revision_id
                    == current.guideline_revision_id,
                    SemanticGuidelineAssessmentReceiptRow.revision_digest
                    == current.guideline_revision_digest,
                    SemanticGuidelineAssessmentReceiptRow.binding_id
                    == current.binding_id,
                    SemanticGuidelineAssessmentReceiptRow.binding_revision
                    == current.binding_revision,
                    SemanticGuidelineAssessmentReceiptRow.configuration_digest
                    == current.binding_configuration_digest,
                    SemanticGuidelineAssessmentReceiptRow.policy_set_digest
                    == current.policy_set_digest,
                    SemanticGuidelineAssessmentReceiptRow.binding_head_digest
                    == current.binding_head_digest,
                    SemanticGuidelineAssessmentReceiptRow.input_digest
                    == current.input_digest,
                    SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                )
                .order_by(
                    SemanticGuidelineAssessmentReceiptRow.assessed_at.desc(),
                    SemanticGuidelineAssessmentReceiptRow.receipt_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return (
            None
            if row is None
            else (await self._result_from_row(row, replayed=False)).receipt
        )

    async def resolve_semantic_assessment_current_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        binding_id: str,
        lock: bool = False,
    ) -> SemanticAssessmentCurrentSnapshot | None:
        """Resolve the live fence for exactly one subject×binding pair."""

        if lock:
            await lock_policy_board(self._session, board_id=board_id)
        subject = await self.resolve_policy_subject_snapshot(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=lock,
        )
        if subject is None:
            return None
        bindings, revisions = await self._authority_bundle_for_subject(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            subject_edition=subject.subject.subject_edition,
            lock=lock,
        )
        selected = tuple(
            binding
            for binding in bindings
            if binding.binding_id == binding_id
            and binding.state is GuidelineBindingState.ACTIVE
        )
        if not selected:
            return None
        if len(selected) != 1:
            raise GuidelinePolicyDigestConflict(
                "semantic_guideline_binding_head_conflict"
            )
        binding = selected[0]
        revision_by_identity = {
            (revision.guideline_id, revision.revision_id): revision
            for revision in revisions
        }
        revision = revision_by_identity.get(
            (binding.guideline_id, binding.revision_id)
        )
        if revision is None:
            raise GuidelinePolicyDigestConflict(
                "semantic_guideline_bound_revision_missing"
            )
        context = SemanticGuidelineAssessmentContext(
            subject_snapshot=subject,
            binding=binding,
            revision=revision,
            policy_set_digest=semantic_policy_set_digest_v1(
                bindings,
                revisions,
            ),
            binding_head_digest=semantic_binding_head_digest_v1(bindings),
        )
        return semantic_assessment_current_snapshot_from_context(context)

    async def resolve_transition_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        expected_from_status: str,
    ) -> PolicyTransitionSnapshot:
        """Resolve the authoritative semantic-v2 gate fence in one UoW.

        This path intentionally never consults the retired predicate/rule
        evaluator.  The board is serialized before subject, binding, receipt,
        waiver, and skip authority is read so the resulting decision cannot
        mix evidence from different transaction fences.
        """

        await lock_policy_board(self._session, board_id=board_id)
        expected_status = str(expected_from_status).strip().lower()
        subject = await self.resolve_policy_subject_snapshot(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=True,
        )
        if subject is None:
            return PolicyTransitionSnapshot(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                expected_from_status=expected_status,
                bindings=(),
                evaluated_at=datetime.now(timezone.utc),
                subject_available=False,
            )
        actual_status = await self._resolve_policy_subject_status(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
        )
        if actual_status != expected_status:
            raise GuidelinePolicySubjectConflict(
                "policy_transition_subject_status_conflict"
            )

        bindings, revisions = await self._authority_bundle_for_subject(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            subject_edition=subject.subject.subject_edition,
            lock=True,
        )
        policy_set_digest = semantic_policy_set_digest_v1(
            bindings,
            revisions,
        )
        binding_head_digest = semantic_binding_head_digest_v1(bindings)
        revision_by_identity = {
            (revision.guideline_id, revision.revision_id): revision
            for revision in revisions
        }
        snapshots: list[SemanticBindingComplianceSnapshot] = []
        for binding in bindings:
            if binding.state is not GuidelineBindingState.ACTIVE:
                continue
            revision = revision_by_identity.get(
                (binding.guideline_id, binding.revision_id)
            )
            if revision is None:
                raise GuidelinePolicyDigestConflict(
                    "semantic_guideline_bound_revision_missing"
                )
            context = SemanticGuidelineAssessmentContext(
                subject_snapshot=subject,
                binding=binding,
                revision=revision,
                policy_set_digest=policy_set_digest,
                binding_head_digest=binding_head_digest,
            )
            current = semantic_assessment_current_snapshot_from_context(
                context
            )
            receipt_row = (
                await self._session.execute(
                    select(SemanticGuidelineAssessmentReceiptRow)
                    .where(
                        SemanticGuidelineAssessmentReceiptRow.board_id
                        == board_id,
                        SemanticGuidelineAssessmentReceiptRow.subject_type
                        == entity_type.value,
                        SemanticGuidelineAssessmentReceiptRow.subject_id
                        == subject_id,
                        SemanticGuidelineAssessmentReceiptRow.binding_id
                        == binding.binding_id,
                        SemanticGuidelineAssessmentReceiptRow.validation_edition
                        == subject.subject.subject_edition,
                        SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                    )
                    .order_by(
                        SemanticGuidelineAssessmentReceiptRow.assessed_at.desc(),
                        SemanticGuidelineAssessmentReceiptRow.receipt_id.desc(),
                    )
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            receipt = (
                None
                if receipt_row is None
                else (
                    await self._result_from_row(
                        receipt_row,
                        replayed=False,
                    )
                ).receipt
            )
            findings = (
                ()
                if receipt is None
                else project_semantic_metric_findings(receipt)
            )
            waiver_rows = ()
            if receipt is not None:
                waiver_rows = tuple(
                    (
                        await self._session.execute(
                            select(SemanticGuidelineWaiverRow)
                            .where(
                                SemanticGuidelineWaiverRow.board_id
                                == board_id,
                                SemanticGuidelineWaiverRow.receipt_id
                                == receipt.receipt_id,
                                SemanticGuidelineWaiverRow.binding_id
                                == binding.binding_id,
                            )
                            .order_by(
                                SemanticGuidelineWaiverRow.waiver_id.asc()
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
            waivers = tuple(_waiver_from_row(row) for row in waiver_rows)
            skip = await self.get_active_semantic_skip(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                subject_version=subject.subject.subject_version,
                subject_edition=subject.subject.subject_edition,
                subject_content_digest=subject.content_digest,
                binding_id=binding.binding_id,
                binding_revision=binding.binding_revision,
                configuration_digest=binding.configuration_digest,
                guideline_id=binding.guideline_id,
                revision_id=binding.revision_id,
                revision_digest=binding.revision_digest,
            )
            snapshots.append(
                SemanticBindingComplianceSnapshot(
                    binding_id=binding.binding_id,
                    guideline_id=binding.guideline_id,
                    enforcement=binding.enforcement,
                    applicable_metric_count=len(context.applicable_metrics),
                    current_snapshot=current,
                    receipt=receipt,
                    findings=findings,
                    waivers=waivers,
                    skip=skip,
                )
            )
        return PolicyTransitionSnapshot(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            expected_from_status=expected_status,
            bindings=tuple(snapshots),
            evaluated_at=datetime.now(timezone.utc),
        )


__all__ = ["CommunitySqlAlchemySemanticGuidelineAssessment"]
