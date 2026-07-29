"""SQLite control-plane adapter for the SK-A C7 legacy import epoch.

The import is intentionally operation-transactional.  Planning, each
candidate resolution/checkpoint, and completion are separate durable
``BEGIN IMMEDIATE`` transactions so a process crash can resume without
duplicating receipts or audit rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_database import (
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
    Ideation,
    IdeationHistory,
    IdeationQAItem,
    QualityAssessmentHeadRow,
    QualityAssessmentLegacyImportCandidateRow,
    QualityAssessmentLegacyImportCheckpointRow,
    QualityAssessmentLegacyImportCompletionRow,
    QualityAssessmentLegacyImportResolutionRow,
    QualityAssessmentLegacyImportRunRow,
    QualityAssessmentOutboxRow,
    QualityAssessmentReceiptRow,
    RefinementHistory,
    Spec,
    SpecHistory,
    SpecQAItem,
)
from okto_pulse.core.domain.quality_assessment import (
    QUALITY_ASSESSMENT_CONTRACT_VERSION,
    AssessmentDigestSet,
    AssessmentKind,
    AssessmentScale,
    AssessmentScaleKind,
    AssessmentSubjectRef,
    AssessmentSubjectType,
    AssessmentVersionSet,
    ScoreDirection,
)
from okto_pulse.core.domain.quality_assessment_legacy_import import (
    QUALITY_ASSESSMENT_LEGACY_IMPORT_EPOCH,
    LegacyIdeationSnapshot,
    LegacyImportCandidate,
    LegacyImportCandidateKey,
    LegacyImportCandidateOutcome,
    LegacyImportCandidateWork,
    LegacyImportCheckpoint,
    LegacyImportCompletionExpectation,
    LegacyImportPlan,
    LegacyImportPostcondition,
    LegacyImportRejectionCount,
    LegacyImportResolution,
    LegacyImportSelectionReason,
    LegacyImportSourceSnapshot,
    LegacySpecSnapshot,
)
from okto_pulse.core.domain.quality_canonicalization import (
    canonical_sha256,
)
from okto_pulse.core.ports.quality_assessment_legacy_import import (
    LegacyImportCheckpointConflict,
    LegacyImportPlanConflict,
    LegacyImportPostconditionConflict,
)
from okto_pulse.core.services.ambiguity_assessment import (
    ambiguity_digest_set,
    ambiguity_versions_v1,
    resolve_ambiguity_gate_configuration,
)
from okto_pulse.core.services.quality_projection_currentness import (
    legacy_spec_validation_digest_set_v1,
    legacy_spec_validation_versions_v1,
)

_CONSOLIDATION_HANDLER_NAME = "ConsolidationEnqueuer"


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    resolved = value or fallback
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return _aware(value, value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value,
        str | bytes | bytearray,
    ):
        return [_json_safe(item) for item in value]
    return value


def _candidate_from_row(
    row: QualityAssessmentLegacyImportCandidateRow,
) -> LegacyImportCandidate:
    return LegacyImportCandidate(
        key=LegacyImportCandidateKey(
            board_id=row.board_id,
            subject_type=AssessmentSubjectType(row.subject_type),
            subject_id=row.subject_id,
            assessment_kind=AssessmentKind(row.assessment_kind),
            epoch=row.epoch,
        ),
        subject_version=row.subject_version,
        scale=AssessmentScale(
            kind=AssessmentScaleKind(row.scale_kind),
            minimum=row.scale_minimum,
            maximum=row.scale_maximum,
            direction=ScoreDirection(row.scale_direction),
        ),
        score=row.score,
        justification=row.justification,
        digests=AssessmentDigestSet(
            content_digest=row.content_digest,
            clarification_digest=row.clarification_digest,
            ruleset_digest=row.ruleset_digest,
            taxonomy_digest=row.taxonomy_digest,
            policy_digest=row.policy_digest,
            input_digest=row.input_digest,
            canonicalization_version=row.canonicalization_version,
        ),
        versions=AssessmentVersionSet(
            ruleset_version=row.ruleset_version,
            taxonomy_version=row.taxonomy_version,
            analyzer_version=row.analyzer_version,
            policy_version=row.policy_version,
        ),
        legacy_source_id=row.legacy_source_id,
        legacy_source_digest=row.legacy_source_digest,
        observed_at=_aware(row.observed_at, row.observed_at),
    )


def _checkpoint_from_row(
    row: QualityAssessmentLegacyImportCheckpointRow,
) -> LegacyImportCheckpoint:
    key = None
    if row.processed_count:
        key = LegacyImportCandidateKey(
            board_id=row.board_id,
            subject_type=AssessmentSubjectType(str(row.last_subject_type)),
            subject_id=str(row.last_subject_id),
            assessment_kind=AssessmentKind(str(row.last_assessment_kind)),
            epoch=row.epoch,
        )
    return LegacyImportCheckpoint(
        board_id=row.board_id,
        epoch=row.epoch,
        plan_digest=row.plan_digest,
        candidate_digest=row.candidate_digest,
        processed_count=row.processed_count,
        imported_count=row.imported_count,
        native_wins_count=row.native_wins_count,
        cursor_ordinal=row.cursor_ordinal,
        last_candidate_key=key,
        updated_at=_aware(row.updated_at, row.updated_at),
    )


def _completion_postcondition_digest(
    expectation: LegacyImportCompletionExpectation,
) -> str:
    return canonical_sha256(
        {
            "board_id": expectation.board_id,
            "epoch": expectation.epoch,
            "plan_digest": expectation.plan_digest,
            "candidate_digest": expectation.candidate_digest,
            "candidate_count": expectation.candidate_count,
            "checkpoint": {
                "processed": expectation.checkpoint.processed_count,
                "imported": expectation.checkpoint.imported_count,
                "native": expectation.checkpoint.native_wins_count,
            },
            "flags": {
                "all_candidates_resolved": True,
                "unique_identity_satisfied": True,
                "checkpoint_consistent": True,
                "zero_orphans": True,
                "audit_bundles_consistent": True,
                "epoch_closed": True,
            },
        }
    )


class CommunitySqlAlchemyQualityAssessmentLegacyImport:
    """Combined legacy source and durable epoch adapter."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()

    @asynccontextmanager
    async def _immediate_session(self):
        session = self._session_factory()
        try:
            await session.execute(text("BEGIN IMMEDIATE"))
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def _capture_with_session(
        self,
        session: AsyncSession,
        *,
        board_id: str,
        cutoff: datetime,
    ) -> LegacyImportSourceSnapshot:
        board = await session.get(Board, board_id)
        if board is None:
            raise LegacyImportPlanConflict("legacy_import_board_missing")
        settings = board.settings if isinstance(board.settings, dict) else {}
        ideations = list(
            (
                await session.execute(
                    select(Ideation)
                    .where(Ideation.board_id == board_id)
                    .order_by(Ideation.id)
                )
            )
            .scalars()
            .all()
        )
        specs = list(
            (
                await session.execute(
                    select(Spec).where(Spec.board_id == board_id).order_by(Spec.id)
                )
            )
            .scalars()
            .all()
        )
        ideation_snapshots: list[LegacyIdeationSnapshot] = []
        for ideation in ideations:
            qa_rows = list(
                (
                    await session.execute(
                        select(IdeationQAItem)
                        .where(IdeationQAItem.ideation_id == ideation.id)
                        .order_by(IdeationQAItem.id)
                    )
                )
                .scalars()
                .all()
            )
            configuration = resolve_ambiguity_gate_configuration(
                AssessmentSubjectType.IDEATION,
                settings,
            )
            observed_at = _aware(
                getattr(ideation, "updated_at", None)
                or getattr(ideation, "created_at", None),
                cutoff,
            )
            ideation_snapshots.append(
                LegacyIdeationSnapshot.capture(
                    subject=AssessmentSubjectRef(
                        board_id=board_id,
                        subject_type=AssessmentSubjectType.IDEATION,
                        subject_id=ideation.id,
                        subject_version=int(ideation.version or 1),
                    ),
                    observed_at=observed_at,
                    active_at_cutoff=(
                        not bool(ideation.archived)
                        and _enum_value(ideation.status) != "cancelled"
                    ),
                    gate_enabled_at_cutoff=configuration.required,
                    scope_assessment=ideation.scope_assessment or {},
                    digests=ambiguity_digest_set(
                        subject_type=AssessmentSubjectType.IDEATION,
                        subject=ideation,
                        qa_items=qa_rows,
                        configuration=configuration,
                    ),
                    versions=ambiguity_versions_v1(),
                )
            )
        spec_snapshots: list[LegacySpecSnapshot] = []
        for spec in specs:
            qa_rows = list(
                (
                    await session.execute(
                        select(SpecQAItem)
                        .where(SpecQAItem.spec_id == spec.id)
                        .order_by(SpecQAItem.id)
                    )
                )
                .scalars()
                .all()
            )
            observed_at = _aware(
                getattr(spec, "updated_at", None)
                or getattr(spec, "created_at", None),
                cutoff,
            )
            spec_snapshots.append(
                LegacySpecSnapshot.capture(
                    subject=AssessmentSubjectRef(
                        board_id=board_id,
                        subject_type=AssessmentSubjectType.SPEC,
                        subject_id=spec.id,
                        subject_version=int(spec.version or 1),
                    ),
                    observed_at=observed_at,
                    active_at_cutoff=(
                        not bool(spec.archived)
                        and _enum_value(spec.status) != "cancelled"
                    ),
                    current_validation_id=spec.current_validation_id,
                    validations=_json_safe(spec.validations or []),
                    digests=legacy_spec_validation_digest_set_v1(
                        subject=spec,
                        qa_items=qa_rows,
                    ),
                    versions=legacy_spec_validation_versions_v1(),
                )
            )
        return LegacyImportSourceSnapshot(
            board_id=board_id,
            cutoff=cutoff,
            ideations=tuple(ideation_snapshots),
            specs=tuple(spec_snapshots),
        )

    async def capture_legacy_snapshot(
        self,
        *,
        board_id: str,
        cutoff: datetime,
    ) -> LegacyImportSourceSnapshot:
        async with self._session_factory() as session:
            return await self._capture_with_session(
                session,
                board_id=board_id,
                cutoff=cutoff,
            )

    async def _load_plan_with_session(
        self,
        session: AsyncSession,
        *,
        board_id: str,
        epoch: str,
    ) -> LegacyImportPlan | None:
        run = await session.get(
            QualityAssessmentLegacyImportRunRow,
            (board_id, epoch),
        )
        if run is None:
            return None
        candidates = list(
            (
                await session.execute(
                    select(QualityAssessmentLegacyImportCandidateRow)
                    .where(
                        QualityAssessmentLegacyImportCandidateRow.board_id
                        == board_id,
                        QualityAssessmentLegacyImportCandidateRow.epoch == epoch,
                    )
                    .order_by(
                        QualityAssessmentLegacyImportCandidateRow.ordinal
                    )
                )
            )
            .scalars()
            .all()
        )
        rejection_counts = tuple(
            LegacyImportRejectionCount(
                reason=LegacyImportSelectionReason(item["reason"]),
                count=int(item["count"]),
            )
            for item in (run.rejection_counts_json or [])
        )
        plan = LegacyImportPlan(
            board_id=run.board_id,
            cutoff=_aware(run.cutoff, run.cutoff),
            code_digest=run.code_digest,
            candidates=tuple(_candidate_from_row(row) for row in candidates),
            rejection_counts=rejection_counts,
            candidate_digest=run.candidate_digest,
            epoch=run.epoch,
            contract_version=run.contract_version,
        )
        if (
            plan.plan_digest != run.plan_digest
            or plan.candidate_count != run.candidate_count
            or [row.ordinal for row in candidates]
            != list(range(plan.candidate_count))
        ):
            raise LegacyImportPlanConflict(
                "legacy_import_stored_plan_corrupt"
            )
        return plan

    async def load_plan(
        self,
        *,
        board_id: str,
        epoch: str,
    ) -> LegacyImportPlan | None:
        async with self._session_factory() as session:
            return await self._load_plan_with_session(
                session,
                board_id=board_id,
                epoch=epoch,
            )

    async def acquire_or_build_plan(
        self,
        *,
        board_id: str,
        cutoff: datetime,
        planner: Callable[[LegacyImportSourceSnapshot], LegacyImportPlan],
    ) -> LegacyImportPlan:
        async with self._immediate_session() as session:
            existing = await self._load_plan_with_session(
                session,
                board_id=board_id,
                epoch=QUALITY_ASSESSMENT_LEGACY_IMPORT_EPOCH,
            )
            if existing is not None:
                return existing
            source = await self._capture_with_session(
                session,
                board_id=board_id,
                cutoff=cutoff,
            )
            plan = planner(source)
            if (
                plan.board_id != board_id
                or plan.cutoff != cutoff
                or plan.epoch != QUALITY_ASSESSMENT_LEGACY_IMPORT_EPOCH
            ):
                raise LegacyImportPlanConflict(
                    "legacy_import_planner_scope_mismatch"
                )
            created_at = datetime.now(timezone.utc)
            session.add(
                QualityAssessmentLegacyImportRunRow(
                    board_id=plan.board_id,
                    epoch=plan.epoch,
                    cutoff=plan.cutoff,
                    code_digest=plan.code_digest,
                    candidate_digest=plan.candidate_digest,
                    plan_digest=plan.plan_digest,
                    candidate_count=plan.candidate_count,
                    rejection_counts_json=[
                        {
                            "reason": item.reason.value,
                            "count": item.count,
                        }
                        for item in plan.rejection_counts
                    ],
                    contract_version=plan.contract_version,
                    created_at=created_at,
                )
            )
            # These persistence-only models deliberately declare no ORM
            # relationships. Flush each FK layer explicitly while retaining
            # the single BEGIN IMMEDIATE planning transaction.
            await session.flush()
            for ordinal, candidate in enumerate(plan.candidates):
                session.add(
                    QualityAssessmentLegacyImportCandidateRow(
                        board_id=candidate.key.board_id,
                        epoch=candidate.key.epoch,
                        ordinal=ordinal,
                        subject_type=candidate.key.subject_type.value,
                        subject_id=candidate.key.subject_id,
                        assessment_kind=candidate.key.assessment_kind.value,
                        subject_version=candidate.subject_version,
                        scale_kind=candidate.scale.kind.value,
                        scale_minimum=candidate.scale.minimum,
                        scale_maximum=candidate.scale.maximum,
                        scale_direction=candidate.scale.direction.value,
                        score=candidate.score,
                        justification=candidate.justification,
                        content_digest=candidate.digests.content_digest,
                        clarification_digest=(
                            candidate.digests.clarification_digest
                        ),
                        ruleset_digest=candidate.digests.ruleset_digest,
                        taxonomy_digest=candidate.digests.taxonomy_digest,
                        policy_digest=candidate.digests.policy_digest,
                        input_digest=candidate.digests.input_digest,
                        canonicalization_version=(
                            candidate.digests.canonicalization_version
                        ),
                        ruleset_version=candidate.versions.ruleset_version,
                        taxonomy_version=candidate.versions.taxonomy_version,
                        analyzer_version=candidate.versions.analyzer_version,
                        policy_version=candidate.versions.policy_version,
                        legacy_source_id=candidate.legacy_source_id,
                        legacy_source_digest=candidate.legacy_source_digest,
                        observed_at=candidate.observed_at,
                    )
                )
            await session.flush()
            initial = LegacyImportCheckpoint.initial(
                plan=plan,
                updated_at=created_at,
            )
            session.add(
                QualityAssessmentLegacyImportCheckpointRow(
                    board_id=initial.board_id,
                    epoch=initial.epoch,
                    plan_digest=initial.plan_digest,
                    candidate_digest=initial.candidate_digest,
                    processed_count=0,
                    imported_count=0,
                    native_wins_count=0,
                    cursor_ordinal=None,
                    last_subject_type=None,
                    last_subject_id=None,
                    last_assessment_kind=None,
                    revision=1,
                    updated_at=initial.updated_at,
                )
            )
            await session.flush()
            return plan

    async def load_checkpoint(
        self,
        *,
        board_id: str,
        epoch: str,
        plan_digest: str,
    ) -> LegacyImportCheckpoint | None:
        async with self._session_factory() as session:
            row = await session.get(
                QualityAssessmentLegacyImportCheckpointRow,
                (board_id, epoch),
            )
            if row is None:
                return None
            if row.plan_digest != plan_digest:
                raise LegacyImportCheckpointConflict(
                    "legacy_import_checkpoint_plan_mismatch"
                )
            return _checkpoint_from_row(row)

    @staticmethod
    def _history_binding(subject_type: AssessmentSubjectType):
        if subject_type is AssessmentSubjectType.IDEATION:
            return IdeationHistory, "ideation_id"
        if subject_type is AssessmentSubjectType.SPEC:
            return SpecHistory, "spec_id"
        return RefinementHistory, "refinement_id"

    async def _native_receipt(
        self,
        session: AsyncSession,
        work: LegacyImportCandidateWork,
    ) -> QualityAssessmentReceiptRow | None:
        candidate = work.candidate
        return (
            (
                await session.execute(
                    select(QualityAssessmentReceiptRow)
                    .where(
                        QualityAssessmentReceiptRow.board_id
                        == candidate.key.board_id,
                        QualityAssessmentReceiptRow.subject_type
                        == candidate.key.subject_type.value,
                        QualityAssessmentReceiptRow.subject_id
                        == candidate.key.subject_id,
                        QualityAssessmentReceiptRow.assessment_kind
                        == candidate.key.assessment_kind.value,
                        QualityAssessmentReceiptRow.source == "native",
                    )
                    .order_by(
                        QualityAssessmentReceiptRow.created_at.desc(),
                        QualityAssessmentReceiptRow.id.desc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    def _checkpoint_matches(
        row: QualityAssessmentLegacyImportCheckpointRow,
        expected: LegacyImportCheckpoint,
    ) -> bool:
        return _checkpoint_from_row(row) == expected

    async def apply_candidate_checkpoint(
        self,
        work: LegacyImportCandidateWork,
    ) -> LegacyImportCandidateOutcome:
        async with self._immediate_session() as session:
            existing_resolution = await session.get(
                QualityAssessmentLegacyImportResolutionRow,
                (
                    work.candidate.key.board_id,
                    work.candidate.key.epoch,
                    work.ordinal,
                ),
            )
            if existing_resolution is not None:
                replay_checkpoint = LegacyImportCheckpoint(
                    board_id=existing_resolution.board_id,
                    epoch=existing_resolution.epoch,
                    plan_digest=work.plan_digest,
                    candidate_digest=work.candidate_digest,
                    processed_count=existing_resolution.processed_count,
                    imported_count=existing_resolution.imported_count,
                    native_wins_count=existing_resolution.native_wins_count,
                    cursor_ordinal=work.ordinal,
                    last_candidate_key=work.candidate.key,
                    updated_at=_aware(
                        existing_resolution.resolved_at,
                        existing_resolution.resolved_at,
                    ),
                )
                return LegacyImportCandidateOutcome(
                    work=work,
                    resolution=LegacyImportResolution(
                        existing_resolution.resolution
                    ),
                    receipt_id=existing_resolution.receipt_id,
                    checkpoint=replay_checkpoint,
                    atomic_resolution_applied=True,
                )

            checkpoint_row = await session.get(
                QualityAssessmentLegacyImportCheckpointRow,
                (
                    work.candidate.key.board_id,
                    work.candidate.key.epoch,
                ),
            )
            if checkpoint_row is None or not self._checkpoint_matches(
                checkpoint_row,
                work.expected_checkpoint,
            ):
                raise LegacyImportCheckpointConflict(
                    "legacy_import_checkpoint_cas_conflict"
                )
            native = await self._native_receipt(session, work)
            identity = work.write_identity
            candidate = work.candidate
            if native is not None:
                resolution = LegacyImportResolution.NATIVE_WINS
                receipt_id = native.id
            else:
                existing_receipt = await session.get(
                    QualityAssessmentReceiptRow,
                    identity.receipt_id,
                )
                if existing_receipt is not None:
                    if (
                        existing_receipt.board_id != candidate.key.board_id
                        or existing_receipt.subject_type
                        != candidate.key.subject_type.value
                        or existing_receipt.subject_id
                        != candidate.key.subject_id
                        or existing_receipt.assessment_kind
                        != candidate.key.assessment_kind.value
                        or existing_receipt.request_digest
                        != identity.request_digest
                    ):
                        raise LegacyImportPlanConflict(
                            "legacy_import_receipt_identity_conflict"
                        )
                head = await session.get(
                    QualityAssessmentHeadRow,
                    (
                        candidate.key.board_id,
                        candidate.key.subject_type.value,
                        candidate.key.subject_id,
                        candidate.key.assessment_kind.value,
                    ),
                )
                if head is not None and (
                    existing_receipt is None
                    or head.receipt_id != existing_receipt.id
                ):
                    raise LegacyImportPlanConflict(
                        "legacy_import_non_native_head_conflict"
                    )
                next_revision = 1 if head is None else head.revision
                if existing_receipt is None:
                    session.add(
                        QualityAssessmentReceiptRow(
                            id=identity.receipt_id,
                            board_id=candidate.key.board_id,
                            subject_type=candidate.key.subject_type.value,
                            subject_id=candidate.key.subject_id,
                            subject_version=candidate.subject_version,
                            assessment_kind=(
                                candidate.key.assessment_kind.value
                            ),
                            origin="legacy_import",
                            source="legacy_migration",
                            channel="migration",
                            outcome="recorded",
                            scale_kind=candidate.scale.kind.value,
                            scale_minimum=candidate.scale.minimum,
                            scale_maximum=candidate.scale.maximum,
                            scale_direction=candidate.scale.direction.value,
                            score=candidate.score,
                            justification=candidate.justification,
                            content_digest=candidate.digests.content_digest,
                            clarification_digest=(
                                candidate.digests.clarification_digest
                            ),
                            ruleset_digest=candidate.digests.ruleset_digest,
                            taxonomy_digest=candidate.digests.taxonomy_digest,
                            policy_digest=candidate.digests.policy_digest,
                            input_digest=candidate.digests.input_digest,
                            canonicalization_version=(
                                candidate.digests.canonicalization_version
                            ),
                            ruleset_version=(
                                candidate.versions.ruleset_version
                            ),
                            taxonomy_version=(
                                candidate.versions.taxonomy_version
                            ),
                            analyzer_version=(
                                candidate.versions.analyzer_version
                            ),
                            policy_version=candidate.versions.policy_version,
                            run_identity_digest=(
                                identity.run_identity_digest
                            ),
                            authority_digest=identity.authority_digest,
                            idempotency_key=identity.idempotency_key,
                            request_digest=identity.request_digest,
                            created_by=identity.actor_id,
                            created_at=identity.occurred_at,
                            predecessor_receipt_id=None,
                            contract_version=(
                                QUALITY_ASSESSMENT_CONTRACT_VERSION
                            ),
                            event_id=identity.event_id,
                            history_id=identity.history_id,
                            outbox_id=identity.outbox_id,
                            head_revision=next_revision,
                        )
                    )
                    payload = {
                        "event_schema_version": 1,
                        "receipt_id": identity.receipt_id,
                        "subject_type": candidate.key.subject_type.value,
                        "subject_id": candidate.key.subject_id,
                        "subject_version": candidate.subject_version,
                        "assessment_kind": (
                            candidate.key.assessment_kind.value
                        ),
                        "input_digest": candidate.digests.input_digest,
                        "request_fingerprint": identity.request_digest,
                        "authority_digest": identity.authority_digest,
                        "head_revision": next_revision,
                        "predecessor_receipt_id": None,
                        "outcome": "recorded",
                        "epoch": candidate.key.epoch,
                    }
                    session.add(
                        DomainEventRow(
                            id=identity.event_id,
                            event_type="quality.assessment_recorded.v1",
                            board_id=candidate.key.board_id,
                            actor_id=identity.actor_id,
                            actor_type="system",
                            payload_json=payload,
                            occurred_at=identity.occurred_at,
                        )
                    )
                    session.add(
                        ActivityLog(
                            id=identity.history_id,
                            board_id=candidate.key.board_id,
                            card_id=None,
                            action="quality_assessment_legacy_imported",
                            actor_type="system",
                            actor_id=identity.actor_id,
                            actor_name=identity.actor_id,
                            details=payload,
                            created_at=identity.occurred_at,
                        )
                    )
                    history_model, subject_fk = self._history_binding(
                        candidate.key.subject_type
                    )
                    session.add(
                        history_model(
                            id=identity.history_id,
                            **{subject_fk: candidate.key.subject_id},
                            action="quality_assessment_legacy_imported",
                            actor_type="system",
                            actor_id=identity.actor_id,
                            actor_name=identity.actor_id,
                            changes=[
                                {
                                    "field": "quality_assessment_head",
                                    "old": None,
                                    "new": identity.receipt_id,
                                }
                            ],
                            summary=(
                                f"{candidate.key.assessment_kind.value} "
                                "assessment imported from legacy data"
                            ),
                            version=candidate.subject_version,
                            created_at=identity.occurred_at,
                        )
                    )
                    # Flush the receipt/event/history parents before their
                    # outbox and head pointers; no ORM relationships are
                    # declared for these persistence-only bundles.
                    await session.flush()
                    session.add(
                        DomainEventHandlerExecution(
                            event_id=identity.event_id,
                            handler_name=_CONSOLIDATION_HANDLER_NAME,
                            status="pending",
                            attempts=0,
                        )
                    )
                    session.add(
                        QualityAssessmentOutboxRow(
                            id=identity.outbox_id,
                            event_id=identity.event_id,
                            receipt_id=identity.receipt_id,
                            board_id=candidate.key.board_id,
                            event_type="quality.assessment_recorded.v1",
                            payload_json=payload,
                            status="pending",
                            occurred_at=identity.occurred_at,
                        )
                    )
                    if head is None:
                        session.add(
                            QualityAssessmentHeadRow(
                                board_id=candidate.key.board_id,
                                subject_type=(
                                    candidate.key.subject_type.value
                                ),
                                subject_id=candidate.key.subject_id,
                                assessment_kind=(
                                    candidate.key.assessment_kind.value
                                ),
                                receipt_id=identity.receipt_id,
                                revision=1,
                                updated_at=identity.occurred_at,
                            )
                        )
                    await session.flush()
                resolution = LegacyImportResolution.IMPORTED
                receipt_id = identity.receipt_id

            imported_count = (
                checkpoint_row.imported_count
                + int(resolution is LegacyImportResolution.IMPORTED)
            )
            native_count = (
                checkpoint_row.native_wins_count
                + int(resolution is LegacyImportResolution.NATIVE_WINS)
            )
            processed_count = checkpoint_row.processed_count + 1
            checkpoint_row.processed_count = processed_count
            checkpoint_row.imported_count = imported_count
            checkpoint_row.native_wins_count = native_count
            checkpoint_row.cursor_ordinal = work.ordinal
            checkpoint_row.last_subject_type = (
                candidate.key.subject_type.value
            )
            checkpoint_row.last_subject_id = candidate.key.subject_id
            checkpoint_row.last_assessment_kind = (
                candidate.key.assessment_kind.value
            )
            checkpoint_row.revision += 1
            checkpoint_row.updated_at = identity.occurred_at
            session.add(
                QualityAssessmentLegacyImportResolutionRow(
                    board_id=candidate.key.board_id,
                    epoch=candidate.key.epoch,
                    ordinal=work.ordinal,
                    resolution=resolution.value,
                    processed_count=processed_count,
                    imported_count=imported_count,
                    native_wins_count=native_count,
                    receipt_id=receipt_id,
                    event_id=identity.event_id,
                    history_id=identity.history_id,
                    outbox_id=identity.outbox_id,
                    idempotency_key=identity.idempotency_key,
                    request_digest=identity.request_digest,
                    run_identity_digest=identity.run_identity_digest,
                    authority_digest=identity.authority_digest,
                    actor_id=identity.actor_id,
                    resolved_at=identity.occurred_at,
                )
            )
            await session.flush()
            checkpoint = _checkpoint_from_row(checkpoint_row)
            return LegacyImportCandidateOutcome(
                work=work,
                resolution=resolution,
                receipt_id=receipt_id,
                checkpoint=checkpoint,
                atomic_resolution_applied=True,
            )

    async def _audit_bundle_count(
        self,
        session: AsyncSession,
        *,
        board_id: str,
        epoch: str,
    ) -> int:
        resolutions = list(
            (
                await session.execute(
                    select(QualityAssessmentLegacyImportResolutionRow).where(
                        QualityAssessmentLegacyImportResolutionRow.board_id
                        == board_id,
                        QualityAssessmentLegacyImportResolutionRow.epoch
                        == epoch,
                        QualityAssessmentLegacyImportResolutionRow.resolution
                        == "imported",
                    )
                )
            )
            .scalars()
            .all()
        )
        consistent = 0
        for row in resolutions:
            receipt = await session.get(
                QualityAssessmentReceiptRow,
                row.receipt_id,
            )
            event = await session.get(DomainEventRow, row.event_id)
            execution = (
                await session.execute(
                    select(DomainEventHandlerExecution).where(
                        DomainEventHandlerExecution.event_id
                        == row.event_id,
                        DomainEventHandlerExecution.handler_name
                        == _CONSOLIDATION_HANDLER_NAME,
                    )
                )
            ).scalar_one_or_none()
            outbox = await session.get(
                QualityAssessmentOutboxRow,
                row.outbox_id,
            )
            activity = await session.get(ActivityLog, row.history_id)
            if all((receipt, event, execution, outbox, activity)):
                consistent += 1
        return consistent

    async def verify_and_complete(
        self,
        expectation: LegacyImportCompletionExpectation,
    ) -> LegacyImportPostcondition:
        async with self._immediate_session() as session:
            existing = await session.get(
                QualityAssessmentLegacyImportCompletionRow,
                (expectation.board_id, expectation.epoch),
            )
            postcondition_digest = _completion_postcondition_digest(
                expectation
            )
            if existing is not None:
                completion_matches = all(
                    (
                        existing.plan_digest == expectation.plan_digest,
                        existing.candidate_digest
                        == expectation.candidate_digest,
                        existing.candidate_count
                        == expectation.candidate_count,
                        existing.processed_count
                        == expectation.checkpoint.processed_count,
                        existing.imported_count
                        == expectation.checkpoint.imported_count,
                        existing.native_wins_count
                        == expectation.checkpoint.native_wins_count,
                        existing.all_candidates_resolved is True,
                        existing.unique_identity_satisfied is True,
                        existing.checkpoint_consistent is True,
                        existing.zero_orphans is True,
                        existing.audit_bundles_consistent is True,
                        existing.epoch_closed is True,
                        existing.postcondition_digest
                        == postcondition_digest,
                    )
                )
                if not completion_matches:
                    raise LegacyImportPostconditionConflict(
                        "legacy_import_completion_conflict"
                    )
                # The immutable completion row is the durable proof that the
                # physical bundle was complete when the epoch closed.  A
                # governed subject purge may subsequently erase that bundle
                # while intentionally preserving the epoch ledger.  Requiring
                # the erased receipt/event/outbox/history again would reopen
                # the one-shot import and brick every later bootstrap.
                return LegacyImportPostcondition(
                    expectation=expectation,
                    all_candidates_resolved=True,
                    unique_identity_satisfied=True,
                    checkpoint_consistent=True,
                    zero_orphans=True,
                    audit_bundles_consistent=True,
                    epoch_closed=True,
                    completed_at=_aware(
                        existing.completed_at,
                        existing.completed_at,
                    ),
                )
            checkpoint_row = await session.get(
                QualityAssessmentLegacyImportCheckpointRow,
                (expectation.board_id, expectation.epoch),
            )
            candidate_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(
                            QualityAssessmentLegacyImportCandidateRow
                        )
                        .where(
                            QualityAssessmentLegacyImportCandidateRow.board_id
                            == expectation.board_id,
                            QualityAssessmentLegacyImportCandidateRow.epoch
                            == expectation.epoch,
                        )
                    )
                ).scalar_one()
            )
            resolution_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(
                            QualityAssessmentLegacyImportResolutionRow
                        )
                        .where(
                            QualityAssessmentLegacyImportResolutionRow.board_id
                            == expectation.board_id,
                            QualityAssessmentLegacyImportResolutionRow.epoch
                            == expectation.epoch,
                        )
                    )
                ).scalar_one()
            )
            imported_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(
                            QualityAssessmentLegacyImportResolutionRow
                        )
                        .where(
                            QualityAssessmentLegacyImportResolutionRow.board_id
                            == expectation.board_id,
                            QualityAssessmentLegacyImportResolutionRow.epoch
                            == expectation.epoch,
                            QualityAssessmentLegacyImportResolutionRow.resolution
                            == "imported",
                        )
                    )
                ).scalar_one()
            )
            checkpoint_consistent = (
                checkpoint_row is not None
                and _checkpoint_from_row(checkpoint_row)
                == expectation.checkpoint
            )
            all_resolved = (
                candidate_count
                == resolution_count
                == expectation.candidate_count
            )
            unique_identity = candidate_count == expectation.candidate_count
            audit_consistent = (
                await self._audit_bundle_count(
                    session,
                    board_id=expectation.board_id,
                    epoch=expectation.epoch,
                )
                == imported_count
            )
            native_resolution_rows = list(
                (
                    await session.execute(
                        select(
                            QualityAssessmentLegacyImportResolutionRow
                        ).where(
                            QualityAssessmentLegacyImportResolutionRow.board_id
                            == expectation.board_id,
                            QualityAssessmentLegacyImportResolutionRow.epoch
                            == expectation.epoch,
                            QualityAssessmentLegacyImportResolutionRow.resolution
                            == "native_wins",
                        )
                    )
                )
                .scalars()
                .all()
            )
            native_receipts_present = True
            for resolution_row in native_resolution_rows:
                if (
                    await session.get(
                        QualityAssessmentReceiptRow,
                        resolution_row.receipt_id,
                    )
                    is None
                ):
                    native_receipts_present = False
                    break
            zero_orphans = audit_consistent and native_receipts_present
            completed_at = datetime.now(timezone.utc)
            postcondition = LegacyImportPostcondition(
                expectation=expectation,
                all_candidates_resolved=all_resolved,
                unique_identity_satisfied=unique_identity,
                checkpoint_consistent=checkpoint_consistent,
                zero_orphans=zero_orphans,
                audit_bundles_consistent=audit_consistent,
                epoch_closed=all(
                    (
                        all_resolved,
                        unique_identity,
                        checkpoint_consistent,
                        zero_orphans,
                        audit_consistent,
                    )
                ),
                completed_at=completed_at,
            )
            if not postcondition.satisfied:
                raise LegacyImportPostconditionConflict(
                    "legacy_import_physical_postcondition_failed"
                )
            session.add(
                QualityAssessmentLegacyImportCompletionRow(
                    board_id=expectation.board_id,
                    epoch=expectation.epoch,
                    plan_digest=expectation.plan_digest,
                    candidate_digest=expectation.candidate_digest,
                    candidate_count=expectation.candidate_count,
                    processed_count=expectation.checkpoint.processed_count,
                    imported_count=expectation.checkpoint.imported_count,
                    native_wins_count=(
                        expectation.checkpoint.native_wins_count
                    ),
                    all_candidates_resolved=True,
                    unique_identity_satisfied=True,
                    checkpoint_consistent=True,
                    zero_orphans=True,
                    audit_bundles_consistent=True,
                    epoch_closed=True,
                    postcondition_digest=postcondition_digest,
                    completed_at=completed_at,
                )
            )
            await session.flush()
            return postcondition


__all__ = ["CommunitySqlAlchemyQualityAssessmentLegacyImport"]
