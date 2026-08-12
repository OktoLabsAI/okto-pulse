"""Lifecycle-edition validation-cycle read model for Community."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    ChecklistExecutionHeadRow,
    ChecklistItemResultRow,
    ChecklistReceiptRow,
    ChecklistValidationBindingSnapshotRow,
    Ideation,
    QualityAssessmentHeadRow,
    QualityAssessmentReceiptRow,
    Refinement,
    SemanticGuidelineAssessmentReceiptRow,
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
    if policy_status is not None and policy_status not in {"passed", "off"}:
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

        policy_by_spec: dict[
            str,
            dict[tuple[str, int], SemanticGuidelineAssessmentReceiptRow],
        ] = {}
        policy_rows: list[SemanticGuidelineAssessmentReceiptRow] = []
        if policy_spec_ids:
            policy_rows = list(
                (
                    (
                        await session.execute(
                            select(SemanticGuidelineAssessmentReceiptRow)
                            .where(
                                SemanticGuidelineAssessmentReceiptRow.subject_type
                                == "spec",
                                SemanticGuidelineAssessmentReceiptRow.subject_id.in_(
                                    policy_spec_ids
                                ),
                                SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                            )
                            .order_by(
                                SemanticGuidelineAssessmentReceiptRow.assessed_at.desc()
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        for row in policy_rows:
            subject = by_id.get(str(row.subject_id))
            if subject is None or row.validation_edition != int(subject.edition):
                continue
            policy_by_spec.setdefault(str(row.subject_id), {}).setdefault(
                (str(row.binding_id), int(row.binding_revision)),
                row,
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
                scope = scopes.get(spec_id)
                active_scope = []
                if scope is not None and isinstance(scope.scope_json, list):
                    active_scope = [
                        item
                        for item in scope.scope_json
                        if isinstance(item, dict)
                        and item.get("state", "active") != "unlinked"
                    ]
                expected_bindings = {
                    (
                        str(item.get("binding_id")),
                        int(item.get("binding_revision", 0)),
                    )
                    for item in active_scope
                }
                latest_by_binding = policy_by_spec.get(spec_id, {})
                completed = expected_bindings.intersection(latest_by_binding)
                failed_policies = sum(
                    1
                    for identity in completed
                    if latest_by_binding[identity].state != "passed"
                )
                if not expected_bindings:
                    policy_status, policy_summary = "off", "No policies required"
                elif not completed:
                    policy_status, policy_summary = "not_started", "Not started"
                elif len(completed) < len(expected_bindings):
                    policy_status = "in_progress"
                    policy_summary = (
                        f"{len(completed)} of {len(expected_bindings)} completed"
                    )
                elif failed_policies:
                    policy_status = "needs_attention"
                    policy_summary = f"{failed_policies} failed"
                else:
                    policy_status, policy_summary = "passed", "All policies passed"
                checks.append(
                    ValidationCycleCheckSummary(
                        ValidationCycleResultType.POLICY_COMPLIANCE,
                        policy_status,
                        policy_summary,
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
            active_scope = []
            if scope is not None and isinstance(scope.scope_json, list):
                active_scope = [
                    item
                    for item in scope.scope_json
                    if isinstance(item, dict)
                    and item.get("state", "active") != "unlinked"
                ]
            expected_bindings = {
                (
                    str(item.get("binding_id")),
                    int(item.get("binding_revision", 0)),
                )
                for item in active_scope
            }
            policy_rows = list(
                (
                    (
                        await session.execute(
                            select(SemanticGuidelineAssessmentReceiptRow)
                            .where(
                                SemanticGuidelineAssessmentReceiptRow.board_id
                                == subject.board_id,
                                SemanticGuidelineAssessmentReceiptRow.subject_type
                                == "spec",
                                SemanticGuidelineAssessmentReceiptRow.subject_id
                                == subject.id,
                                SemanticGuidelineAssessmentReceiptRow.validation_edition
                                == edition,
                                SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                            )
                            .order_by(
                                SemanticGuidelineAssessmentReceiptRow.assessed_at.desc()
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            latest_by_binding: dict[
                tuple[str, int], SemanticGuidelineAssessmentReceiptRow
            ] = {}
            for row in policy_rows:
                latest_by_binding.setdefault(
                    (row.binding_id, row.binding_revision), row
                )
            completed = expected_bindings.intersection(latest_by_binding)
            failed = sum(
                1
                for identity in completed
                if latest_by_binding[identity].state != "passed"
            )
            if not expected_bindings:
                policy_status, policy_summary = "off", "No policies required"
            elif not completed:
                policy_status, policy_summary = "not_started", "Not started"
            elif len(completed) < len(expected_bindings):
                policy_status = "in_progress"
                policy_summary = (
                    f"{len(completed)} of {len(expected_bindings)} completed"
                )
            elif failed:
                policy_status, policy_summary = "needs_attention", f"{failed} failed"
            else:
                policy_status, policy_summary = "passed", "All policies passed"
            checks.append(
                ValidationCycleCheckSummary(
                    ValidationCycleResultType.POLICY_COMPLIANCE,
                    policy_status,
                    policy_summary,
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
