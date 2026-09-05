"""Community SQLAlchemy KG governance store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    lock_policy_board,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Board,
    BoardErasureJob,
    BoardErasurePermit,
    Card,
    CodeEvidenceDispositionRow,
    CodeEvidenceRow,
    CodeEvidenceSpecLinkRow,
    CodeInvestigationHeadRow,
    CodeInvestigationReceiptRevocationRow,
    CodeInvestigationReceiptRow,
    CodeInvestigationRequestRow,
    CodeTraceabilityWaiverRow,
    ConsolidationAudit,
    ConsolidationQueue,
    DesignSystemGateAudit,
    ExactRebuildConsolidationAckJournal,
    ExactRebuildConsolidationCompensation,
    GlobalDiscoveryDeliveryRedriveControl,
    GlobalUpdateOutbox,
    Guideline,
    GuidelineBoardBindingRow,
    GuidelineHeadRow,
    GuidelineImpactAdoptionRow,
    GuidelineImpactItemRow,
    GuidelineImpactReceiptRow,
    GuidelineImpactUnlinkRow,
    GuidelineRetirementImpactRow,
    GuidelineRetirementRow,
    GuidelineRevisionRow,
    Ideation,
    ImplementationTargetEvidenceLinkRow,
    ImplementationTargetExecutionRecordRow,
    ImplementationTargetResolutionRow,
    ImplementationTargetRow,
    ImplementationTargetSpecLinkRow,
    KGCognitiveSource,
    KGCognitiveSourceRevision,
    KGCurationProposal,
    KGEquivalenceLedger,
    KnowledgeAssignmentRecord,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    KuzuNodeRef,
    PolicyComplianceAdoptedRevisionRow,
    PolicyComplianceFindingRow,
    PolicyComplianceReceiptRow,
    PolicyWaiverEventRow,
    PolicyWaiverRow,
    Refinement,
    SemanticGuidelineAssessmentReceiptRow,
    SemanticGuidelineBindingConfigurationRow,
    SemanticGuidelineFindingRow,
    SemanticGuidelineLegacyMigrationRow,
    SemanticGuidelineMetricResultRow,
    SemanticGuidelineRevisionRow,
    SemanticGuidelineSkipRow,
    SemanticGuidelineWaiverEventRow,
    SemanticGuidelineWaiverRow,
    SemanticSubjectVersionEventRow,
    SemanticSubjectVersionRow,
    Spec,
    SpecDependency,
    SpecDependencyOperation,
    Sprint,
    Story,
    TargetOverlapAcknowledgementRow,
)
from okto_pulse.core.domain.enums import SpecStatus, SprintStatus
from okto_pulse.core.ports.kg_events import HISTORICAL_PROGRESS_SETTINGS_KEY
from okto_pulse.core.ports.kg_governance import (
    BoardErasureJobFact,
    BoostAuditRecord,
    GovernanceUndoFact,
    HistoricalArtifactFact,
    HistoricalBoardRecord,
    HistoricalQueueFact,
    HistoricalQueueInsert,
)


class BoardRelationalErasureError(RuntimeError):
    """Relational right-to-erasure could not prove a zero-residual result."""


def _board_erasure_job_fact(row: BoardErasureJob) -> BoardErasureJobFact:
    next_attempt_at = row.next_attempt_at
    if next_attempt_at.tzinfo is None:
        next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
    return BoardErasureJobFact(
        board_id=str(row.board_id),
        actor_id=str(row.actor_id),
        attempts=int(row.attempts),
        last_error=row.last_error,
        next_attempt_at=next_attempt_at,
    )


async def _delete_self_referencing_history(
    context: Any,
    *,
    model: Any,
    identity: Any,
    scope_ids: tuple[str, ...],
) -> None:
    """Delete oldest-to-newest so self ``RESTRICT`` successor FKs remain valid."""

    if not scope_ids:
        return
    rows = (
        await context.execute(
            select(identity, model.superseded_by_id).where(
                model.scope_id.in_(scope_ids)
            )
        )
    ).all()
    pending = {
        str(row[0]): (str(row[1]) if row[1] is not None else None) for row in rows
    }
    while pending:
        referenced_successors = {
            successor
            for successor in pending.values()
            if successor is not None and successor in pending
        }
        removable = sorted(set(pending).difference(referenced_successors))
        if not removable:
            raise BoardRelationalErasureError(
                f"board_erasure_history_cycle:{model.__tablename__}"
            )
        for row_id in removable:
            result = await context.execute(delete(model).where(identity == row_id))
            if int(result.rowcount or 0) != 1:
                raise BoardRelationalErasureError(
                    "board_erasure_history_delete_mismatch:"
                    f"{model.__tablename__}:{row_id}"
                )
            pending.pop(row_id)


async def _count_where(context: Any, model: Any, predicate: Any) -> int:
    return int(
        (
            await context.execute(
                select(func.count()).select_from(model).where(predicate)
            )
        ).scalar_one()
    )


class CommunitySqlAlchemyKGGovernanceStore:
    async def get_board(
        self, context: Any, *, board_id: str
    ) -> HistoricalBoardRecord | None:
        row = await context.get(Board, board_id)
        return (
            HistoricalBoardRecord(id=str(row.id), settings=dict(row.settings or {}))
            if row is not None
            else None
        )

    async def save_board(self, context: Any, board: HistoricalBoardRecord) -> None:
        row = await context.get(Board, board.id)
        if row is not None:
            row.settings = dict(board.settings)
            flag_modified(row, "settings")
            await context.flush()

    async def queue_counts(self, context: Any, *, board_id: str) -> dict[str, int]:
        rows = (
            await context.execute(
                select(ConsolidationQueue.status, func.count())
                .where(
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.source == "historical_backfill",
                )
                .group_by(ConsolidationQueue.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}

    async def list_historical_artifacts(
        self, context: Any, *, board_id: str
    ) -> tuple[HistoricalArtifactFact, ...]:
        queries = (
            (
                "story",
                select(Story.id).where(
                    Story.board_id == board_id, Story.archived.is_(False)
                ),
            ),
            (
                "ideation",
                select(Ideation.id).where(
                    Ideation.board_id == board_id, Ideation.archived.is_(False)
                ),
            ),
            (
                "refinement",
                select(Refinement.id).where(
                    Refinement.board_id == board_id, Refinement.archived.is_(False)
                ),
            ),
            (
                "spec",
                select(Spec.id).where(
                    Spec.board_id == board_id,
                    Spec.status.in_(
                        (SpecStatus.DONE, SpecStatus.APPROVED, SpecStatus.VALIDATED)
                    ),
                    Spec.archived.is_(False),
                ),
            ),
            (
                "sprint",
                select(Sprint.id).where(
                    Sprint.board_id == board_id,
                    Sprint.status == SprintStatus.CLOSED,
                    Sprint.archived.is_(False),
                ),
            ),
            ("card", select(Card.id).where(Card.board_id == board_id)),
        )
        output: list[HistoricalArtifactFact] = []
        for artifact_type, statement in queries:
            ids = (await context.execute(statement)).scalars().all()
            output.extend(
                HistoricalArtifactFact(artifact_type, str(artifact_id))
                for artifact_id in ids
            )
        return tuple(output)

    async def list_live_queue(
        self, context: Any, *, board_id: str
    ) -> tuple[HistoricalQueueFact, ...]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue).where(
                        ConsolidationQueue.board_id == board_id,
                        ConsolidationQueue.status.in_(("pending", "claimed", "paused")),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            HistoricalQueueFact(
                str(row.id),
                str(row.artifact_type),
                str(row.artifact_id),
                str(row.source),
                str(row.status),
            )
            for row in rows
        )

    async def delete_terminal_queue(self, context: Any, *, board_id: str) -> None:
        await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.status.in_(("done", "failed")),
            )
        )

    async def add_queue_entries(
        self, context: Any, entries: Sequence[HistoricalQueueInsert]
    ) -> None:
        context.add_all(
            [
                ConsolidationQueue(
                    id=entry.id,
                    board_id=entry.board_id,
                    artifact_type=entry.artifact_type,
                    artifact_id=entry.artifact_id,
                    priority=entry.priority,
                    source=entry.source,
                    status=entry.status,
                )
                for entry in entries
            ]
        )

    async def update_historical_status(
        self, context: Any, *, board_id: str, old_status: str, new_status: str
    ) -> None:
        await context.execute(
            update(ConsolidationQueue)
            .where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == "historical_backfill",
                ConsolidationQueue.status == old_status,
            )
            .values(status=new_status)
        )

    async def delete_historical_pending(self, context: Any, *, board_id: str) -> int:
        """Remove the full live historical run, including an in-flight claim.

        A worker that already holds the deleted claim is fenced by its claim
        token on ACK and compensates any unacknowledged graph mutation.  This
        makes cancellation terminal instead of leaving a legacy ``claimed``
        row that blocks a later restart.
        """
        result = await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == "historical_backfill",
                ConsolidationQueue.status.in_(("pending", "claimed", "paused")),
            )
        )
        return int(result.rowcount or 0)

    async def purge_stale_metadata(self, context: Any, *, board_id: str) -> None:
        for model in (KuzuNodeRef, ConsolidationAudit, GlobalUpdateOutbox):
            await context.execute(delete(model).where(model.board_id == board_id))

    async def get_undo_fact(
        self, context: Any, *, board_id: str, session_id: str
    ) -> GovernanceUndoFact | None:
        audit = (
            (
                await context.execute(
                    select(ConsolidationAudit).where(
                        ConsolidationAudit.session_id == session_id,
                        ConsolidationAudit.board_id == board_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if audit is None:
            return None
        refs = (
            (
                await context.execute(
                    select(KuzuNodeRef).where(KuzuNodeRef.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
        node_ids = tuple(str(row.kuzu_node_id) for row in refs)
        blockers: tuple[str, ...] = ()
        if node_ids:
            rows = (
                (
                    await context.execute(
                        select(KuzuNodeRef.session_id).where(
                            KuzuNodeRef.kuzu_node_id.in_(node_ids),
                            KuzuNodeRef.session_id != session_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            blockers = tuple(sorted({str(value) for value in rows}))
        return GovernanceUndoFact(
            session_id, str(audit.undo_status), node_ids, blockers
        )

    async def mark_session_undone(
        self, context: Any, *, session_id: str, undone_at
    ) -> None:
        row = await context.get(ConsolidationAudit, session_id)
        if row is not None:
            row.undo_status = "undone"
            row.undone_at = undone_at

    async def purge_expired_audit(self, context: Any, *, board_id: str, cutoff) -> int:
        result = await context.execute(
            delete(ConsolidationAudit).where(
                ConsolidationAudit.board_id == board_id,
                ConsolidationAudit.committed_at < cutoff,
            )
        )
        return int(result.rowcount or 0)

    async def purge_board_metadata(self, context: Any, *, board_id: str) -> None:
        from okto_pulse.community.adapters.sqlalchemy_quality_assessment_lifecycle import (
            CommunitySqlAlchemyQualityAssessmentLifecycle,
        )
        from okto_pulse.core.domain.quality_assessment_lifecycle import (
            AssessmentBoardErasureCompletion,
        )
        from okto_pulse.core.services.quality_assessment_lifecycle import (
            QualityAssessmentLifecycleService,
        )

        try:
            await lock_policy_board(context, board_id=board_id)
        except (TypeError, ValueError) as exc:
            raise BoardRelationalErasureError(
                f"board_erasure_mutex_failed:{board_id}"
            ) from exc

        existing_permit = await context.get(BoardErasurePermit, board_id)
        if existing_permit is not None:
            raise BoardRelationalErasureError(
                f"board_erasure_permit_conflict:{board_id}"
            )

        permit_token = uuid4().hex
        context.add(
            BoardErasurePermit(
                board_id=board_id,
                permit_token=permit_token,
            )
        )
        await context.flush()
        quality_lifecycle = QualityAssessmentLifecycleService()
        quality_purge_plan = quality_lifecycle.prepare_board_purge(
            board_id=board_id,
            board_erasure_permit_id=permit_token,
        )
        quality_postcondition = None
        all_board_purges_completed = False
        permit_released = False

        cognitive_source_ids = tuple(
            str(value)
            for value in (
                await context.execute(
                    select(KGCognitiveSource.id).where(
                        KGCognitiveSource.board_id == board_id
                    )
                )
            ).scalars()
        )
        propagation_scope_ids = tuple(
            str(value)
            for value in (
                await context.execute(
                    select(KnowledgePropagationScopeRecord.id).where(
                        KnowledgePropagationScopeRecord.board_id == board_id
                    )
                )
            ).scalars()
        )
        inline_guideline_ids = tuple(
            str(value)
            for value in (
                await context.execute(
                    select(Guideline.id).where(
                        Guideline.board_id == board_id,
                        Guideline.scope == "inline",
                    )
                )
            ).scalars()
        )
        code_traceability_target_ids = tuple(
            str(value)
            for value in (
                await context.execute(
                    select(ImplementationTargetRow.id).where(
                        ImplementationTargetRow.board_id == board_id
                    )
                )
            ).scalars()
        )

        try:
            quality_postcondition = await CommunitySqlAlchemyQualityAssessmentLifecycle(
                context
            ).apply_purge_plan(quality_purge_plan)
            quality_lifecycle.validate_purge_postcondition(
                plan=quality_purge_plan,
                postcondition=quality_postcondition,
            )

            # Spec precedence edges and their idempotency/result ledger are
            # immutable outside governed erasure.  Purge both explicitly while
            # the transaction-scoped Board permit is visible to their guards;
            # the later Board DELETE must not depend on an implicit cascade to
            # cross the immutability boundary.
            await context.execute(
                delete(SpecDependency).where(SpecDependency.board_id == board_id)
            )
            await context.execute(
                delete(SpecDependencyOperation).where(
                    SpecDependencyOperation.board_id == board_id
                )
            )

            # Code Traceability attestations are physically immutable during
            # ordinary operation.  Delete the complete graph in FK order while
            # the board-scoped permit is visible to every immutable trigger.
            await context.execute(
                delete(TargetOverlapAcknowledgementRow).where(
                    TargetOverlapAcknowledgementRow.board_id == board_id
                )
            )
            await context.execute(
                delete(ImplementationTargetExecutionRecordRow).where(
                    ImplementationTargetExecutionRecordRow.board_id == board_id
                )
            )
            if code_traceability_target_ids:
                await context.execute(
                    delete(ImplementationTargetSpecLinkRow).where(
                        ImplementationTargetSpecLinkRow.target_id.in_(
                            code_traceability_target_ids
                        )
                    )
                )
                await context.execute(
                    delete(ImplementationTargetEvidenceLinkRow).where(
                        ImplementationTargetEvidenceLinkRow.target_id.in_(
                            code_traceability_target_ids
                        )
                    )
                )
            await context.execute(
                delete(ImplementationTargetResolutionRow).where(
                    ImplementationTargetResolutionRow.board_id == board_id
                )
            )
            await context.execute(
                delete(ImplementationTargetRow).where(
                    ImplementationTargetRow.board_id == board_id
                )
            )
            await context.execute(
                delete(CodeEvidenceDispositionRow).where(
                    CodeEvidenceDispositionRow.board_id == board_id
                )
            )
            await context.execute(
                delete(CodeEvidenceSpecLinkRow).where(
                    CodeEvidenceSpecLinkRow.board_id == board_id
                )
            )
            await context.execute(
                delete(CodeEvidenceRow).where(CodeEvidenceRow.board_id == board_id)
            )
            await context.execute(
                delete(CodeInvestigationHeadRow).where(
                    CodeInvestigationHeadRow.board_id == board_id
                )
            )
            await context.execute(
                delete(CodeInvestigationReceiptRevocationRow).where(
                    CodeInvestigationReceiptRevocationRow.board_id == board_id
                )
            )
            await context.execute(
                delete(CodeInvestigationReceiptRow).where(
                    CodeInvestigationReceiptRow.board_id == board_id
                )
            )
            await context.execute(
                delete(CodeInvestigationRequestRow).where(
                    CodeInvestigationRequestRow.board_id == board_id
                )
            )
            await context.execute(
                delete(CodeTraceabilityWaiverRow).where(
                    CodeTraceabilityWaiverRow.board_id == board_id
                )
            )

            # SK-B3 semantic governance is independently append-only and must
            # participate in the same permit-scoped physical erasure. Delete
            # lifecycle heads before exact receipt/binding authorities; their
            # deferred/cascading lineage removes the corresponding events.
            await context.execute(
                delete(SemanticGuidelineWaiverRow).where(
                    SemanticGuidelineWaiverRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticGuidelineWaiverEventRow).where(
                    SemanticGuidelineWaiverEventRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticGuidelineSkipRow).where(
                    SemanticGuidelineSkipRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticGuidelineFindingRow).where(
                    SemanticGuidelineFindingRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticGuidelineMetricResultRow).where(
                    SemanticGuidelineMetricResultRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticGuidelineAssessmentReceiptRow).where(
                    SemanticGuidelineAssessmentReceiptRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticSubjectVersionRow).where(
                    SemanticSubjectVersionRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticSubjectVersionEventRow).where(
                    SemanticSubjectVersionEventRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticGuidelineLegacyMigrationRow).where(
                    SemanticGuidelineLegacyMigrationRow.board_id == board_id
                )
            )
            await context.execute(
                delete(SemanticGuidelineBindingConfigurationRow).where(
                    SemanticGuidelineBindingConfigurationRow.board_id == board_id
                )
            )

            # SK-B policy history is append-only during ordinary lifecycle
            # operations.  Right-to-erasure is the sole physical-delete path:
            # remove receipt children before their exact binding/revision pins,
            # then remove bindings and inline revision clusters while the
            # scoped permit is still visible to the immutable guards.
            receipt_ids = tuple(
                str(value)
                for value in (
                    await context.execute(
                        select(PolicyComplianceReceiptRow.receipt_id).where(
                            PolicyComplianceReceiptRow.board_id == board_id
                        )
                    )
                ).scalars()
            )
            waiver_event_ids = tuple(
                str(value)
                for value in (
                    await context.execute(
                        select(PolicyWaiverEventRow.event_id)
                        .where(PolicyWaiverEventRow.board_id == board_id)
                        .order_by(
                            PolicyWaiverEventRow.waiver_id.asc(),
                            PolicyWaiverEventRow.waiver_revision.desc(),
                        )
                    )
                ).scalars()
            )
            for event_id in waiver_event_ids:
                result = await context.execute(
                    delete(PolicyWaiverEventRow).where(
                        PolicyWaiverEventRow.event_id == event_id
                    )
                )
                if int(result.rowcount or 0) != 1:
                    raise BoardRelationalErasureError(
                        "board_erasure_waiver_event_delete_mismatch:" + event_id
                    )
            await context.execute(
                delete(PolicyWaiverRow).where(PolicyWaiverRow.board_id == board_id)
            )
            await context.execute(
                delete(PolicyComplianceFindingRow).where(
                    PolicyComplianceFindingRow.board_id == board_id
                )
            )
            if receipt_ids:
                await context.execute(
                    delete(PolicyComplianceAdoptedRevisionRow).where(
                        PolicyComplianceAdoptedRevisionRow.receipt_id.in_(receipt_ids)
                    )
                )
            await context.execute(
                delete(PolicyComplianceReceiptRow).where(
                    PolicyComplianceReceiptRow.board_id == board_id
                )
            )
            # B08 has deferred binding -> operation proofs and reciprocal
            # operation -> binding lineage. Remove both ledgers inside this
            # permit-scoped transaction before bindings, then receipt evidence.
            await context.execute(
                delete(GuidelineRetirementImpactRow).where(
                    GuidelineRetirementImpactRow.board_id == board_id
                )
            )
            await context.execute(
                delete(GuidelineImpactUnlinkRow).where(
                    GuidelineImpactUnlinkRow.board_id == board_id
                )
            )
            await context.execute(
                delete(GuidelineImpactAdoptionRow).where(
                    GuidelineImpactAdoptionRow.board_id == board_id
                )
            )
            await context.execute(
                delete(GuidelineBoardBindingRow).where(
                    GuidelineBoardBindingRow.board_id == board_id
                )
            )
            await context.execute(
                delete(GuidelineImpactItemRow).where(
                    GuidelineImpactItemRow.board_id == board_id
                )
            )
            await context.execute(
                delete(GuidelineImpactReceiptRow).where(
                    GuidelineImpactReceiptRow.board_id == board_id
                )
            )
            if inline_guideline_ids:
                await context.execute(
                    delete(GuidelineBoardBindingRow).where(
                        GuidelineBoardBindingRow.guideline_id.in_(inline_guideline_ids)
                    )
                )
                await context.execute(
                    delete(GuidelineHeadRow).where(
                        GuidelineHeadRow.guideline_id.in_(inline_guideline_ids)
                    )
                )
                await context.execute(
                    delete(GuidelineRetirementRow).where(
                        GuidelineRetirementRow.guideline_id.in_(inline_guideline_ids)
                    )
                )
                inline_revision_ids = tuple(
                    str(value)
                    for value in (
                        await context.execute(
                            select(GuidelineRevisionRow.revision_id)
                            .where(
                                GuidelineRevisionRow.guideline_id.in_(
                                    inline_guideline_ids
                                )
                            )
                            .order_by(
                                GuidelineRevisionRow.revision_number.desc(),
                                GuidelineRevisionRow.revision_id.desc(),
                            )
                        )
                    ).scalars()
                )
                for revision_id in inline_revision_ids:
                    await context.execute(
                        delete(SemanticGuidelineRevisionRow).where(
                            SemanticGuidelineRevisionRow.revision_id == revision_id
                        )
                    )
                    await context.execute(
                        delete(GuidelineRevisionRow).where(
                            GuidelineRevisionRow.revision_id == revision_id
                        )
                    )

            # Append-only mutation records hold a RESTRICT/SET NULL reference
            # to the propagation scope, so they must be removed first.
            for model in (
                KnowledgeMutationAttemptRecord,
                KnowledgeMutationLedgerRecord,
            ):
                await context.execute(delete(model).where(model.board_id == board_id))

            # Temporal rows point at their successor with self-RESTRICT FKs.
            # Deleting the oldest unreferenced row first preserves that
            # invariant without weakening UPDATE immutability.
            await _delete_self_referencing_history(
                context,
                model=KnowledgeSnapshotRecord,
                identity=KnowledgeSnapshotRecord.snapshot_id,
                scope_ids=propagation_scope_ids,
            )
            await _delete_self_referencing_history(
                context,
                model=KnowledgeTombstoneRecord,
                identity=KnowledgeTombstoneRecord.tombstone_id,
                scope_ids=propagation_scope_ids,
            )
            await _delete_self_referencing_history(
                context,
                model=KnowledgeAssignmentRecord,
                identity=KnowledgeAssignmentRecord.assignment_id,
                scope_ids=propagation_scope_ids,
            )
            if propagation_scope_ids:
                await context.execute(
                    delete(KnowledgePropagationScopeRecord).where(
                        KnowledgePropagationScopeRecord.id.in_(propagation_scope_ids)
                    )
                )

            # Cognitive revisions RESTRICT parent deletion and both tables are
            # guarded by immutable-history triggers.
            if cognitive_source_ids:
                await context.execute(
                    delete(KGCognitiveSourceRevision).where(
                        KGCognitiveSourceRevision.cognitive_source_id.in_(
                            cognitive_source_ids
                        )
                    )
                )
            await context.execute(
                delete(KGCognitiveSource).where(KGCognitiveSource.board_id == board_id)
            )

            # Remaining board-scoped rows have no boards.id FK and would
            # otherwise survive the source Board DELETE.
            for model in (
                # The exact ACK journal RESTRICTs its consolidation audit.
                # Its terminal receipt is scope-owned evidence, so remove the
                # receipt first and the journal before audit/node history.
                ExactRebuildConsolidationCompensation,
                ExactRebuildConsolidationAckJournal,
                KuzuNodeRef,
                ConsolidationAudit,
                ConsolidationQueue,
                GlobalUpdateOutbox,
                KGEquivalenceLedger,
                KGCurationProposal,
                DesignSystemGateAudit,
                ActivityLog,
                SemanticGuidelineAssessmentReceiptRow,
                SemanticGuidelineBindingConfigurationRow,
                SemanticGuidelineFindingRow,
                SemanticGuidelineLegacyMigrationRow,
                SemanticGuidelineMetricResultRow,
                SemanticGuidelineSkipRow,
                SemanticGuidelineWaiverEventRow,
                SemanticGuidelineWaiverRow,
                SemanticSubjectVersionEventRow,
                SemanticSubjectVersionRow,
            ):
                await context.execute(delete(model).where(model.board_id == board_id))

            await context.execute(
                update(GlobalDiscoveryDeliveryRedriveControl)
                .where(
                    GlobalDiscoveryDeliveryRedriveControl.cursor_board_id == board_id
                )
                .values(
                    cursor_board_id=None,
                    cursor_oldest_at=None,
                    cursor_delivery_key=None,
                    checkpoint_version=(
                        GlobalDiscoveryDeliveryRedriveControl.checkpoint_version + 1
                    ),
                    updated_at=func.now(),
                )
            )

            board = await context.get(Board, board_id)
            if board is not None and isinstance(board.settings, dict):
                settings = dict(board.settings)
                settings.pop(HISTORICAL_PROGRESS_SETTINGS_KEY, None)
                board.settings = settings
                flag_modified(board, "settings")

            # Verify every explicitly purged direct board identity before the
            # caller is allowed to commit.
            direct_models = (
                KnowledgeMutationAttemptRecord,
                KnowledgeMutationLedgerRecord,
                KnowledgePropagationScopeRecord,
                KGCognitiveSource,
                ExactRebuildConsolidationCompensation,
                ExactRebuildConsolidationAckJournal,
                KuzuNodeRef,
                ConsolidationAudit,
                ConsolidationQueue,
                GlobalUpdateOutbox,
                KGEquivalenceLedger,
                KGCurationProposal,
                DesignSystemGateAudit,
                ActivityLog,
                SemanticGuidelineAssessmentReceiptRow,
                SemanticGuidelineBindingConfigurationRow,
                SemanticGuidelineFindingRow,
                SemanticGuidelineLegacyMigrationRow,
                SemanticGuidelineMetricResultRow,
                SemanticGuidelineSkipRow,
                SemanticGuidelineWaiverEventRow,
                SemanticGuidelineWaiverRow,
                SemanticSubjectVersionEventRow,
                SemanticSubjectVersionRow,
                CodeInvestigationRequestRow,
                CodeInvestigationReceiptRow,
                CodeInvestigationReceiptRevocationRow,
                CodeInvestigationHeadRow,
                CodeEvidenceRow,
                CodeEvidenceSpecLinkRow,
                CodeEvidenceDispositionRow,
                ImplementationTargetRow,
                ImplementationTargetResolutionRow,
                ImplementationTargetExecutionRecordRow,
                TargetOverlapAcknowledgementRow,
                CodeTraceabilityWaiverRow,
                SpecDependency,
                SpecDependencyOperation,
            )
            residuals = {
                model.__tablename__: await _count_where(
                    context,
                    model,
                    model.board_id == board_id,
                )
                for model in direct_models
            }
            residuals[GuidelineBoardBindingRow.__tablename__] = await _count_where(
                context,
                GuidelineBoardBindingRow,
                GuidelineBoardBindingRow.board_id == board_id,
            )
            residuals[GuidelineImpactAdoptionRow.__tablename__] = await _count_where(
                context,
                GuidelineImpactAdoptionRow,
                GuidelineImpactAdoptionRow.board_id == board_id,
            )
            residuals[GuidelineImpactUnlinkRow.__tablename__] = await _count_where(
                context,
                GuidelineImpactUnlinkRow,
                GuidelineImpactUnlinkRow.board_id == board_id,
            )
            residuals[GuidelineRetirementImpactRow.__tablename__] = await _count_where(
                context,
                GuidelineRetirementImpactRow,
                GuidelineRetirementImpactRow.board_id == board_id,
            )
            residuals[GuidelineImpactItemRow.__tablename__] = await _count_where(
                context,
                GuidelineImpactItemRow,
                GuidelineImpactItemRow.board_id == board_id,
            )
            residuals[GuidelineImpactReceiptRow.__tablename__] = await _count_where(
                context,
                GuidelineImpactReceiptRow,
                GuidelineImpactReceiptRow.board_id == board_id,
            )
            residuals[PolicyComplianceReceiptRow.__tablename__] = await _count_where(
                context,
                PolicyComplianceReceiptRow,
                PolicyComplianceReceiptRow.board_id == board_id,
            )
            residuals[PolicyWaiverRow.__tablename__] = await _count_where(
                context,
                PolicyWaiverRow,
                PolicyWaiverRow.board_id == board_id,
            )
            residuals[PolicyWaiverEventRow.__tablename__] = await _count_where(
                context,
                PolicyWaiverEventRow,
                PolicyWaiverEventRow.board_id == board_id,
            )
            residuals[PolicyComplianceFindingRow.__tablename__] = await _count_where(
                context,
                PolicyComplianceFindingRow,
                PolicyComplianceFindingRow.board_id == board_id,
            )
            for model in (
                ImplementationTargetSpecLinkRow,
                ImplementationTargetEvidenceLinkRow,
            ):
                residuals[model.__tablename__] = 0
                if code_traceability_target_ids:
                    residuals[model.__tablename__] = await _count_where(
                        context,
                        model,
                        model.target_id.in_(code_traceability_target_ids),
                    )
            residuals[PolicyComplianceAdoptedRevisionRow.__tablename__] = 0
            if receipt_ids:
                residuals[
                    PolicyComplianceAdoptedRevisionRow.__tablename__
                ] = await _count_where(
                    context,
                    PolicyComplianceAdoptedRevisionRow,
                    PolicyComplianceAdoptedRevisionRow.receipt_id.in_(receipt_ids),
                )
            if inline_guideline_ids:
                residuals[GuidelineHeadRow.__tablename__] = await _count_where(
                    context,
                    GuidelineHeadRow,
                    GuidelineHeadRow.guideline_id.in_(inline_guideline_ids),
                )
                residuals[GuidelineRevisionRow.__tablename__] = await _count_where(
                    context,
                    GuidelineRevisionRow,
                    GuidelineRevisionRow.guideline_id.in_(inline_guideline_ids),
                )
                residuals[
                    SemanticGuidelineRevisionRow.__tablename__
                ] = await _count_where(
                    context,
                    SemanticGuidelineRevisionRow,
                    SemanticGuidelineRevisionRow.guideline_id.in_(inline_guideline_ids),
                )
                residuals[GuidelineRetirementRow.__tablename__] = await _count_where(
                    context,
                    GuidelineRetirementRow,
                    GuidelineRetirementRow.guideline_id.in_(inline_guideline_ids),
                )
            if cognitive_source_ids:
                residuals[KGCognitiveSourceRevision.__tablename__] = await _count_where(
                    context,
                    KGCognitiveSourceRevision,
                    KGCognitiveSourceRevision.cognitive_source_id.in_(
                        cognitive_source_ids
                    ),
                )
            for model in (
                KnowledgeSnapshotRecord,
                KnowledgeTombstoneRecord,
                KnowledgeAssignmentRecord,
            ):
                residuals[model.__tablename__] = 0
                if propagation_scope_ids:
                    residuals[model.__tablename__] = await _count_where(
                        context,
                        model,
                        model.scope_id.in_(propagation_scope_ids),
                    )
            residuals[
                GlobalDiscoveryDeliveryRedriveControl.__tablename__
            ] = await _count_where(
                context,
                GlobalDiscoveryDeliveryRedriveControl,
                GlobalDiscoveryDeliveryRedriveControl.cursor_board_id == board_id,
            )
            nonzero = {table: count for table, count in residuals.items() if count}
            if nonzero:
                raise BoardRelationalErasureError(
                    f"board_erasure_residuals:{board_id}:{nonzero}"
                )
            all_board_purges_completed = True
        finally:
            result = await context.execute(
                delete(BoardErasurePermit).where(
                    BoardErasurePermit.board_id == board_id,
                    BoardErasurePermit.permit_token == permit_token,
                )
            )
            permit_released = int(result.rowcount or 0) == 1
            if not permit_released:
                raise BoardRelationalErasureError(
                    f"board_erasure_permit_release_failed:{board_id}"
                )
        if quality_postcondition is None:
            raise BoardRelationalErasureError(
                f"board_erasure_quality_postcondition_missing:{board_id}"
            )
        completion = AssessmentBoardErasureCompletion(
            target=quality_purge_plan.target,
            quality_purge_postcondition=quality_postcondition,
            board_erasure_permit_id=permit_token,
            all_board_purges_completed=all_board_purges_completed,
            permit_released=permit_released,
            verified_at=datetime.now(timezone.utc),
        )
        quality_lifecycle.validate_board_erasure_completion(
            plan=quality_purge_plan,
            inner_postcondition=quality_postcondition,
            completion=completion,
        )

    async def stage_board_erasure_job(
        self,
        context: Any,
        *,
        board_id: str,
        actor_id: str,
    ) -> BoardErasureJobFact:
        if await context.get(BoardErasureJob, board_id) is not None:
            raise BoardRelationalErasureError(f"board_erasure_job_conflict:{board_id}")
        now = datetime.now(timezone.utc)
        row = BoardErasureJob(
            board_id=board_id,
            actor_id=actor_id,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        context.add(row)
        await context.flush()
        return _board_erasure_job_fact(row)

    async def get_board_erasure_job(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> BoardErasureJobFact | None:
        row = await context.get(BoardErasureJob, board_id)
        return _board_erasure_job_fact(row) if row is not None else None

    async def list_due_board_erasure_jobs(
        self,
        context: Any,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[BoardErasureJobFact, ...]:
        rows = (
            (
                await context.execute(
                    select(BoardErasureJob)
                    .where(
                        BoardErasureJob.status == "pending",
                        BoardErasureJob.next_attempt_at <= now,
                    )
                    .order_by(
                        BoardErasureJob.next_attempt_at.asc(),
                        BoardErasureJob.board_id.asc(),
                    )
                    .limit(max(1, min(int(limit), 100)))
                )
            )
            .scalars()
            .all()
        )
        return tuple(_board_erasure_job_fact(row) for row in rows)

    async def record_board_erasure_failure(
        self,
        context: Any,
        *,
        board_id: str,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        await context.execute(
            update(BoardErasureJob)
            .where(
                BoardErasureJob.board_id == board_id,
                BoardErasureJob.status == "pending",
            )
            .values(
                attempts=BoardErasureJob.attempts + 1,
                last_error=error[:2048],
                next_attempt_at=next_attempt_at,
                updated_at=func.now(),
            )
        )

    async def complete_board_erasure_job(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> bool:
        result = await context.execute(
            delete(BoardErasureJob).where(
                BoardErasureJob.board_id == board_id,
                BoardErasureJob.status == "pending",
            )
        )
        return int(result.rowcount or 0) == 1

    def add_boost_audit(self, context: Any, audit: BoostAuditRecord) -> None:
        context.add(
            ConsolidationAudit(
                session_id=audit.session_id,
                board_id=audit.board_id,
                artifact_id=audit.artifact_id,
                artifact_type="boost",
                agent_id=audit.agent_id,
                started_at=audit.started_at,
                committed_at=audit.committed_at,
                nodes_added=0,
                edges_added=0,
            )
        )

    async def commit(self, context: Any) -> None:
        await context.commit()


__all__ = [
    "BoardRelationalErasureError",
    "CommunitySqlAlchemyKGGovernanceStore",
]
