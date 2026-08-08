"""Transaction-bound lifecycle and governed purge adapter for SK-A quality."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    BoardErasurePermit,
    ChecklistBindingHeadRow,
    ChecklistBindingRow,
    ChecklistExecutionHeadRow,
    ChecklistExecutionRow,
    ChecklistItemResultRow,
    ChecklistReceiptRow,
    DomainEventRow,
    IdeationHistory,
    IdeationQAItem,
    QualityAssessmentHeadRow,
    QualityAssessmentLegacyImportCandidateRow,
    QualityAssessmentLegacyImportCheckpointRow,
    QualityAssessmentLegacyImportCompletionRow,
    QualityAssessmentLegacyImportResolutionRow,
    QualityAssessmentLegacyImportRunRow,
    QualityAssessmentLifecycleStaleTransitionRow,
    QualityAssessmentLifecycleTransitionRow,
    QualityAssessmentOutboxRow,
    QualityAssessmentReceiptRow,
    QualityAssessmentSubjectErasurePermitRow,
    QualityFindingQaLinkRow,
    QualityFindingRow,
    QualityProposedQuestionRow,
    RefinementHistory,
    RefinementQAItem,
    ResearchDecisionDerivationRow,
    ResearchDecisionEntryRow,
    ResearchDecisionHeadRow,
    ResearchDecisionHistoryRow,
    ResearchDecisionIdempotencyRow,
    ResearchDecisionOutboxRow,
    ResearchDecisionSnapshotRow,
    SpecHistory,
    SpecQAItem,
)
from okto_pulse.core.domain.quality_assessment import (
    AssessmentKind,
    AssessmentSubjectRef,
    AssessmentSubjectType,
)
from okto_pulse.core.domain.quality_assessment_lifecycle import (
    AssessmentHeadStrategy,
    AssessmentLifecycleHead,
    AssessmentLifecyclePlan,
    AssessmentLifecycleReceipt,
    AssessmentPurgePlan,
    AssessmentPurgePostcondition,
    AssessmentPurgeResidual,
    AssessmentPurgeResource,
    AssessmentPurgeScope,
)
from okto_pulse.core.domain.research_decision_ledger import (
    ResearchDecisionEventType,
)
from okto_pulse.core.ports.quality_assessment_lifecycle import (
    AssessmentLifecycleCasConflict,
    AssessmentPurgePostconditionConflict,
)


_QUALITY_HISTORY_ACTIONS = (
    "quality_assessment_legacy_imported",
    "quality_assessment_lifecycle",
)
_RESEARCH_DECISION_EVENT_TYPES = tuple(
    event_type.value for event_type in ResearchDecisionEventType
)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _history_binding(subject_type: AssessmentSubjectType):
    if subject_type is AssessmentSubjectType.IDEATION:
        return IdeationHistory, "ideation_id", IdeationQAItem, "ideation_id"
    if subject_type is AssessmentSubjectType.REFINEMENT:
        return (
            RefinementHistory,
            "refinement_id",
            RefinementQAItem,
            "refinement_id",
        )
    return SpecHistory, "spec_id", SpecQAItem, "spec_id"


class CommunitySqlAlchemyQualityAssessmentLifecycle:
    """Apply lifecycle changes and purge slices in the caller-owned UoW."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_lifecycle_state(
        self,
        *,
        board_id: str,
        subject_type: str,
        subject_id: str,
    ) -> tuple[
        tuple[AssessmentLifecycleHead, ...],
        tuple[AssessmentLifecycleReceipt, ...],
    ]:
        resolved_type = AssessmentSubjectType(subject_type)
        head_rows = list(
            (
                await self._session.execute(
                    select(QualityAssessmentHeadRow)
                    .where(
                        QualityAssessmentHeadRow.board_id == board_id,
                        QualityAssessmentHeadRow.subject_type
                        == resolved_type.value,
                        QualityAssessmentHeadRow.subject_id == subject_id,
                    )
                    .order_by(QualityAssessmentHeadRow.assessment_kind)
                )
            )
            .scalars()
            .all()
        )
        receipt_rows = list(
            (
                await self._session.execute(
                    select(QualityAssessmentReceiptRow)
                    .where(
                        QualityAssessmentReceiptRow.board_id == board_id,
                        QualityAssessmentReceiptRow.subject_type
                        == resolved_type.value,
                        QualityAssessmentReceiptRow.subject_id == subject_id,
                    )
                    .order_by(
                        QualityAssessmentReceiptRow.created_at,
                        QualityAssessmentReceiptRow.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return (
            tuple(
                AssessmentLifecycleHead(
                    assessment_kind=AssessmentKind(row.assessment_kind),
                    receipt_id=row.receipt_id,
                    revision=row.revision,
                )
                for row in head_rows
            ),
            tuple(
                AssessmentLifecycleReceipt(
                    receipt_id=row.id,
                    subject=AssessmentSubjectRef(
                        board_id=row.board_id,
                        subject_type=AssessmentSubjectType(row.subject_type),
                        subject_id=row.subject_id,
                        subject_version=row.subject_version,
                    ),
                    assessment_kind=AssessmentKind(row.assessment_kind),
                    input_digest=row.input_digest,
                    created_at=_aware(row.created_at),
                )
                for row in receipt_rows
            ),
        )

    async def apply_lifecycle_plan(
        self,
        plan: AssessmentLifecyclePlan,
    ) -> None:
        transition = plan.transition
        subject = transition.after.subject
        existing = await self._session.get(
            QualityAssessmentLifecycleTransitionRow,
            transition.transition_digest,
        )
        if existing is not None:
            if (
                existing.board_id != subject.board_id
                or existing.idempotency_key != transition.idempotency_key
            ):
                raise AssessmentLifecycleCasConflict(
                    "assessment_lifecycle_transition_digest_conflict"
                )
            return
        idempotent_row = (
            (
                await self._session.execute(
                    select(QualityAssessmentLifecycleTransitionRow).where(
                        QualityAssessmentLifecycleTransitionRow.board_id
                        == subject.board_id,
                        QualityAssessmentLifecycleTransitionRow.idempotency_key
                        == transition.idempotency_key,
                    )
                )
            )
            .scalars()
            .first()
        )
        if idempotent_row is not None:
            raise AssessmentLifecycleCasConflict(
                "assessment_lifecycle_idempotency_conflict"
            )

        if plan.head_strategy is AssessmentHeadStrategy.RECOMPUTE:
            for rebuild in plan.head_rebuilds:
                identity = (
                    subject.board_id,
                    subject.subject_type.value,
                    subject.subject_id,
                    rebuild.assessment_kind.value,
                )
                head = await self._session.get(
                    QualityAssessmentHeadRow,
                    identity,
                )
                actual_revision = head.revision if head is not None else 0
                actual_receipt = head.receipt_id if head is not None else None
                if (
                    actual_revision != rebuild.expected_revision
                    or actual_receipt != rebuild.previous_receipt_id
                ):
                    raise AssessmentLifecycleCasConflict(
                        "assessment_lifecycle_head_cas_conflict"
                    )
                if rebuild.selected_receipt_id is None:
                    if head is not None:
                        await self._session.delete(head)
                elif head is None:
                    self._session.add(
                        QualityAssessmentHeadRow(
                            board_id=subject.board_id,
                            subject_type=subject.subject_type.value,
                            subject_id=subject.subject_id,
                            assessment_kind=rebuild.assessment_kind.value,
                            receipt_id=rebuild.selected_receipt_id,
                            revision=rebuild.resulting_revision,
                            updated_at=transition.occurred_at,
                        )
                    )
                elif rebuild.selected_receipt_id != head.receipt_id:
                    head.receipt_id = rebuild.selected_receipt_id
                    head.revision = rebuild.resulting_revision
                    head.updated_at = transition.occurred_at
                if rebuild.stale_transition_required:
                    self._session.add(
                        QualityAssessmentLifecycleStaleTransitionRow(
                            stale_transition_key=rebuild.stale_transition_key,
                            transition_digest=transition.transition_digest,
                            board_id=subject.board_id,
                            subject_type=subject.subject_type.value,
                            subject_id=subject.subject_id,
                            assessment_kind=rebuild.assessment_kind.value,
                            receipt_id=str(rebuild.previous_receipt_id),
                            created_at=transition.occurred_at,
                        )
                    )

        event_id = f"qal_evt_{transition.transition_digest[:24]}"
        history_id = f"qal_hst_{transition.transition_digest[:24]}"
        payload = {
            "schema_version": 1,
            "transition_digest": transition.transition_digest,
            "action": transition.action.value,
            "board_id": subject.board_id,
            "subject_type": subject.subject_type.value,
            "subject_id": subject.subject_id,
            "before_version": transition.before.subject.subject_version,
            "after_version": transition.after.subject.subject_version,
            "head_strategy": plan.head_strategy.value,
            "projection_action": plan.projection_action.value,
            "kg_action": plan.kg_action.value,
            "head_rebuilds": [
                {
                    "assessment_kind": item.assessment_kind.value,
                    "expected_revision": item.expected_revision,
                    "previous_receipt_id": item.previous_receipt_id,
                    "selected_receipt_id": item.selected_receipt_id,
                    "selected_state": (
                        item.selected_state.value
                        if item.selected_state is not None
                        else None
                    ),
                    "resulting_revision": item.resulting_revision,
                    "stale_transition_key": item.stale_transition_key,
                }
                for item in plan.head_rebuilds
            ],
        }
        self._session.add(
            DomainEventRow(
                id=event_id,
                event_type="quality.assessment_lifecycle.v1",
                board_id=subject.board_id,
                actor_id=transition.actor_id,
                actor_type="user",
                payload_json=payload,
                occurred_at=transition.occurred_at,
            )
        )
        self._session.add(
            ActivityLog(
                id=history_id,
                board_id=subject.board_id,
                card_id=None,
                action="quality_assessment_lifecycle",
                actor_type="user",
                actor_id=transition.actor_id,
                actor_name=transition.actor_id,
                details=payload,
                created_at=transition.occurred_at,
            )
        )
        history_model, subject_fk, _qa_model, _qa_fk = _history_binding(
            subject.subject_type
        )
        self._session.add(
            history_model(
                id=history_id,
                **{subject_fk: subject.subject_id},
                action="quality_assessment_lifecycle",
                actor_type="user",
                actor_id=transition.actor_id,
                actor_name=transition.actor_id,
                changes=[
                    {
                        "field": "quality_assessment_projection",
                        "old": transition.before.status,
                        "new": transition.after.status,
                    }
                ],
                summary=(
                    f"Quality assessment lifecycle: "
                    f"{transition.action.value}"
                ),
                version=transition.after.subject.subject_version,
                created_at=transition.occurred_at,
            )
        )
        self._session.add(
            QualityAssessmentLifecycleTransitionRow(
                transition_digest=transition.transition_digest,
                board_id=subject.board_id,
                idempotency_key=transition.idempotency_key,
                action=transition.action.value,
                subject_type=subject.subject_type.value,
                subject_id=subject.subject_id,
                before_version=transition.before.subject.subject_version,
                before_status=transition.before.status,
                before_archived=transition.before.archived,
                after_version=transition.after.subject.subject_version,
                after_status=transition.after.status,
                after_archived=transition.after.archived,
                head_rebuilds_json=payload["head_rebuilds"],
                actor_id=transition.actor_id,
                event_id=event_id,
                history_id=history_id,
                # DomainEventRow is the durable outbox row for lifecycle.
                outbox_id=event_id,
                occurred_at=transition.occurred_at,
                applied_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()

    def _scope_filter(self, model, plan: AssessmentPurgePlan):
        target = plan.target
        filters = [model.board_id == target.board_id]
        if target.scope is AssessmentPurgeScope.SUBJECT:
            if hasattr(model, "subject_type"):
                filters.append(model.subject_type == target.subject_type.value)
            if hasattr(model, "subject_id"):
                filters.append(model.subject_id == target.subject_id)
        return tuple(filters)

    async def _prepare_context(self, plan: AssessmentPurgePlan) -> dict[str, object]:
        target = plan.target
        receipt_filters = [
            QualityAssessmentReceiptRow.board_id == target.board_id
        ]
        if target.scope is AssessmentPurgeScope.SUBJECT:
            receipt_filters.extend(
                (
                    QualityAssessmentReceiptRow.subject_type
                    == target.subject_type.value,
                    QualityAssessmentReceiptRow.subject_id
                    == target.subject_id,
                )
            )
        receipt_rows = list(
            (
                await self._session.execute(
                    select(QualityAssessmentReceiptRow)
                    .where(*receipt_filters)
                    .order_by(
                        QualityAssessmentReceiptRow.created_at.desc(),
                        QualityAssessmentReceiptRow.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        receipt_ids = tuple(row.id for row in receipt_rows)
        quality_event_ids = tuple(row.event_id for row in receipt_rows)
        quality_history_ids = {row.history_id for row in receipt_rows}
        lifecycle_filters = [
            QualityAssessmentLifecycleTransitionRow.board_id == target.board_id
        ]
        if target.scope is AssessmentPurgeScope.SUBJECT:
            lifecycle_filters.extend(
                (
                    QualityAssessmentLifecycleTransitionRow.subject_type
                    == target.subject_type.value,
                    QualityAssessmentLifecycleTransitionRow.subject_id
                    == target.subject_id,
                )
            )
        lifecycle_rows = list(
            (
                await self._session.execute(
                    select(QualityAssessmentLifecycleTransitionRow).where(
                        *lifecycle_filters
                    )
                )
            )
            .scalars()
            .all()
        )
        quality_event_ids += tuple(row.event_id for row in lifecycle_rows)
        quality_history_ids.update(row.history_id for row in lifecycle_rows)
        qa_ids: tuple[str, ...] = ()
        finding_ids: tuple[str, ...] = ()
        if receipt_ids:
            qa_ids = tuple(
                str(value)
                for value in (
                    await self._session.execute(
                        select(QualityProposedQuestionRow.qa_id).where(
                            QualityProposedQuestionRow.receipt_id.in_(
                                receipt_ids
                            )
                        )
                    )
                ).scalars()
            )
            finding_ids = tuple(
                str(value)
                for value in (
                    await self._session.execute(
                        select(QualityFindingRow.id).where(
                            QualityFindingRow.receipt_id.in_(receipt_ids)
                        )
                    )
                ).scalars()
            )

        checklist_receipt_filters = [
            ChecklistReceiptRow.board_id == target.board_id
        ]
        checklist_execution_filters = [
            ChecklistExecutionRow.board_id == target.board_id
        ]
        if target.scope is AssessmentPurgeScope.SUBJECT:
            if target.subject_type is AssessmentSubjectType.SPEC:
                checklist_receipt_filters.append(
                    ChecklistReceiptRow.spec_id == target.subject_id
                )
                checklist_execution_filters.append(
                    ChecklistExecutionRow.spec_id == target.subject_id
                )
            else:
                checklist_receipt_filters.append(False)
                checklist_execution_filters.append(False)
        checklist_receipt_ids = tuple(
            str(value)
            for value in (
                await self._session.execute(
                    select(ChecklistReceiptRow.id).where(
                        *checklist_receipt_filters
                    )
                )
            ).scalars()
        )
        checklist_execution_ids = tuple(
            str(value)
            for value in (
                await self._session.execute(
                    select(ChecklistExecutionRow.id).where(
                        *checklist_execution_filters
                    )
                )
            ).scalars()
        )

        rdl_filters = [
            ResearchDecisionEntryRow.board_id == target.board_id
        ]
        if target.scope is AssessmentPurgeScope.SUBJECT:
            if target.subject_type is AssessmentSubjectType.REFINEMENT:
                rdl_filters.append(
                    ResearchDecisionEntryRow.refinement_id
                    == target.subject_id
                )
            else:
                rdl_filters.append(False)
        rdl_entries = list(
            (
                await self._session.execute(
                    select(ResearchDecisionEntryRow)
                    .where(*rdl_filters)
                    .order_by(
                        ResearchDecisionEntryRow.created_at.desc(),
                        ResearchDecisionEntryRow.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        rdl_idempotency_filters = [
            ResearchDecisionIdempotencyRow.board_id == target.board_id
        ]
        if target.scope is AssessmentPurgeScope.SUBJECT:
            if target.subject_type is AssessmentSubjectType.REFINEMENT:
                rdl_idempotency_filters.append(
                    ResearchDecisionIdempotencyRow.refinement_id
                    == target.subject_id
                )
            else:
                rdl_idempotency_filters.append(False)
        rdl_idempotency_rows = list(
            (
                await self._session.execute(
                    select(ResearchDecisionIdempotencyRow).where(
                        *rdl_idempotency_filters
                    )
                )
            )
            .scalars()
            .all()
        )
        idempotency_outbox_ids = tuple(
            row.outbox_id for row in rdl_idempotency_rows
        )
        rdl_outbox_filters = [
            ResearchDecisionOutboxRow.partition_key == target.board_id
        ]
        if target.scope is AssessmentPurgeScope.SUBJECT:
            if (
                target.subject_type is AssessmentSubjectType.REFINEMENT
                and idempotency_outbox_ids
            ):
                rdl_outbox_filters.append(
                    ResearchDecisionOutboxRow.id.in_(
                        idempotency_outbox_ids
                    )
                )
            else:
                rdl_outbox_filters.append(False)
        rdl_outbox_rows = list(
            (
                await self._session.execute(
                    select(ResearchDecisionOutboxRow).where(
                        *rdl_outbox_filters
                    )
                )
            )
            .scalars()
            .all()
        )
        rdl_event_ids = tuple(
            sorted(
                {
                    *(row.event_id for row in rdl_idempotency_rows),
                    *(row.event_id for row in rdl_outbox_rows),
                }
            )
        )
        return {
            "receipt_rows": receipt_rows,
            "receipt_ids": receipt_ids,
            "quality_event_ids": quality_event_ids,
            "quality_history_ids": tuple(sorted(quality_history_ids)),
            "qa_ids": qa_ids,
            "finding_ids": finding_ids,
            "checklist_receipt_ids": checklist_receipt_ids,
            "checklist_execution_ids": checklist_execution_ids,
            "rdl_entries": rdl_entries,
            "rdl_outbox_ids": tuple(row.id for row in rdl_outbox_rows),
            "rdl_event_ids": rdl_event_ids,
        }

    async def _delete_resource(
        self,
        resource: AssessmentPurgeResource,
        plan: AssessmentPurgePlan,
        context: dict[str, object],
    ) -> None:
        target = plan.target
        receipt_ids = context["receipt_ids"]
        finding_ids = context["finding_ids"]
        qa_ids = context["qa_ids"]
        checklist_receipt_ids = context["checklist_receipt_ids"]
        checklist_execution_ids = context["checklist_execution_ids"]
        rdl_outbox_ids = context["rdl_outbox_ids"]

        if resource in {
            AssessmentPurgeResource.QUALITY_PROJECTIONS,
            AssessmentPurgeResource.KG_PROJECTIONS,
        }:
            return
        if resource is AssessmentPurgeResource.QUALITY_OUTBOX:
            await self._session.execute(
                delete(QualityAssessmentOutboxRow).where(
                    QualityAssessmentOutboxRow.receipt_id.in_(receipt_ids)
                )
            )
            if context["quality_event_ids"]:
                await self._session.execute(
                    delete(DomainEventRow).where(
                        DomainEventRow.id.in_(context["quality_event_ids"])
                    )
                )
        elif resource is AssessmentPurgeResource.QUALITY_HEADS:
            await self._session.execute(
                delete(QualityAssessmentHeadRow).where(
                    *self._scope_filter(QualityAssessmentHeadRow, plan)
                )
            )
        elif resource is AssessmentPurgeResource.QUALITY_FINDING_QA_LINKS:
            await self._session.execute(
                delete(QualityFindingQaLinkRow).where(
                    QualityFindingQaLinkRow.finding_id.in_(finding_ids)
                )
            )
        elif resource is AssessmentPurgeResource.QUALITY_FINDINGS:
            await self._session.execute(
                delete(QualityFindingRow).where(
                    QualityFindingRow.receipt_id.in_(receipt_ids)
                )
            )
        elif resource is AssessmentPurgeResource.QUALITY_PROPOSED_QUESTIONS:
            await self._session.execute(
                delete(QualityProposedQuestionRow).where(
                    QualityProposedQuestionRow.receipt_id.in_(receipt_ids)
                )
            )
        elif resource is AssessmentPurgeResource.OPERATIONAL_QA_ITEMS:
            if qa_ids:
                if target.scope is AssessmentPurgeScope.SUBJECT:
                    _history, _history_fk, qa_model, qa_fk = _history_binding(
                        target.subject_type
                    )
                    await self._session.execute(
                        delete(qa_model).where(
                            qa_model.id.in_(qa_ids),
                            getattr(qa_model, qa_fk) == target.subject_id,
                        )
                    )
                else:
                    for qa_model in (
                        IdeationQAItem,
                        RefinementQAItem,
                        SpecQAItem,
                    ):
                        await self._session.execute(
                            delete(qa_model).where(qa_model.id.in_(qa_ids))
                        )
        elif resource is AssessmentPurgeResource.CHECKLIST_ITEM_RESULTS:
            await self._session.execute(
                delete(ChecklistItemResultRow).where(
                    ChecklistItemResultRow.receipt_id.in_(
                        checklist_receipt_ids
                    )
                )
            )
        elif resource is AssessmentPurgeResource.CHECKLIST_EXECUTION_HEADS:
            filters = [
                ChecklistExecutionHeadRow.board_id == target.board_id
            ]
            if target.scope is AssessmentPurgeScope.SUBJECT:
                if target.subject_type is AssessmentSubjectType.SPEC:
                    filters.append(
                        ChecklistExecutionHeadRow.spec_id
                        == target.subject_id
                    )
                else:
                    filters.append(False)
            await self._session.execute(
                delete(ChecklistExecutionHeadRow).where(*filters)
            )
        elif resource is AssessmentPurgeResource.CHECKLIST_RECEIPTS:
            await self._session.execute(
                delete(ChecklistReceiptRow).where(
                    ChecklistReceiptRow.id.in_(checklist_receipt_ids)
                )
            )
        elif resource is AssessmentPurgeResource.CHECKLIST_EXECUTIONS:
            await self._session.execute(
                delete(ChecklistExecutionRow).where(
                    ChecklistExecutionRow.id.in_(checklist_execution_ids)
                )
            )
        elif resource is AssessmentPurgeResource.CHECKLIST_BINDING_HEADS:
            if target.scope is AssessmentPurgeScope.BOARD:
                await self._session.execute(
                    delete(ChecklistBindingHeadRow).where(
                        ChecklistBindingHeadRow.board_id == target.board_id
                    )
                )
        elif resource is AssessmentPurgeResource.CHECKLIST_BINDING_VERSIONS:
            if target.scope is AssessmentPurgeScope.BOARD:
                await self._session.execute(
                    delete(ChecklistBindingRow).where(
                        ChecklistBindingRow.board_id == target.board_id
                    )
                )
        elif resource is AssessmentPurgeResource.RESEARCH_IDEMPOTENCY:
            filters = [
                ResearchDecisionIdempotencyRow.board_id == target.board_id
            ]
            if target.scope is AssessmentPurgeScope.SUBJECT:
                if target.subject_type is AssessmentSubjectType.REFINEMENT:
                    filters.append(
                        ResearchDecisionIdempotencyRow.refinement_id
                        == target.subject_id
                    )
                else:
                    filters.append(False)
            await self._session.execute(
                delete(ResearchDecisionIdempotencyRow).where(*filters)
            )
        elif resource is AssessmentPurgeResource.RESEARCH_DERIVATIONS:
            filters = [
                ResearchDecisionDerivationRow.board_id == target.board_id
            ]
            if target.scope is AssessmentPurgeScope.SUBJECT:
                if target.subject_type is AssessmentSubjectType.SPEC:
                    filters.append(
                        ResearchDecisionDerivationRow.spec_id
                        == target.subject_id
                    )
                elif target.subject_type is AssessmentSubjectType.REFINEMENT:
                    filters.append(
                        ResearchDecisionDerivationRow.source_refinement_id
                        == target.subject_id
                    )
                else:
                    filters.append(False)
            await self._session.execute(
                delete(ResearchDecisionDerivationRow).where(*filters)
            )
        elif resource is AssessmentPurgeResource.RESEARCH_SNAPSHOTS:
            filters = [
                ResearchDecisionSnapshotRow.board_id == target.board_id
            ]
            if target.scope is AssessmentPurgeScope.SUBJECT:
                if target.subject_type is AssessmentSubjectType.REFINEMENT:
                    filters.append(
                        ResearchDecisionSnapshotRow.refinement_id
                        == target.subject_id
                    )
                else:
                    filters.append(False)
            await self._session.execute(
                delete(ResearchDecisionSnapshotRow).where(*filters)
            )
        elif resource is AssessmentPurgeResource.RESEARCH_OUTBOX:
            if target.scope is AssessmentPurgeScope.BOARD:
                await self._session.execute(
                    delete(ResearchDecisionOutboxRow).where(
                        ResearchDecisionOutboxRow.partition_key
                        == target.board_id
                    )
                )
            elif rdl_outbox_ids:
                await self._session.execute(
                    delete(ResearchDecisionOutboxRow).where(
                        ResearchDecisionOutboxRow.id.in_(rdl_outbox_ids)
                    )
                )
        elif resource is AssessmentPurgeResource.RESEARCH_EVENTS:
            if target.scope is AssessmentPurgeScope.BOARD:
                await self._session.execute(
                    delete(DomainEventRow).where(
                        DomainEventRow.board_id == target.board_id,
                        DomainEventRow.event_type.in_(
                            _RESEARCH_DECISION_EVENT_TYPES
                        ),
                    )
                )
            elif context["rdl_event_ids"]:
                await self._session.execute(
                    delete(DomainEventRow).where(
                        DomainEventRow.id.in_(context["rdl_event_ids"])
                    )
                )
        elif resource is AssessmentPurgeResource.RESEARCH_HISTORY:
            filters = [
                ResearchDecisionHistoryRow.board_id == target.board_id
            ]
            if target.scope is AssessmentPurgeScope.SUBJECT:
                if target.subject_type is AssessmentSubjectType.REFINEMENT:
                    filters.append(
                        ResearchDecisionHistoryRow.refinement_id
                        == target.subject_id
                    )
                else:
                    filters.append(False)
            await self._session.execute(
                delete(ResearchDecisionHistoryRow).where(*filters)
            )
        elif resource is AssessmentPurgeResource.RESEARCH_HEADS:
            filters = [ResearchDecisionHeadRow.board_id == target.board_id]
            if target.scope is AssessmentPurgeScope.SUBJECT:
                if target.subject_type is AssessmentSubjectType.REFINEMENT:
                    filters.append(
                        ResearchDecisionHeadRow.refinement_id
                        == target.subject_id
                    )
                else:
                    filters.append(False)
            await self._session.execute(
                delete(ResearchDecisionHeadRow).where(*filters)
            )
        elif resource is AssessmentPurgeResource.RESEARCH_ENTRIES:
            for row in context["rdl_entries"]:
                await self._session.delete(row)
                await self._session.flush()
        elif resource is AssessmentPurgeResource.QUALITY_RECEIPTS:
            for row in context["receipt_rows"]:
                await self._session.delete(row)
                await self._session.flush()
        elif (
            resource
            is AssessmentPurgeResource.QUALITY_LIFECYCLE_STALE_TRANSITIONS
        ):
            await self._session.execute(
                delete(QualityAssessmentLifecycleStaleTransitionRow).where(
                    *self._scope_filter(
                        QualityAssessmentLifecycleStaleTransitionRow,
                        plan,
                    )
                )
            )
        elif (
            resource
            is AssessmentPurgeResource.QUALITY_LIFECYCLE_TRANSITIONS
        ):
            await self._session.execute(
                delete(QualityAssessmentLifecycleTransitionRow).where(
                    *self._scope_filter(
                        QualityAssessmentLifecycleTransitionRow,
                        plan,
                    )
                )
            )
        elif resource is AssessmentPurgeResource.SUBJECT_HISTORY:
            if target.scope is AssessmentPurgeScope.SUBJECT:
                history_model, history_fk, _qa_model, _qa_fk = (
                    _history_binding(target.subject_type)
                )
                await self._session.execute(
                    delete(history_model).where(
                        getattr(history_model, history_fk) == target.subject_id,
                        history_model.action.in_(_QUALITY_HISTORY_ACTIONS),
                    )
                )
                if context["quality_history_ids"]:
                    await self._session.execute(
                        delete(ActivityLog).where(
                            ActivityLog.id.in_(
                                context["quality_history_ids"]
                            )
                        )
                    )
            else:
                await self._session.execute(
                    delete(ActivityLog).where(
                        ActivityLog.board_id == target.board_id,
                        ActivityLog.action.in_(_QUALITY_HISTORY_ACTIONS),
                    )
                )
        elif resource is AssessmentPurgeResource.LEGACY_IMPORT_RESOLUTIONS:
            await self._session.execute(
                delete(QualityAssessmentLegacyImportResolutionRow).where(
                    QualityAssessmentLegacyImportResolutionRow.board_id
                    == target.board_id
                )
            )
        elif resource is AssessmentPurgeResource.LEGACY_IMPORT_CHECKPOINTS:
            await self._session.execute(
                delete(QualityAssessmentLegacyImportCheckpointRow).where(
                    QualityAssessmentLegacyImportCheckpointRow.board_id
                    == target.board_id
                )
            )
        elif resource is AssessmentPurgeResource.LEGACY_IMPORT_COMPLETIONS:
            await self._session.execute(
                delete(QualityAssessmentLegacyImportCompletionRow).where(
                    QualityAssessmentLegacyImportCompletionRow.board_id
                    == target.board_id
                )
            )
        elif resource is AssessmentPurgeResource.LEGACY_IMPORT_CANDIDATES:
            await self._session.execute(
                delete(QualityAssessmentLegacyImportCandidateRow).where(
                    QualityAssessmentLegacyImportCandidateRow.board_id
                    == target.board_id
                )
            )
        elif resource is AssessmentPurgeResource.LEGACY_IMPORT_RUNS:
            await self._session.execute(
                delete(QualityAssessmentLegacyImportRunRow).where(
                    QualityAssessmentLegacyImportRunRow.board_id
                    == target.board_id
                )
            )

    async def _count_rows(self, model, *filters) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count()).select_from(model).where(*filters)
                )
            ).scalar_one()
        )

    async def _residual_count(
        self,
        resource: AssessmentPurgeResource,
        plan: AssessmentPurgePlan,
        context: dict[str, object],
    ) -> int:
        target = plan.target
        receipt_ids = context["receipt_ids"]
        qa_ids = context["qa_ids"]
        checklist_receipt_ids = context["checklist_receipt_ids"]
        checklist_execution_ids = context["checklist_execution_ids"]
        direct_models = {
            AssessmentPurgeResource.QUALITY_HEADS: QualityAssessmentHeadRow,
            AssessmentPurgeResource.QUALITY_FINDINGS: QualityFindingRow,
            AssessmentPurgeResource.QUALITY_LIFECYCLE_STALE_TRANSITIONS: (
                QualityAssessmentLifecycleStaleTransitionRow
            ),
            AssessmentPurgeResource.QUALITY_LIFECYCLE_TRANSITIONS: (
                QualityAssessmentLifecycleTransitionRow
            ),
        }
        if resource in {
            AssessmentPurgeResource.QUALITY_PROJECTIONS,
            AssessmentPurgeResource.KG_PROJECTIONS,
        }:
            return 0
        if resource in direct_models:
            return await self._count_rows(
                direct_models[resource],
                *self._scope_filter(direct_models[resource], plan),
            )
        if resource is AssessmentPurgeResource.QUALITY_OUTBOX:
            return await self._count_rows(
                QualityAssessmentOutboxRow,
                QualityAssessmentOutboxRow.receipt_id.in_(receipt_ids),
            )
        if resource is AssessmentPurgeResource.QUALITY_FINDING_QA_LINKS:
            return await self._count_rows(
                QualityFindingQaLinkRow,
                QualityFindingQaLinkRow.finding_id.in_(
                    context["finding_ids"]
                ),
            )
        if resource is AssessmentPurgeResource.QUALITY_PROPOSED_QUESTIONS:
            return await self._count_rows(
                QualityProposedQuestionRow,
                QualityProposedQuestionRow.receipt_id.in_(receipt_ids),
            )
        if resource is AssessmentPurgeResource.OPERATIONAL_QA_ITEMS:
            total = 0
            for qa_model in (
                IdeationQAItem,
                RefinementQAItem,
                SpecQAItem,
            ):
                total += await self._count_rows(
                    qa_model,
                    qa_model.id.in_(qa_ids),
                )
            return total
        if resource is AssessmentPurgeResource.CHECKLIST_ITEM_RESULTS:
            return await self._count_rows(
                ChecklistItemResultRow,
                ChecklistItemResultRow.receipt_id.in_(
                    checklist_receipt_ids
                ),
            )
        if resource is AssessmentPurgeResource.CHECKLIST_RECEIPTS:
            return await self._count_rows(
                ChecklistReceiptRow,
                ChecklistReceiptRow.id.in_(checklist_receipt_ids),
            )
        if resource is AssessmentPurgeResource.CHECKLIST_EXECUTIONS:
            return await self._count_rows(
                ChecklistExecutionRow,
                ChecklistExecutionRow.id.in_(checklist_execution_ids),
            )
        if resource is AssessmentPurgeResource.CHECKLIST_EXECUTION_HEADS:
            filters = [ChecklistExecutionHeadRow.board_id == target.board_id]
            if target.scope is AssessmentPurgeScope.SUBJECT:
                if target.subject_type is AssessmentSubjectType.SPEC:
                    filters.append(
                        ChecklistExecutionHeadRow.spec_id
                        == target.subject_id
                    )
                else:
                    filters.append(False)
            return await self._count_rows(
                ChecklistExecutionHeadRow,
                *filters,
            )
        if resource is AssessmentPurgeResource.CHECKLIST_BINDING_HEADS:
            return (
                await self._count_rows(
                    ChecklistBindingHeadRow,
                    ChecklistBindingHeadRow.board_id == target.board_id,
                )
                if target.scope is AssessmentPurgeScope.BOARD
                else 0
            )
        if resource is AssessmentPurgeResource.CHECKLIST_BINDING_VERSIONS:
            return (
                await self._count_rows(
                    ChecklistBindingRow,
                    ChecklistBindingRow.board_id == target.board_id,
                )
                if target.scope is AssessmentPurgeScope.BOARD
                else 0
            )
        rdl_models = {
            AssessmentPurgeResource.RESEARCH_IDEMPOTENCY: (
                ResearchDecisionIdempotencyRow
            ),
            AssessmentPurgeResource.RESEARCH_DERIVATIONS: (
                ResearchDecisionDerivationRow
            ),
            AssessmentPurgeResource.RESEARCH_SNAPSHOTS: (
                ResearchDecisionSnapshotRow
            ),
            AssessmentPurgeResource.RESEARCH_HISTORY: (
                ResearchDecisionHistoryRow
            ),
            AssessmentPurgeResource.RESEARCH_HEADS: ResearchDecisionHeadRow,
            AssessmentPurgeResource.RESEARCH_ENTRIES: (
                ResearchDecisionEntryRow
            ),
        }
        if resource in rdl_models:
            model = rdl_models[resource]
            filters = [model.board_id == target.board_id]
            if target.scope is AssessmentPurgeScope.SUBJECT:
                if target.subject_type is AssessmentSubjectType.REFINEMENT:
                    if resource is AssessmentPurgeResource.RESEARCH_DERIVATIONS:
                        filters.append(
                            ResearchDecisionDerivationRow.source_refinement_id
                            == target.subject_id
                        )
                    elif hasattr(model, "refinement_id"):
                        filters.append(model.refinement_id == target.subject_id)
                    else:
                        filters.append(False)
                elif (
                    resource is AssessmentPurgeResource.RESEARCH_DERIVATIONS
                    and target.subject_type is AssessmentSubjectType.SPEC
                ):
                    filters.append(model.spec_id == target.subject_id)
                else:
                    filters.append(False)
            return await self._count_rows(model, *filters)
        if resource is AssessmentPurgeResource.RESEARCH_OUTBOX:
            if target.scope is AssessmentPurgeScope.BOARD:
                return await self._count_rows(
                    ResearchDecisionOutboxRow,
                    ResearchDecisionOutboxRow.partition_key
                    == target.board_id,
                )
            if not context["rdl_outbox_ids"]:
                return 0
            return await self._count_rows(
                ResearchDecisionOutboxRow,
                ResearchDecisionOutboxRow.id.in_(
                    context["rdl_outbox_ids"]
                ),
            )
        if resource is AssessmentPurgeResource.RESEARCH_EVENTS:
            if target.scope is AssessmentPurgeScope.BOARD:
                return await self._count_rows(
                    DomainEventRow,
                    DomainEventRow.board_id == target.board_id,
                    DomainEventRow.event_type.in_(
                        _RESEARCH_DECISION_EVENT_TYPES
                    ),
                )
            if not context["rdl_event_ids"]:
                return 0
            return await self._count_rows(
                DomainEventRow,
                DomainEventRow.id.in_(context["rdl_event_ids"]),
            )
        if resource is AssessmentPurgeResource.QUALITY_RECEIPTS:
            return await self._count_rows(
                QualityAssessmentReceiptRow,
                QualityAssessmentReceiptRow.id.in_(receipt_ids),
            )
        if resource is AssessmentPurgeResource.SUBJECT_HISTORY:
            if target.scope is AssessmentPurgeScope.BOARD:
                return await self._count_rows(
                    ActivityLog,
                    ActivityLog.board_id == target.board_id,
                    ActivityLog.action.in_(_QUALITY_HISTORY_ACTIONS),
                )
            history_model, history_fk, _qa_model, _qa_fk = _history_binding(
                target.subject_type
            )
            history_count = await self._count_rows(
                history_model,
                getattr(history_model, history_fk) == target.subject_id,
                history_model.action.in_(_QUALITY_HISTORY_ACTIONS),
            )
            activity_count = (
                await self._count_rows(
                    ActivityLog,
                    ActivityLog.id.in_(context["quality_history_ids"]),
                )
                if context["quality_history_ids"]
                else 0
            )
            return history_count + activity_count
        epoch_models = {
            AssessmentPurgeResource.LEGACY_IMPORT_RESOLUTIONS: (
                QualityAssessmentLegacyImportResolutionRow
            ),
            AssessmentPurgeResource.LEGACY_IMPORT_CHECKPOINTS: (
                QualityAssessmentLegacyImportCheckpointRow
            ),
            AssessmentPurgeResource.LEGACY_IMPORT_COMPLETIONS: (
                QualityAssessmentLegacyImportCompletionRow
            ),
            AssessmentPurgeResource.LEGACY_IMPORT_CANDIDATES: (
                QualityAssessmentLegacyImportCandidateRow
            ),
            AssessmentPurgeResource.LEGACY_IMPORT_RUNS: (
                QualityAssessmentLegacyImportRunRow
            ),
        }
        if resource in epoch_models:
            return await self._count_rows(
                epoch_models[resource],
                epoch_models[resource].board_id == target.board_id,
            )
        raise AssessmentPurgePostconditionConflict(
            f"assessment_purge_resource_unmapped:{resource.value}"
        )

    async def apply_purge_plan(
        self,
        plan: AssessmentPurgePlan,
    ) -> AssessmentPurgePostcondition:
        target = plan.target
        subject_permit: QualityAssessmentSubjectErasurePermitRow | None = None
        if target.scope is AssessmentPurgeScope.BOARD:
            permit = await self._session.get(
                BoardErasurePermit,
                target.board_id,
            )
            if (
                permit is None
                or permit.permit_token != plan.board_erasure_permit_id
            ):
                raise AssessmentPurgePostconditionConflict(
                    "assessment_board_erasure_permit_missing"
                )
        else:
            subject_permit = QualityAssessmentSubjectErasurePermitRow(
                permit_token=uuid4().hex,
                board_id=target.board_id,
                subject_type=target.subject_type.value,
                subject_id=str(target.subject_id),
            )
            self._session.add(subject_permit)
            await self._session.flush()

        context = await self._prepare_context(plan)
        for resource in plan.deletion_order:
            await self._delete_resource(resource, plan, context)
        await self._session.flush()

        residual_rows: list[AssessmentPurgeResidual] = []
        for resource in plan.deletion_order:
            residual_rows.append(
                AssessmentPurgeResidual(
                    resource=resource,
                    count=await self._residual_count(
                        resource,
                        plan,
                        context,
                    ),
                )
            )
        residuals = tuple(residual_rows)
        if subject_permit is not None:
            await self._session.delete(subject_permit)
            await self._session.flush()
            permit_residual = await self._count_rows(
                QualityAssessmentSubjectErasurePermitRow,
                QualityAssessmentSubjectErasurePermitRow.permit_token
                == subject_permit.permit_token,
            )
            if permit_residual:
                raise AssessmentPurgePostconditionConflict(
                    "assessment_subject_erasure_permit_release_failed"
                )
        return AssessmentPurgePostcondition(
            target=target,
            residuals=residuals,
            zero_orphans=not any(item.count for item in residuals),
            projections_reconciled=True,
            outbox_reconciled=not any(
                item.count
                for item in residuals
                if item.resource
                in {
                    AssessmentPurgeResource.QUALITY_OUTBOX,
                    AssessmentPurgeResource.RESEARCH_OUTBOX,
                    AssessmentPurgeResource.RESEARCH_EVENTS,
                }
            ),
            epoch_consistency_preserved=(
                target.scope is AssessmentPurgeScope.SUBJECT
                or not any(
                    item.count
                    for item in residuals
                    if item.resource.value.startswith("legacy_import_")
                )
            ),
            verified_at=datetime.now(timezone.utc),
        )


__all__ = ["CommunitySqlAlchemyQualityAssessmentLifecycle"]
