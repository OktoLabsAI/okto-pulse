"""Community SQLAlchemy persistence adapter for consolidation processing."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, selectinload

from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision,
    ArtifactDeletionTombstone,
    Board,
    CanonicalDebt,
    Card,
    CodeEvidenceRow,
    CodeEvidenceSpecLinkRow,
    CodeInvestigationHeadRow,
    CodeInvestigationReceiptRevocationRow,
    CodeInvestigationReceiptRow,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    Ideation,
    IdeationQAItem,
    KGTakedownStateEvent,
    ImplementationTargetEvidenceLinkRow,
    ImplementationTargetResolutionRow,
    ImplementationTargetRow,
    QualityAssessmentHeadRow,
    QualityAssessmentReceiptRow,
    Refinement,
    RefinementQAItem,
    ResearchDecisionEntryRow,
    ResearchDecisionHeadRow,
    Spec,
    SpecDependency,
    SpecQAItem,
    Sprint,
    Story,
)
from okto_pulse.core.ports.consolidation import (
    ConsolidationPoisonRow,
    ConsolidationProjectionInputs,
    ConsolidationQueueRecord,
    CurrentQualityAssessmentSummary,
    CurrentResearchDecisionSummary,
    CurrentSpecDependencyProjection,
)
from okto_pulse.core.kg.board_source_store import (
    quality_current_head_fingerprint,
    research_decision_current_head_fingerprint,
)
from okto_pulse.core.domain.quality_assessment import AssessmentDigestSet
from okto_pulse.core.services.quality_projection_currentness import (
    QualityProjectionCurrentnessError,
    evaluate_quality_projection_currentness,
)
from okto_pulse.core.ports.reconcile_intent import (
    ReconcileIntentCreate,
    ReconcileIntentReceipt,
)
from okto_pulse.core.ports.tombstone import (
    DeletionTombstoneAdvance,
    DeletionTombstoneReceipt,
)
from okto_pulse.core.ports.takedown_telemetry import (
    TakedownState,
    TakedownTransition,
)
from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
    stage_takedown_transition,
)
from okto_pulse.core.ports.stale_sweep import (
    STALE_SWEEP_ARTIFACT_TYPE,
    STALE_SWEEP_CATCHUP_EPOCH,
    STALE_SWEEP_WORK_KIND,
    StaleSweepBatchRequest,
    StaleSweepClaimConflict,
    StaleSweepRescheduleRequest,
    StaleSweepRunAction,
    StaleSweepRunReceipt,
    StaleSweepScheduleReceipt,
    StaleSweepScheduleRequest,
)


_MODELS = {
    "story": Story,
    "ideation": Ideation,
    "refinement": Refinement,
    "spec": Spec,
    "sprint": Sprint,
    "card": Card,
    "amendment_hotfix_revision": AmendmentHotfixRevision,
}

_QUALITY_QA_BINDINGS = {
    "ideation": (IdeationQAItem, "ideation_id"),
    "refinement": (RefinementQAItem, "refinement_id"),
    "spec": (SpecQAItem, "spec_id"),
}

_DELETION_INTENT_SCHEMA_VERSION = 1
_GOVERNED_DELETION_ARTIFACT_TYPES = frozenset(
    {"card", "spec", "ideation", "refinement", "sprint"}
)


def _event_trigger_marker(event_id: str) -> str:
    """Keep queue trace markers within the legacy 100-character column."""

    if len(event_id) <= 100:
        return event_id
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"catchup:{digest}"


def _stale_sweep_payload(*, cursor: str, budget: int, attempt: int) -> dict[str, Any]:
    return {
        "cursor": cursor,
        "budget": budget,
        # Immutable zero-based catch-up epoch. Retry accounting lives in the
        # queue's ``attempts`` column so replay never changes synthetic IDs.
        "attempt": attempt,
    }


def _parse_stale_sweep_payload(payload: Any) -> tuple[str, int, int]:
    if not isinstance(payload, dict):
        raise RuntimeError("stale_sweep_payload_invalid")
    cursor = payload.get("cursor")
    budget = payload.get("budget")
    attempt = payload.get("attempt", 0)
    if (
        not isinstance(cursor, str)
        or isinstance(budget, bool)
        or not isinstance(budget, int)
        or budget < 1
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 0
        or not set(payload).issubset({"cursor", "budget", "attempt"})
    ):
        raise RuntimeError("stale_sweep_payload_invalid")
    return cursor, budget, attempt


def _validate_deletion_identity(
    *,
    board_id: str,
    artifact_type: str,
    artifact_id: str,
    delete_event_id: str,
) -> None:
    if (
        not board_id
        or artifact_type not in _GOVERNED_DELETION_ARTIFACT_TYPES
        or not artifact_id
        or not delete_event_id
        or len(delete_event_id) > 255
    ):
        raise ValueError("governed_deletion_identity_invalid")


def _queue_record(row: Any) -> ConsolidationQueueRecord:
    return ConsolidationQueueRecord(
        id=str(row.id),
        board_id=str(row.board_id),
        artifact_type=str(row.artifact_type),
        artifact_id=str(row.artifact_id),
        status=str(row.status),
        attempts=int(row.attempts or 0),
        last_error=row.last_error,
        next_retry_at=row.next_retry_at,
        claimed_at=row.claimed_at,
        claim_timeout_at=row.claim_timeout_at,
        worker_id=row.worker_id,
        claimed_by_session_id=row.claimed_by_session_id,
        triggered_at=row.triggered_at,
        priority=str(getattr(row.priority, "value", row.priority)),
        source=str(getattr(row, "source", None) or "state_transition"),
        work_kind=str(row.work_kind),
        generation=int(row.generation or 0),
        payload=row.payload,
        delete_event_id=row.delete_event_id,
        claim_token=row.claim_token,
        triggered_by_event=row.triggered_by_event,
    )


def _deferred_rebuild_live_intent(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    candidate = payload.get("_rebuild_deferred_live")
    if not isinstance(candidate, Mapping):
        return None
    source = str(candidate.get("source") or "").strip()
    if not source or source.startswith("rebuild:"):
        raise RuntimeError("rebuild_deferred_live_source_invalid")
    live_payload = candidate.get("payload")
    if live_payload is not None and not isinstance(live_payload, Mapping):
        raise RuntimeError("rebuild_deferred_live_payload_invalid")
    triggered_by_event = candidate.get("triggered_by_event")
    return {
        "source": source,
        "triggered_by_event": (
            str(triggered_by_event) if triggered_by_event is not None else None
        ),
        "payload": dict(live_payload) if live_payload is not None else None,
    }


async def _stage_intent_created_transition(
    context: Any,
    request: ReconcileIntentCreate,
    *,
    occurred_at: Any,
) -> None:
    from okto_pulse.core.ports.delivery_ledger import build_delivery_key

    await stage_takedown_transition(
        context,
        TakedownTransition(
            delete_event_id=request.delete_event_id,
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            generation=request.generation,
            state=TakedownState.INTENT_CREATED,
            occurred_at=occurred_at,
            # The delivery identity is deterministic from the immutable
            # deletion tuple and exists before worker pickup. Persist it on the
            # first transition so the receipt's delivery_key is immediately
            # queryable after the caller commits.
            delivery_key=build_delivery_key(
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
            ),
            details={
                "source": (
                    "stale_sweep_catchup"
                    if request.delete_event_id.startswith("catchup:")
                    else "governed_delete"
                )
            },
        ),
    )


def _apply_queue(row: Any, record: ConsolidationQueueRecord) -> None:
    for field_name in (
        "status",
        "attempts",
        "last_error",
        "next_retry_at",
        "claimed_at",
        "claim_timeout_at",
        "worker_id",
        "claimed_by_session_id",
        "claim_token",
    ):
        setattr(row, field_name, getattr(record, field_name))


class CommunitySqlAlchemyConsolidationPersistence:
    async def _load_code_investigation_receipt(
        self,
        context: Any,
        *,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        row = (
            await context.execute(
                select(
                    CodeInvestigationReceiptRow,
                    CodeInvestigationReceiptRevocationRow,
                    CodeInvestigationHeadRow,
                )
                .outerjoin(
                    CodeInvestigationReceiptRevocationRow,
                    CodeInvestigationReceiptRevocationRow.receipt_id
                    == CodeInvestigationReceiptRow.id,
                )
                .outerjoin(
                    CodeInvestigationHeadRow,
                    and_(
                        CodeInvestigationHeadRow.board_id
                        == CodeInvestigationReceiptRow.board_id,
                        CodeInvestigationHeadRow.source_ref
                        == CodeInvestigationReceiptRow.source_ref,
                    ),
                )
                .where(CodeInvestigationReceiptRow.id == artifact_id)
            )
        ).first()
        if row is None:
            return None
        receipt, revocation, head = row
        status = "accepted"
        if revocation is not None:
            status = "revoked"
        elif receipt.trust_level == "conflicted" or (
            head is not None
            and head.latest_receipt_id == receipt.id
            and head.state == "conflicted"
        ):
            status = "conflicted"
        return {
            "id": receipt.id,
            "board_id": receipt.board_id,
            "status": status,
            "investigation_source_ref": receipt.source_ref,
            "attestor_actor_id": receipt.attestor_actor_id,
            "declared_revision": receipt.declared_revision,
            "workspace_state_id": receipt.workspace_state_id,
            "trust_level": receipt.trust_level,
            "outcome": receipt.outcome,
            "generation": receipt.generation,
            "payload_sha256": receipt.payload_sha256,
            "content_hash": receipt.payload_sha256,
        }

    async def _load_code_evidence(
        self,
        context: Any,
        *,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        row = (
            await context.execute(
                select(CodeEvidenceRow, CodeInvestigationReceiptRow)
                .join(
                    CodeInvestigationReceiptRow,
                    CodeInvestigationReceiptRow.id
                    == CodeEvidenceRow.investigation_receipt_id,
                )
                .where(CodeEvidenceRow.id == artifact_id)
            )
        ).first()
        if row is None:
            return None
        evidence, receipt = row
        if (
            evidence.board_id != receipt.board_id
            or evidence.source_ref != receipt.source_ref
        ):
            raise RuntimeError("code_evidence_projection_receipt_scope_mismatch")
        links = tuple(
            (
                await context.execute(
                    select(CodeEvidenceSpecLinkRow)
                    .where(CodeEvidenceSpecLinkRow.evidence_id == evidence.id)
                    .order_by(CodeEvidenceSpecLinkRow.id)
                )
            )
            .scalars()
            .all()
        )
        return {
            "id": evidence.id,
            "board_id": evidence.board_id,
            "lifecycle_status": evidence.lifecycle_status,
            "investigation_receipt_id": evidence.investigation_receipt_id,
            "investigation_source_ref": evidence.source_ref,
            "declared_revision": evidence.declared_revision,
            "workspace_state_id": evidence.workspace_state_id,
            "relative_path": evidence.relative_path,
            "qualified_symbol": evidence.qualified_symbol,
            "symbol_kind": evidence.symbol_kind,
            "selector_kind": evidence.selector_kind,
            "snapshot_line_start": evidence.snapshot_line_start,
            "snapshot_line_end": evidence.snapshot_line_end,
            "declared_source_content_sha256": (evidence.declared_source_content_sha256),
            "evidence_type": evidence.evidence_type,
            "claim": evidence.claim,
            "supersedes_evidence_id": evidence.supersedes_evidence_id,
            "content_hash": evidence.payload_sha256,
            "spec_links": [
                {
                    "id": link.id,
                    "spec_id": link.spec_id,
                    "entity_type": link.entity_type,
                    "entity_id": link.entity_id,
                    "relation_type": link.relation_type,
                }
                for link in links
            ],
        }

    async def _load_implementation_target(
        self,
        context: Any,
        *,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        row = (
            await context.execute(
                select(
                    ImplementationTargetRow,
                    Card,
                    ImplementationTargetResolutionRow,
                )
                .join(Card, Card.id == ImplementationTargetRow.card_id)
                .outerjoin(
                    ImplementationTargetResolutionRow,
                    and_(
                        ImplementationTargetResolutionRow.id
                        == ImplementationTargetRow.current_resolution_id,
                        ImplementationTargetResolutionRow.target_id
                        == ImplementationTargetRow.id,
                        ImplementationTargetResolutionRow.board_id
                        == ImplementationTargetRow.board_id,
                        ImplementationTargetResolutionRow.target_revision
                        == ImplementationTargetRow.revision,
                    ),
                )
                .where(ImplementationTargetRow.id == artifact_id)
            )
        ).first()
        if row is None:
            return None
        target, card, resolution = row
        if target.board_id != card.board_id:
            raise RuntimeError("implementation_target_projection_card_scope_mismatch")
        if target.current_resolution_id is not None and resolution is None:
            raise RuntimeError("implementation_target_projection_resolution_dangling")
        if resolution is not None and resolution.source_ref != target.source_ref:
            raise RuntimeError("implementation_target_projection_source_mismatch")

        evidence_links = tuple(
            (
                await context.execute(
                    select(ImplementationTargetEvidenceLinkRow)
                    .where(ImplementationTargetEvidenceLinkRow.target_id == target.id)
                    .order_by(ImplementationTargetEvidenceLinkRow.id)
                )
            )
            .scalars()
            .all()
        )

        overlap_target_ids: list[str] = []
        if target.lifecycle_status == "active":
            from okto_pulse.community.adapters.sqlalchemy_code_traceability import (
                CommunitySqlAlchemyCodeTraceabilityStore,
            )
            from okto_pulse.core.ports.code_traceability import TargetOverlapQuery

            overlaps = await CommunitySqlAlchemyCodeTraceabilityStore(
                context
            ).overlap_report(
                TargetOverlapQuery(
                    board_id=target.board_id,
                    card_id=target.card_id,
                    include_informational=True,
                )
            )
            overlap_target_ids = sorted(
                {
                    (
                        overlap.target_b_id
                        if overlap.target_a_id == target.id
                        else overlap.target_a_id
                    )
                    for overlap in overlaps
                    if target.id in {overlap.target_a_id, overlap.target_b_id}
                }
            )

        card_type = getattr(card.card_type, "value", card.card_type)
        payload: dict[str, Any] = {
            "id": target.id,
            "board_id": target.board_id,
            "card_id": target.card_id,
            "card_node_type": "Bug" if card_type == "bug" else "Entity",
            "investigation_source_ref": target.source_ref,
            "selector_kind": target.selector_kind,
            "relative_path_hint": target.relative_path_hint,
            "qualified_symbol": target.qualified_symbol,
            "symbol_kind": target.symbol_kind,
            "role": target.role,
            "intent": target.intent,
            "lifecycle_status": target.lifecycle_status,
            "revision": target.revision,
            "baseline_evidence_id": target.baseline_evidence_id,
            "resolution_state": None,
            "investigation_receipt_id": None,
            "declared_revision": None,
            "workspace_state_id": None,
            "selector_fingerprint": None,
            "resolved_relative_path": None,
            "resolved_qualified_symbol": None,
            "resolved_symbol_kind": None,
            "resolved_line_start": None,
            "resolved_line_end": None,
            "payload_sha256": None,
            "content_hash": None,
            "evidence_links": [
                {
                    "id": link.id,
                    "evidence_id": link.evidence_id,
                    "relation_type": link.relation_type,
                }
                for link in evidence_links
            ],
            "overlap_target_ids": overlap_target_ids,
        }
        if resolution is not None:
            payload.update(
                {
                    "resolution_state": resolution.state,
                    "investigation_receipt_id": (resolution.investigation_receipt_id),
                    "declared_revision": resolution.declared_revision,
                    "workspace_state_id": resolution.workspace_state_id,
                    "selector_fingerprint": resolution.selector_fingerprint,
                    "resolved_relative_path": resolution.resolved_relative_path,
                    "resolved_qualified_symbol": (resolution.resolved_qualified_symbol),
                    "resolved_symbol_kind": resolution.resolved_symbol_kind,
                    "resolved_line_start": resolution.resolved_line_start,
                    "resolved_line_end": resolution.resolved_line_end,
                    "payload_sha256": resolution.payload_sha256,
                    "content_hash": resolution.payload_sha256,
                }
            )
        return payload

    async def load_artifact(
        self, context: Any, *, artifact_type: str, artifact_id: str
    ) -> Any | None:
        if artifact_type == "code_investigation_receipt":
            return await self._load_code_investigation_receipt(
                context,
                artifact_id=artifact_id,
            )
        if artifact_type == "code_evidence":
            return await self._load_code_evidence(
                context,
                artifact_id=artifact_id,
            )
        if artifact_type == "implementation_target":
            return await self._load_implementation_target(
                context,
                artifact_id=artifact_id,
            )
        model = _MODELS.get(artifact_type)
        if model is None:
            return None
        statement = select(model).where(model.id == artifact_id)
        if artifact_type == "ideation":
            statement = statement.options(selectinload(Ideation.story_links))
        elif artifact_type == "spec":
            statement = statement.options(selectinload(Spec.architecture_designs))
        elif artifact_type == "sprint":
            statement = statement.options(selectinload(Sprint.spec))
        elif artifact_type == "card":
            statement = statement.options(selectinload(Card.architecture_designs))
        return (await context.execute(statement)).scalars().first()

    async def load_projection_inputs(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        artifact: Any | None = None,
    ) -> ConsolidationProjectionInputs:
        if artifact_type not in {"ideation", "refinement", "spec"}:
            return ConsolidationProjectionInputs()
        if not board_id or not artifact_id:
            raise ValueError("consolidation_projection_scope_invalid")

        quality_rows = (
            await context.execute(
                select(
                    QualityAssessmentHeadRow,
                    QualityAssessmentReceiptRow,
                )
                .outerjoin(
                    QualityAssessmentReceiptRow,
                    and_(
                        QualityAssessmentReceiptRow.id
                        == QualityAssessmentHeadRow.receipt_id,
                        QualityAssessmentReceiptRow.board_id
                        == QualityAssessmentHeadRow.board_id,
                        QualityAssessmentReceiptRow.subject_type
                        == QualityAssessmentHeadRow.subject_type,
                        QualityAssessmentReceiptRow.subject_id
                        == QualityAssessmentHeadRow.subject_id,
                        QualityAssessmentReceiptRow.assessment_kind
                        == QualityAssessmentHeadRow.assessment_kind,
                    ),
                )
                .where(
                    QualityAssessmentHeadRow.board_id == board_id,
                    QualityAssessmentHeadRow.subject_type == artifact_type,
                    QualityAssessmentHeadRow.subject_id == artifact_id,
                )
                .order_by(
                    QualityAssessmentHeadRow.assessment_kind.asc(),
                    QualityAssessmentHeadRow.receipt_id.asc(),
                )
            )
        ).all()
        for head, receipt in quality_rows:
            if receipt is None:
                raise RuntimeError("quality_projection_head_dangling")
            if (
                receipt.id != head.receipt_id
                or receipt.board_id != board_id
                or receipt.subject_type != artifact_type
                or receipt.subject_id != artifact_id
                or receipt.assessment_kind != head.assessment_kind
                or receipt.head_revision != head.revision
            ):
                raise RuntimeError("quality_projection_scope_mismatch")

        board_settings: dict[str, object] = {}
        qa_items: list[object] = []
        if quality_rows:
            expected_model = _MODELS[artifact_type]
            if (
                artifact is None
                or not isinstance(artifact, expected_model)
                or str(getattr(artifact, "id", "")) != artifact_id
                or str(getattr(artifact, "board_id", "")) != board_id
            ):
                raise RuntimeError("quality_projection_subject_mismatch")
            qa_model, subject_fk = _QUALITY_QA_BINDINGS[artifact_type]
            context_rows = (
                await context.execute(
                    select(Board.settings, qa_model)
                    .select_from(Board)
                    .outerjoin(
                        qa_model,
                        getattr(qa_model, subject_fk) == artifact_id,
                    )
                    .where(Board.id == board_id)
                    .order_by(qa_model.id.asc())
                )
            ).all()
            if not context_rows:
                raise RuntimeError("quality_projection_board_missing")
            settings_value = context_rows[0][0]
            if settings_value is not None and not isinstance(
                settings_value,
                dict,
            ):
                raise RuntimeError("quality_projection_board_settings_invalid")
            board_settings = dict(settings_value or {})
            qa_items = [row[1] for row in context_rows if row[1] is not None]

        quality_assessments: list[CurrentQualityAssessmentSummary] = []
        for head, receipt in quality_rows:
            try:
                assessed_digests = AssessmentDigestSet(
                    content_digest=receipt.content_digest,
                    clarification_digest=receipt.clarification_digest,
                    ruleset_digest=receipt.ruleset_digest,
                    taxonomy_digest=receipt.taxonomy_digest,
                    policy_digest=receipt.policy_digest,
                    input_digest=receipt.input_digest,
                    canonicalization_version=(receipt.canonicalization_version),
                )
                currentness = evaluate_quality_projection_currentness(
                    board_id=board_id,
                    subject_type=artifact_type,
                    subject_id=artifact_id,
                    assessed_subject_version=receipt.subject_version,
                    assessed_subject_edition=receipt.subject_edition,
                    assessed_digests=assessed_digests,
                    assessment_kind=receipt.assessment_kind,
                    origin=receipt.origin,
                    source=receipt.source,
                    current_subject=artifact,
                    qa_items=qa_items,
                    board_settings=board_settings,
                )
            except (QualityProjectionCurrentnessError, ValueError) as exc:
                raise RuntimeError(
                    "quality_projection_currentness_unresolvable"
                ) from exc
            if not currentness.current:
                continue
            fingerprint_payload = {
                "board_id": receipt.board_id,
                "subject_type": receipt.subject_type,
                "subject_id": receipt.subject_id,
                "subject_version": receipt.subject_version,
                "subject_edition": receipt.subject_edition,
                "assessment_kind": receipt.assessment_kind,
                "receipt_id": receipt.id,
                "head_revision": head.revision,
                "outcome": receipt.outcome,
                "score": receipt.score,
                "justification": receipt.justification,
                "scale_kind": receipt.scale_kind,
                "scale_minimum": receipt.scale_minimum,
                "scale_maximum": receipt.scale_maximum,
                "scale_direction": receipt.scale_direction,
                "content_digest": receipt.content_digest,
                "clarification_digest": receipt.clarification_digest,
                "ruleset_digest": receipt.ruleset_digest,
                "taxonomy_digest": receipt.taxonomy_digest,
                "policy_digest": receipt.policy_digest,
                "input_digest": receipt.input_digest,
                "canonicalization_version": receipt.canonicalization_version,
                "ruleset_version": receipt.ruleset_version,
                "taxonomy_version": receipt.taxonomy_version,
                "analyzer_version": receipt.analyzer_version,
                "policy_version": receipt.policy_version,
                "created_at": receipt.created_at,
                "updated_at": head.updated_at,
            }
            quality_assessments.append(
                CurrentQualityAssessmentSummary(
                    board_id=receipt.board_id,
                    subject_type=receipt.subject_type,
                    subject_id=receipt.subject_id,
                    subject_version=receipt.subject_version,
                    subject_edition=receipt.subject_edition,
                    assessment_kind=receipt.assessment_kind,
                    receipt_id=receipt.id,
                    head_revision=head.revision,
                    outcome=receipt.outcome,
                    score=receipt.score,
                    justification=receipt.justification,
                    scale_kind=receipt.scale_kind,
                    scale_minimum=receipt.scale_minimum,
                    scale_maximum=receipt.scale_maximum,
                    scale_direction=receipt.scale_direction,
                    content_digest=receipt.content_digest,
                    clarification_digest=receipt.clarification_digest,
                    ruleset_digest=receipt.ruleset_digest,
                    taxonomy_digest=receipt.taxonomy_digest,
                    policy_digest=receipt.policy_digest,
                    input_digest=receipt.input_digest,
                    canonicalization_version=(receipt.canonicalization_version),
                    ruleset_version=receipt.ruleset_version,
                    taxonomy_version=receipt.taxonomy_version,
                    analyzer_version=receipt.analyzer_version,
                    policy_version=receipt.policy_version,
                    created_at=receipt.created_at,
                    updated_at=head.updated_at,
                    projection_fingerprint=(
                        quality_current_head_fingerprint(fingerprint_payload)
                    ),
                )
            )

        if artifact_type == "spec":
            prerequisite = aliased(Spec)
            dependency_rows = (
                await context.execute(
                    select(SpecDependency, prerequisite)
                    .join(
                        prerequisite,
                        prerequisite.id == SpecDependency.prerequisite_spec_id,
                    )
                    .where(
                        SpecDependency.board_id == board_id,
                        SpecDependency.dependent_spec_id == artifact_id,
                        SpecDependency.active.is_(True),
                    )
                    .order_by(
                        SpecDependency.prerequisite_spec_ref.asc(),
                        SpecDependency.id.asc(),
                    )
                )
            ).all()
            dependencies: list[CurrentSpecDependencyProjection] = []
            for dependency, target in dependency_rows:
                target_id = str(dependency.prerequisite_spec_id or "")
                if (
                    not target_id
                    or target_id != str(dependency.prerequisite_spec_ref)
                    or str(target.id) != target_id
                    or str(target.board_id) != board_id
                    or str(dependency.dependent_spec_id) != artifact_id
                ):
                    raise RuntimeError("spec_dependency_projection_scope_mismatch")
                dependencies.append(
                    CurrentSpecDependencyProjection(
                        dependency_id=str(dependency.id),
                        board_id=board_id,
                        dependent_spec_id=artifact_id,
                        prerequisite_spec_id=target_id,
                        prerequisite_title=str(target.title or ""),
                        prerequisite_status=str(
                            getattr(target.status, "value", target.status)
                        ),
                        prerequisite_version=int(target.version),
                    )
                )
            return ConsolidationProjectionInputs(
                quality_assessments=tuple(quality_assessments),
                spec_dependencies=tuple(dependencies),
            )

        if artifact_type != "refinement":
            return ConsolidationProjectionInputs(
                quality_assessments=tuple(quality_assessments)
            )

        research_rows = (
            await context.execute(
                select(
                    ResearchDecisionHeadRow,
                    ResearchDecisionEntryRow,
                )
                .outerjoin(
                    ResearchDecisionEntryRow,
                    ResearchDecisionEntryRow.id
                    == ResearchDecisionHeadRow.current_entry_id,
                )
                .where(
                    ResearchDecisionHeadRow.board_id == board_id,
                    ResearchDecisionHeadRow.refinement_id == artifact_id,
                )
                .order_by(
                    ResearchDecisionHeadRow.ledger_id.asc(),
                    ResearchDecisionHeadRow.current_entry_id.asc(),
                )
            )
        ).all()
        research_decisions: list[CurrentResearchDecisionSummary] = []
        for head, entry in research_rows:
            if entry is None:
                raise RuntimeError("research_decision_projection_head_dangling")
            if (
                entry.id != head.current_entry_id
                or entry.ledger_id != head.ledger_id
                or entry.board_id != board_id
                or entry.refinement_id != artifact_id
                or entry.refinement_version != head.refinement_version
                or entry.status != head.status
            ):
                raise RuntimeError("research_decision_projection_scope_mismatch")
            evidence_refs = tuple(str(value) for value in entry.evidence_refs or ())
            alternatives = tuple(str(value) for value in entry.alternatives or ())
            fingerprint_payload = {
                "board_id": entry.board_id,
                "refinement_id": entry.refinement_id,
                "refinement_version": entry.refinement_version,
                "ledger_id": entry.ledger_id,
                "entry_id": entry.id,
                "head_revision": head.revision,
                "predecessor_entry_id": entry.predecessor_entry_id,
                "unknown": entry.unknown,
                "status": entry.status,
                "anchor_type": entry.anchor_type,
                "anchor_ref": entry.anchor_ref,
                "evidence_refs": list(evidence_refs),
                "alternatives": list(alternatives),
                "decision": entry.decision,
                "rationale": entry.rationale,
                "confidence": entry.confidence,
                "evidence_absence_justification": (
                    entry.evidence_absence_justification
                ),
                "created_by": entry.created_by,
                "created_at": entry.created_at,
                "updated_at": head.updated_at,
            }
            research_decisions.append(
                CurrentResearchDecisionSummary(
                    board_id=entry.board_id,
                    refinement_id=entry.refinement_id,
                    refinement_version=entry.refinement_version,
                    ledger_id=entry.ledger_id,
                    entry_id=entry.id,
                    head_revision=head.revision,
                    predecessor_entry_id=entry.predecessor_entry_id,
                    unknown=entry.unknown,
                    status=entry.status,
                    anchor_type=entry.anchor_type,
                    anchor_ref=entry.anchor_ref,
                    evidence_refs=evidence_refs,
                    alternatives=alternatives,
                    decision=entry.decision,
                    rationale=entry.rationale,
                    confidence=entry.confidence,
                    evidence_absence_justification=(
                        entry.evidence_absence_justification
                    ),
                    created_by=entry.created_by,
                    created_at=entry.created_at,
                    updated_at=head.updated_at,
                    projection_fingerprint=(
                        research_decision_current_head_fingerprint(fingerprint_payload)
                    ),
                )
            )
        return ConsolidationProjectionInputs(
            quality_assessments=tuple(quality_assessments),
            research_decisions=tuple(research_decisions),
        )

    async def list_artifacts(
        self,
        context: Any,
        *,
        artifact_type: str,
        artifact_ids: Sequence[str],
        board_id: str | None = None,
    ) -> tuple[Any, ...]:
        model = _MODELS.get(artifact_type)
        if model is None or not artifact_ids:
            return ()
        statement = select(model).where(model.id.in_(tuple(artifact_ids)))
        if board_id is not None and hasattr(model, "board_id"):
            statement = statement.where(model.board_id == board_id)
        return tuple((await context.execute(statement)).scalars().all())

    async def list_stale_claims(
        self, context: Any, *, now, legacy_cutoff
    ) -> tuple[ConsolidationQueueRecord, ...]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue).where(
                        ConsolidationQueue.status == "claimed",
                        or_(
                            # Rows claimed before the claim-token migration cannot
                            # prove ownership and must be recovered immediately.
                            ConsolidationQueue.claim_token.is_(None),
                            (
                                ConsolidationQueue.claim_timeout_at.is_not(None)
                                & (ConsolidationQueue.claim_timeout_at < now)
                            ),
                            (
                                ConsolidationQueue.claim_timeout_at.is_(None)
                                & ConsolidationQueue.claimed_at.is_not(None)
                                & (ConsolidationQueue.claimed_at < legacy_cutoff)
                            ),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_queue_record(row) for row in rows)

    async def count_pending(self, context: Any) -> int:
        value = await context.scalar(
            select(func.count()).where(ConsolidationQueue.status == "pending")
        )
        return int(value or 0)

    async def list_claimed_board_ids(self, context: Any) -> frozenset[str]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue.board_id).where(
                        ConsolidationQueue.status == "claimed"
                    )
                )
            )
            .scalars()
            .all()
        )
        return frozenset(str(value) for value in rows)

    async def list_ready_pending(
        self, context: Any, *, now
    ) -> tuple[ConsolidationQueueRecord, ...]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue)
                    .where(
                        ConsolidationQueue.status == "pending",
                    )
                    .order_by(
                        ConsolidationQueue.priority.asc(),
                        ConsolidationQueue.triggered_at.asc(),
                        ConsolidationQueue.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_queue_record(row) for row in rows)

    async def list_ready_pending_exact(
        self,
        context: Any,
        *,
        now,
        board_id: str,
        source: str,
        work_kind: str,
    ) -> tuple[ConsolidationQueueRecord, ...]:
        """List one recovery fence without exposing unrelated ready work."""

        row = (
            (
                await context.execute(
                    select(ConsolidationQueue)
                    .where(
                        ConsolidationQueue.status.in_(("pending", "claimed")),
                        ConsolidationQueue.board_id == board_id,
                        ConsolidationQueue.source == source,
                        ConsolidationQueue.work_kind == work_kind,
                    )
                    .order_by(
                        ConsolidationQueue.priority.asc(),
                        ConsolidationQueue.triggered_at.asc(),
                        ConsolidationQueue.id.asc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None or row.status != "pending":
            return ()
        comparable_now = now
        if row.next_retry_at is not None:
            if row.next_retry_at.tzinfo is None and now.tzinfo is not None:
                comparable_now = now.replace(tzinfo=None)
            if row.next_retry_at > comparable_now:
                return ()
        return (_queue_record(row),)

    async def list_claimed_exact(
        self,
        context: Any,
        *,
        board_id: str,
        source: str,
        work_kind: str,
    ) -> tuple[ConsolidationQueueRecord, ...]:
        """List claimed membership for one offline recovery reservation."""

        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue)
                    .where(
                        ConsolidationQueue.status == "claimed",
                        ConsolidationQueue.board_id == board_id,
                        ConsolidationQueue.source == source,
                        ConsolidationQueue.work_kind == work_kind,
                    )
                    .order_by(
                        ConsolidationQueue.priority.asc(),
                        ConsolidationQueue.triggered_at.asc(),
                        ConsolidationQueue.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_queue_record(row) for row in rows)

    async def claim_ready_pending_exact(
        self,
        context: Any,
        *,
        entry_id: str,
        board_id: str,
        source: str,
        work_kind: str,
        generation: int,
        now,
        claim_timeout_at,
        worker_id: str,
        claim_token: str,
    ) -> ConsolidationQueueRecord | None:
        """CAS an exact recovery head after listing and before graph work."""

        active_head_id = (
            select(ConsolidationQueue.id)
            .where(
                ConsolidationQueue.status.in_(("pending", "claimed")),
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == source,
                ConsolidationQueue.work_kind == work_kind,
            )
            .order_by(
                ConsolidationQueue.priority.asc(),
                ConsolidationQueue.triggered_at.asc(),
                ConsolidationQueue.id.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        row = (
            await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == entry_id,
                    ConsolidationQueue.id == active_head_id,
                    ConsolidationQueue.status == "pending",
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.source == source,
                    ConsolidationQueue.work_kind == work_kind,
                    ConsolidationQueue.generation == generation,
                    or_(
                        ConsolidationQueue.next_retry_at.is_(None),
                        ConsolidationQueue.next_retry_at <= now,
                    ),
                )
                .values(
                    status="claimed",
                    claimed_at=now,
                    claim_timeout_at=claim_timeout_at,
                    worker_id=worker_id,
                    claimed_by_session_id=worker_id,
                    claim_token=claim_token,
                )
                .returning(ConsolidationQueue)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        return _queue_record(row) if row is not None else None

    async def board_administrative_rebuild_source(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> str | None:
        """Read the canonical reservation used by rebuild and erasure.

        ``context`` is intentionally accepted for the Core persistence seam;
        the reservation itself is owned by the configured coordination port,
        not by the relational transaction.
        """

        del context
        from okto_pulse.core.kg.single_writer_lock import (
            KGAdministrativeOperationReservation,
        )

        reservation = KGAdministrativeOperationReservation().inspect(board_id=board_id)
        if (
            reservation is None
            or reservation.expires_at_epoch <= datetime.now(timezone.utc).timestamp()
        ):
            return None
        prefix = "kg02_rebuild_reservation:"
        if reservation.operation.startswith(prefix):
            manifest_ref = reservation.operation.removeprefix(prefix)
            if manifest_ref:
                return f"rebuild:{manifest_ref}"
        # A different/invalid administrative operation still reserves the
        # board, but authorizes no queue membership.
        return ""

    async def get_queue_entry(
        self, context: Any, *, entry_id: str
    ) -> ConsolidationQueueRecord | None:
        row = await context.get(ConsolidationQueue, entry_id)
        return _queue_record(row) if row is not None else None

    async def queue_claim_is_current_and_unfenced(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        work_kind: str,
        source: str,
        generation: int,
        delete_event_id: str | None,
    ) -> bool:
        """Atomically re-check claim ownership and its deletion fence."""

        if not entry_id or not claim_token:
            return False

        claim_predicates = (
            ConsolidationQueue.id == entry_id,
            ConsolidationQueue.status == "claimed",
            ConsolidationQueue.claim_token == claim_token,
            ConsolidationQueue.board_id == board_id,
            ConsolidationQueue.artifact_type == artifact_type,
            ConsolidationQueue.artifact_id == artifact_id,
            ConsolidationQueue.work_kind == work_kind,
            ConsolidationQueue.source == source,
            ConsolidationQueue.generation == generation,
            (
                ConsolidationQueue.delete_event_id.is_(None)
                if delete_event_id is None
                else ConsolidationQueue.delete_event_id == delete_event_id
            ),
        )
        tombstone_key = (
            ArtifactDeletionTombstone.board_id == board_id,
            ArtifactDeletionTombstone.artifact_type == artifact_type,
            ArtifactDeletionTombstone.artifact_id == artifact_id,
        )

        if work_kind == "consolidate":
            if generation != 0 or delete_event_id is not None:
                return False
            deletion_fence = ~exists(select(1).where(*tombstone_key))
        elif work_kind == "stale_reconcile":
            if generation < 1 or delete_event_id is None:
                return False
            deletion_fence = exists(
                select(1).where(
                    *tombstone_key,
                    ArtifactDeletionTombstone.generation == generation,
                    ArtifactDeletionTombstone.delete_event_id == delete_event_id,
                )
            )
        elif work_kind == STALE_SWEEP_WORK_KIND:
            if (
                artifact_type != STALE_SWEEP_ARTIFACT_TYPE
                or artifact_id != board_id
                or generation != 0
                or delete_event_id is not None
            ):
                return False
            deletion_fence = exists(select(1).where(Board.id == board_id))
        else:
            return False

        # A read-only SELECT is not a sufficient linearization point in WAL
        # mode: a governed delete could commit after the read and before the
        # external graph mutation.  This conditional no-op UPDATE acquires the
        # SQLite writer slot, evaluates claim + tombstone in that same write
        # statement, and keeps the delete UoW serialized until the worker ACK
        # commits.  A concurrent delete that already won makes the predicate
        # return no row (or the write upgrade fail), both fail-closed outcomes.
        statement = (
            update(ConsolidationQueue)
            .where(*claim_predicates, deletion_fence)
            .values(claim_token=ConsolidationQueue.claim_token)
            .returning(ConsolidationQueue.id)
            .execution_options(synchronize_session=False)
        )
        matched = (await context.execute(statement)).scalar_one_or_none()
        return matched is not None

    async def ack_claimed_queue_entry(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        source: str,
        work_kind: str,
        generation: int,
        delete_event_id: str | None,
    ) -> bool:
        """Delete exactly the generation still owned by ``claim_token``."""

        if not entry_id or not claim_token:
            return False
        delete_event_predicate = (
            ConsolidationQueue.delete_event_id.is_(None)
            if delete_event_id is None
            else ConsolidationQueue.delete_event_id == delete_event_id
        )
        claim_predicates = (
            ConsolidationQueue.id == entry_id,
            ConsolidationQueue.status == "claimed",
            ConsolidationQueue.claim_token == claim_token,
            ConsolidationQueue.board_id == board_id,
            ConsolidationQueue.source == source,
            ConsolidationQueue.work_kind == work_kind,
            ConsolidationQueue.generation == generation,
            delete_event_predicate,
        )
        claimed = (
            await context.execute(
                select(ConsolidationQueue).where(*claim_predicates).with_for_update()
            )
        ).scalar_one_or_none()
        if claimed is None:
            return False
        deferred_live = (
            _deferred_rebuild_live_intent(claimed.payload)
            if source.startswith("rebuild:")
            else None
        )
        if deferred_live is not None:
            result = await context.execute(
                update(ConsolidationQueue)
                .where(*claim_predicates)
                .values(
                    status="pending",
                    attempts=0,
                    last_error=None,
                    next_retry_at=None,
                    source=deferred_live["source"],
                    triggered_by_event=deferred_live["triggered_by_event"],
                    payload=deferred_live["payload"],
                    triggered_at=func.now(),
                    claimed_by_session_id=None,
                    claim_token=None,
                    claimed_at=None,
                    worker_id=None,
                    claim_timeout_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            return int(result.rowcount or 0) == 1
        result = await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.id == entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == claim_token,
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == source,
                ConsolidationQueue.work_kind == work_kind,
                ConsolidationQueue.generation == generation,
                delete_event_predicate,
            )
        )
        return int(result.rowcount or 0) == 1

    async def repend_claimed_queue_entry(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        source: str,
        work_kind: str,
        generation: int,
        delete_event_id: str | None,
    ) -> bool:
        """Release one exact claim without changing its durable work intent."""

        if not entry_id or not claim_token:
            return False
        delete_event_predicate = (
            ConsolidationQueue.delete_event_id.is_(None)
            if delete_event_id is None
            else ConsolidationQueue.delete_event_id == delete_event_id
        )
        result = await context.execute(
            update(ConsolidationQueue)
            .where(
                ConsolidationQueue.id == entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == claim_token,
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == source,
                ConsolidationQueue.work_kind == work_kind,
                ConsolidationQueue.generation == generation,
                delete_event_predicate,
            )
            .values(
                status="pending",
                claimed_by_session_id=None,
                claim_token=None,
                claimed_at=None,
                worker_id=None,
                claim_timeout_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0) == 1

    async def save_queue_entries(
        self, context: Any, entries: Sequence[ConsolidationQueueRecord]
    ) -> None:
        for entry in entries:
            row = await context.get(ConsolidationQueue, entry.id)
            if row is not None:
                _apply_queue(row, entry)
        await context.flush()

    async def delete_queue_entry(self, context: Any, *, entry_id: str) -> None:
        row = await context.get(ConsolidationQueue, entry_id)
        if row is not None:
            await context.delete(row)
            await context.flush()

    async def discard_artifact_work(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
    ) -> None:
        """Remove operational rows made obsolete by a governed hard delete."""

        await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.artifact_type == artifact_type,
                ConsolidationQueue.artifact_id == artifact_id,
                ConsolidationQueue.work_kind == "consolidate",
            )
        )
        for model in (ConsolidationDeadLetter, CanonicalDebt):
            await context.execute(
                delete(model).where(
                    model.board_id == board_id,
                    model.artifact_type == artifact_type,
                    model.artifact_id == artifact_id,
                )
            )
        await context.flush()

    async def advance_deletion_tombstone(
        self,
        context: Any,
        request: DeletionTombstoneAdvance,
    ) -> DeletionTombstoneReceipt:
        """Atomically create or advance the artifact's permanent fence."""

        _validate_deletion_identity(
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            delete_event_id=request.delete_event_id,
        )
        statement = (
            sqlite_insert(ArtifactDeletionTombstone)
            .values(
                id=str(uuid.uuid4()),
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=1,
                delete_event_id=request.delete_event_id,
            )
            .on_conflict_do_update(
                index_elements=["board_id", "artifact_type", "artifact_id"],
                set_={
                    "generation": case(
                        (
                            ArtifactDeletionTombstone.delete_event_id
                            == request.delete_event_id,
                            ArtifactDeletionTombstone.generation,
                        ),
                        else_=ArtifactDeletionTombstone.generation + 1,
                    ),
                    "delete_event_id": request.delete_event_id,
                    "updated_at": func.now(),
                },
            )
            .returning(
                ArtifactDeletionTombstone.generation,
                ArtifactDeletionTombstone.delete_event_id,
            )
        )
        try:
            row = (await context.execute(statement)).one()
        except IntegrityError as exc:
            # ``delete_event_id`` is globally unique.  Reusing it for a
            # different artifact is divergent history, never a replay.
            raise RuntimeError(
                "artifact_deletion_tombstone_delete_event_conflict"
            ) from exc
        await context.flush()
        return DeletionTombstoneReceipt(
            generation=int(row.generation),
            delete_event_id=str(row.delete_event_id),
        )

    async def persist_reconcile_intent(
        self,
        context: Any,
        request: ReconcileIntentCreate,
    ) -> ReconcileIntentReceipt:
        """Insert or replay the immutable stale-reconcile queue intent."""

        _validate_deletion_identity(
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            delete_event_id=request.delete_event_id,
        )
        source_refs = tuple(str(ref) for ref in request.source_refs)
        expected_refs = (f"{request.artifact_type}:{request.artifact_id}",)
        if request.generation < 1 or source_refs != expected_refs:
            raise ValueError("governed_reconcile_intent_invalid")
        if request.occurred_at is not None and not isinstance(
            request.occurred_at,
            datetime,
        ):
            raise ValueError("governed_reconcile_intent_occurred_at_invalid")

        tombstone = (
            await context.execute(
                select(ArtifactDeletionTombstone).where(
                    ArtifactDeletionTombstone.board_id == request.board_id,
                    ArtifactDeletionTombstone.artifact_type == request.artifact_type,
                    ArtifactDeletionTombstone.artifact_id == request.artifact_id,
                )
            )
        ).scalar_one_or_none()
        if (
            tombstone is None
            or int(tombstone.generation) != request.generation
            or str(tombstone.delete_event_id) != request.delete_event_id
        ):
            raise RuntimeError("governed_reconcile_intent_tombstone_mismatch")

        payload = {
            "schema_version": _DELETION_INTENT_SCHEMA_VERSION,
            "delete_event_id": request.delete_event_id,
            "source_refs": list(source_refs),
        }
        existing_intent_transition = await context.get(
            KGTakedownStateEvent,
            f"takedown:{request.delete_event_id}:intent_created",
        )
        authoritative_occurred_at = (
            existing_intent_transition.occurred_at
            if existing_intent_transition is not None
            else request.occurred_at
        )
        intent_id = str(uuid.uuid4())
        intent_values: dict[str, object] = {
            "id": intent_id,
            "board_id": request.board_id,
            "artifact_type": request.artifact_type,
            "artifact_id": request.artifact_id,
            "work_kind": "stale_reconcile",
            "generation": request.generation,
            "payload": payload,
            "delete_event_id": request.delete_event_id,
            "priority": "high",
            "source": "governed_delete",
            "status": "pending",
            "triggered_by_event": _event_trigger_marker(request.delete_event_id),
        }
        if authoritative_occurred_at is not None:
            intent_values["triggered_at"] = authoritative_occurred_at
        statement = (
            sqlite_insert(ConsolidationQueue)
            .values(**intent_values)
            .on_conflict_do_nothing(
                index_elements=[
                    "board_id",
                    "artifact_type",
                    "artifact_id",
                    "work_kind",
                    "generation",
                ],
                index_where=ConsolidationQueue.work_kind == "stale_reconcile",
            )
            .returning(
                ConsolidationQueue.id,
                ConsolidationQueue.generation,
                ConsolidationQueue.delete_event_id,
                ConsolidationQueue.triggered_at,
            )
        )
        inserted = (await context.execute(statement)).first()
        if inserted is not None:
            await _stage_intent_created_transition(
                context,
                request,
                occurred_at=inserted.triggered_at,
            )
            await context.flush()
            return ReconcileIntentReceipt(
                intent_id=str(inserted.id),
                generation=int(inserted.generation),
                delete_event_id=str(inserted.delete_event_id),
                created=True,
            )

        existing = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.board_id == request.board_id,
                    ConsolidationQueue.artifact_type == request.artifact_type,
                    ConsolidationQueue.artifact_id == request.artifact_id,
                    ConsolidationQueue.work_kind == "stale_reconcile",
                    ConsolidationQueue.generation == request.generation,
                )
            )
        ).scalar_one_or_none()
        if (
            existing is None
            or str(existing.delete_event_id) != request.delete_event_id
            or existing.payload != payload
        ):
            raise RuntimeError("governed_reconcile_intent_replay_conflict")
        await _stage_intent_created_transition(
            context,
            request,
            occurred_at=existing.triggered_at,
        )
        return ReconcileIntentReceipt(
            intent_id=str(existing.id),
            generation=int(existing.generation),
            delete_event_id=str(existing.delete_event_id),
            created=False,
        )

    async def schedule_stale_sweep(
        self,
        context: Any,
        request: StaleSweepScheduleRequest,
    ) -> StaleSweepScheduleReceipt:
        """Insert one low-priority coordinator without resetting active work."""

        board_present = await context.scalar(
            select(exists(select(1).where(Board.id == request.board_id)))
        )
        if not bool(board_present):
            return StaleSweepScheduleReceipt(
                board_id=request.board_id,
                sweep_id=None,
                scheduled=False,
                board_present=False,
                cursor="",
                budget=request.budget,
                attempt=0,
            )

        sweep_id = str(uuid.uuid4())
        payload = _stale_sweep_payload(
            cursor="",
            budget=request.budget,
            attempt=0,
        )
        inserted = (
            await context.execute(
                sqlite_insert(ConsolidationQueue)
                .values(
                    id=sweep_id,
                    board_id=request.board_id,
                    artifact_type=STALE_SWEEP_ARTIFACT_TYPE,
                    artifact_id=request.board_id,
                    work_kind=STALE_SWEEP_WORK_KIND,
                    generation=0,
                    payload=payload,
                    delete_event_id=None,
                    priority="low",
                    source="kg_tick",
                    status="pending",
                    triggered_at=request.now,
                    triggered_by_event=f"stale-sweep:{sweep_id}",
                )
                .on_conflict_do_nothing(
                    index_elements=["board_id", "work_kind"],
                    index_where=(ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND),
                )
                .returning(ConsolidationQueue.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            await context.flush()
            return StaleSweepScheduleReceipt(
                board_id=request.board_id,
                sweep_id=str(inserted),
                scheduled=True,
                board_present=True,
                cursor="",
                budget=request.budget,
                attempt=0,
            )

        existing = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.board_id == request.board_id,
                    ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise RuntimeError("stale_sweep_schedule_conflict_without_row")
        cursor, budget, attempt = _parse_stale_sweep_payload(existing.payload)
        return StaleSweepScheduleReceipt(
            board_id=request.board_id,
            sweep_id=str(existing.id),
            scheduled=False,
            board_present=True,
            cursor=cursor,
            budget=budget,
            attempt=attempt,
        )

    async def _lock_stale_sweep_claim(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        expected_cursor: str,
        expected_budget: int,
        expected_attempt: int,
    ) -> Any:
        """Acquire SQLite's writer slot and validate the exact checkpoint."""

        matched = (
            await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == claim_token,
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.artifact_type == STALE_SWEEP_ARTIFACT_TYPE,
                    ConsolidationQueue.artifact_id == board_id,
                    ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND,
                    ConsolidationQueue.generation == 0,
                    ConsolidationQueue.delete_event_id.is_(None),
                )
                .values(claim_token=ConsolidationQueue.claim_token)
                .returning(ConsolidationQueue.id)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if matched is None:
            raise StaleSweepClaimConflict(f"stale_sweep_claim_lost entry_id={entry_id}")
        row = (
            await context.execute(
                select(ConsolidationQueue).where(ConsolidationQueue.id == entry_id)
            )
        ).scalar_one()
        cursor, budget, attempt = _parse_stale_sweep_payload(row.payload)
        if (
            cursor != expected_cursor
            or budget != expected_budget
            or attempt != expected_attempt
        ):
            raise StaleSweepClaimConflict(
                f"stale_sweep_checkpoint_changed entry_id={entry_id}"
            )
        return row

    async def _ensure_catchup_tombstone(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        synthetic_event_id: str,
    ) -> DeletionTombstoneReceipt:
        """Insert generation one once, preserving any real tombstone."""

        inserted = (
            await context.execute(
                sqlite_insert(ArtifactDeletionTombstone)
                .values(
                    id=str(uuid.uuid4()),
                    board_id=board_id,
                    artifact_type=artifact_type,
                    artifact_id=artifact_id,
                    generation=1,
                    delete_event_id=synthetic_event_id,
                )
                .on_conflict_do_nothing()
                .returning(
                    ArtifactDeletionTombstone.generation,
                    ArtifactDeletionTombstone.delete_event_id,
                )
            )
        ).first()
        if inserted is not None:
            return DeletionTombstoneReceipt(
                generation=int(inserted.generation),
                delete_event_id=str(inserted.delete_event_id),
            )
        existing = (
            await context.execute(
                select(ArtifactDeletionTombstone).where(
                    ArtifactDeletionTombstone.board_id == board_id,
                    ArtifactDeletionTombstone.artifact_type == artifact_type,
                    ArtifactDeletionTombstone.artifact_id == artifact_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            # The deterministic event key collided with a different artifact.
            raise RuntimeError("stale_sweep_synthetic_event_identity_conflict")
        return DeletionTombstoneReceipt(
            generation=int(existing.generation),
            delete_event_id=str(existing.delete_event_id),
        )

    async def stage_stale_sweep_batch(
        self,
        context: Any,
        request: StaleSweepBatchRequest,
    ) -> StaleSweepRunReceipt:
        """Stage identities/intents before the exact checkpoint CAS."""

        await self._lock_stale_sweep_claim(
            context,
            entry_id=request.entry_id,
            claim_token=request.claim_token,
            board_id=request.board_id,
            expected_cursor=request.cursor,
            expected_budget=request.budget,
            expected_attempt=request.attempt,
        )

        ensured = 0
        for candidate in request.candidates:
            model = _MODELS[candidate.artifact_type]
            source_exists = await context.scalar(
                select(
                    exists(
                        select(1).where(
                            model.id == candidate.artifact_id,
                            model.board_id == request.board_id,
                        )
                    )
                )
            )
            # Close snapshot->checkpoint TOCTOU: a recreated/live source must
            # never receive a synthetic deletion fence.
            if bool(source_exists):
                continue
            synthetic_event_id = candidate.synthetic_delete_event_id(
                board_id=request.board_id,
                # Catch-up identity is independent from queue retry accounting.
                # ``attempt`` remains in the durable coordinator payload for
                # observability/forward compatibility, never as an ID input.
                epoch=STALE_SWEEP_CATCHUP_EPOCH,
            )
            tombstone = await self._ensure_catchup_tombstone(
                context,
                board_id=request.board_id,
                artifact_type=candidate.artifact_type,
                artifact_id=candidate.artifact_id,
                synthetic_event_id=synthetic_event_id,
            )
            intent = await self.persist_reconcile_intent(
                context,
                ReconcileIntentCreate(
                    board_id=request.board_id,
                    artifact_type=candidate.artifact_type,
                    artifact_id=candidate.artifact_id,
                    generation=tombstone.generation,
                    delete_event_id=tombstone.delete_event_id,
                    source_refs=(candidate.source_ref,),
                    occurred_at=request.now,
                ),
            )
            if intent.created:
                ensured += 1

        if request.has_more:
            checkpoint = await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == request.entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == request.claim_token,
                )
                .values(
                    status="pending",
                    payload=_stale_sweep_payload(
                        cursor=request.next_cursor,
                        budget=request.budget,
                        attempt=request.attempt,
                    ),
                    attempts=0,
                    last_error=None,
                    next_retry_at=None,
                    claimed_at=None,
                    claim_timeout_at=None,
                    worker_id=None,
                    claimed_by_session_id=None,
                    claim_token=None,
                    triggered_at=request.now,
                )
                .execution_options(synchronize_session=False)
            )
            if int(checkpoint.rowcount or 0) != 1:
                raise StaleSweepClaimConflict(
                    f"stale_sweep_checkpoint_cas_lost entry_id={request.entry_id}"
                )
            action = StaleSweepRunAction.ADVANCED
        else:
            completed = await context.execute(
                delete(ConsolidationQueue).where(
                    ConsolidationQueue.id == request.entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == request.claim_token,
                )
            )
            if int(completed.rowcount or 0) != 1:
                raise StaleSweepClaimConflict(
                    f"stale_sweep_complete_cas_lost entry_id={request.entry_id}"
                )
            action = StaleSweepRunAction.COMPLETED
        await context.flush()
        return StaleSweepRunReceipt(
            entry_id=request.entry_id,
            board_id=request.board_id,
            action=action,
            cursor=request.next_cursor,
            budget=request.budget,
            attempt=request.attempt,
            enqueued=ensured,
            has_more=request.has_more,
        )

    async def reschedule_stale_sweep(
        self,
        context: Any,
        request: StaleSweepRescheduleRequest,
    ) -> StaleSweepRunReceipt:
        """Preserve cursor/epoch and defer degraded work without legacy DLQ."""

        await self._lock_stale_sweep_claim(
            context,
            entry_id=request.entry_id,
            claim_token=request.claim_token,
            board_id=request.board_id,
            expected_cursor=request.cursor,
            expected_budget=request.budget,
            expected_attempt=request.attempt,
        )
        result = await context.execute(
            update(ConsolidationQueue)
            .where(
                ConsolidationQueue.id == request.entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == request.claim_token,
            )
            .values(
                status="pending",
                payload=_stale_sweep_payload(
                    cursor=request.cursor,
                    budget=request.budget,
                    attempt=request.attempt,
                ),
                attempts=ConsolidationQueue.attempts + 1,
                last_error=request.reason,
                next_retry_at=request.retry_at,
                claimed_at=None,
                claim_timeout_at=None,
                worker_id=None,
                claimed_by_session_id=None,
                claim_token=None,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            raise StaleSweepClaimConflict(
                f"stale_sweep_reschedule_cas_lost entry_id={request.entry_id}"
            )
        await context.flush()
        return StaleSweepRunReceipt(
            entry_id=request.entry_id,
            board_id=request.board_id,
            action=StaleSweepRunAction.RESCHEDULED,
            cursor=request.cursor,
            budget=request.budget,
            attempt=request.attempt,
            enqueued=0,
            has_more=True,
            reason=request.reason,
        )

    async def board_exists(self, context: Any, *, board_id: str) -> bool:
        return await context.get(Board, board_id) is not None

    async def list_dlq_auto_drain_board_ids(self, context: Any) -> tuple[str, ...]:
        rows = (await context.execute(select(Board))).scalars().all()
        return tuple(
            str(row.id)
            for row in rows
            if isinstance(row.settings, dict)
            and row.settings.get("dlq_auto_drain_enabled")
        )

    async def count_dead_letters(self, context: Any, *, board_id: str) -> int:
        value = await context.scalar(
            select(func.count()).where(ConsolidationDeadLetter.board_id == board_id)
        )
        return int(value or 0)

    async def delete_poison_dead_letters(
        self, context: Any, *, board_id: str, max_attempts: int
    ) -> tuple[ConsolidationPoisonRow, ...]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationDeadLetter).where(
                        ConsolidationDeadLetter.board_id == board_id,
                        ConsolidationDeadLetter.attempts >= max_attempts,
                    )
                )
            )
            .scalars()
            .all()
        )
        result = tuple(
            ConsolidationPoisonRow(id=str(row.id), attempts=int(row.attempts))
            for row in rows
        )
        for row in rows:
            await context.delete(row)
        if rows:
            await context.flush()
        return result

    async def commit(self, context: Any) -> None:
        await context.commit()

    async def rollback(self, context: Any) -> None:
        await context.rollback()


__all__ = ["CommunitySqlAlchemyConsolidationPersistence"]
