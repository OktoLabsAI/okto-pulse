"""Permission-aware, batched Quality summaries for parent page envelopes.

The paginated Ideation, Refinement and Spec surfaces expose only one compact
summary per current assessment kind.  This adapter keeps the projection out of
the legacy array routes, resolves the dedicated read leaf before touching
Quality rows, and uses one page-bounded aggregate statement for both the
current head and the prior-result count.  Findings and historical bodies remain
lazy and are never loaded by a parent-page projection.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from time import perf_counter
from typing import Any, Literal, TypeAlias

from sqlalchemy import and_, func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Ideation,
    QualityAssessmentHeadRow,
    QualityAssessmentReceiptRow,
    Refinement,
    Spec,
)
from okto_pulse.core.domain.permissions import check_permission
from okto_pulse.core.services.ska_observability import (
    observe_ska_projection_queries,
)

QualitySubjectType: TypeAlias = Literal["ideation", "refinement", "spec"]
QualitySummary: TypeAlias = dict[str, object]
QualitySummaryMap: TypeAlias = dict[str, dict[str, QualitySummary]]


@dataclass(frozen=True, slots=True)
class _SubjectBinding:
    model: type


_SUBJECT_BINDINGS: dict[str, _SubjectBinding] = {
    "ideation": _SubjectBinding(
        model=Ideation,
    ),
    "refinement": _SubjectBinding(
        model=Refinement,
    ),
    "spec": _SubjectBinding(
        model=Spec,
    ),
}

_SUBJECT_ASSESSMENT_KINDS: dict[QualitySubjectType, tuple[str, ...]] = {
    "ideation": ("ambiguity",),
    "refinement": ("ambiguity",),
    "spec": ("requirement_lint", "spec_validation"),
}


def quality_summary_field(
    subject_id: str,
    summaries: QualitySummaryMap | None,
) -> dict[str, object]:
    """Return constructor kwargs while preserving denial-by-omission."""

    if summaries is None:
        return {}
    return {"quality_summaries": summaries.get(subject_id, {})}


async def load_quality_summaries_for_page(
    *,
    uow: Any,
    user_id: str,
    board_id: str,
    subject_type: QualitySubjectType,
    subject_ids: tuple[str, ...],
) -> QualitySummaryMap | None:
    """Resolve the read leaf and batch-project current Quality heads.

    ``None`` means the actor lacks the subject-specific Quality read leaf and
    callers must omit ``quality_summaries`` entirely.  An empty mapping means
    the actor may read Quality but the page has no current heads.
    """

    started = perf_counter()
    query_count = 0
    try:
        ordered_ids = tuple(
            dict.fromkeys(item for item in subject_ids if item)
        )
        if not ordered_ids:
            summaries: QualitySummaryMap | None = {}
        else:
            permission_set = await uow.services.resolve_user_permissions(
                user_id,
                board_id,
            )
            if check_permission(
                permission_set,
                f"{subject_type}.quality.read",
            ):
                summaries = None
            else:
                query_count = 1
                summaries = await _load_quality_summaries(
                    session=uow.services.cards.db,
                    board_id=board_id,
                    subject_type=subject_type,
                    subject_ids=ordered_ids,
                )
    except Exception:
        observe_ska_projection_queries(
            surface="parent_summary",
            subject_type=subject_type,
            query_count=query_count,
            duration_ms=(perf_counter() - started) * 1000,
            payload_bytes=0,
            outcome="error",
        )
        raise
    payload_bytes = (
        0
        if summaries is None
        else len(
            json.dumps(
                summaries,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    )
    observe_ska_projection_queries(
        surface="parent_summary",
        subject_type=subject_type,
        query_count=query_count,
        duration_ms=(perf_counter() - started) * 1000,
        payload_bytes=payload_bytes,
    )
    return summaries


async def _load_quality_summaries(
    *,
    session: Any,
    board_id: str,
    subject_type: QualitySubjectType,
    subject_ids: tuple[str, ...],
) -> QualitySummaryMap:
    binding = _SUBJECT_BINDINGS[subject_type]
    columns: list[Any] = [
        binding.model.id.label("subject_id"),
        binding.model.edition.label("edition"),
    ]
    for assessment_kind in _SUBJECT_ASSESSMENT_KINDS[subject_type]:
        receipt_scope = (
            QualityAssessmentReceiptRow.board_id == board_id,
            QualityAssessmentReceiptRow.subject_type == subject_type,
            QualityAssessmentReceiptRow.subject_id == binding.model.id,
            QualityAssessmentReceiptRow.assessment_kind == assessment_kind,
        )
        total_count = (
            select(func.count(QualityAssessmentReceiptRow.id))
            .where(*receipt_scope)
            .correlate(binding.model)
            .scalar_subquery()
        )
        head_join = and_(
            QualityAssessmentHeadRow.board_id
            == QualityAssessmentReceiptRow.board_id,
            QualityAssessmentHeadRow.subject_type
            == QualityAssessmentReceiptRow.subject_type,
            QualityAssessmentHeadRow.subject_id
            == QualityAssessmentReceiptRow.subject_id,
            QualityAssessmentHeadRow.assessment_kind
            == QualityAssessmentReceiptRow.assessment_kind,
            QualityAssessmentHeadRow.receipt_id
            == QualityAssessmentReceiptRow.id,
        )

        def current_value(column: Any) -> Any:
            return (
                select(column)
                .select_from(QualityAssessmentReceiptRow)
                .join(QualityAssessmentHeadRow, head_join)
                .where(
                    *receipt_scope,
                    QualityAssessmentReceiptRow.subject_edition
                    == binding.model.edition,
                )
                .correlate(binding.model)
                .limit(1)
                .scalar_subquery()
            )

        columns.extend(
            (
                total_count.label(f"{assessment_kind}_total_count"),
                current_value(QualityAssessmentReceiptRow.score).label(
                    f"{assessment_kind}_current_score"
                ),
                current_value(QualityAssessmentReceiptRow.scale_kind).label(
                    f"{assessment_kind}_current_scale_kind"
                ),
                current_value(
                    QualityAssessmentReceiptRow.scale_minimum
                ).label(f"{assessment_kind}_current_scale_minimum"),
                current_value(
                    QualityAssessmentReceiptRow.scale_maximum
                ).label(f"{assessment_kind}_current_scale_maximum"),
                current_value(
                    QualityAssessmentReceiptRow.scale_direction
                ).label(f"{assessment_kind}_current_scale_direction"),
            )
        )
    rows = (
        await session.execute(
            select(*columns)
            .where(
                binding.model.id.in_(subject_ids),
                binding.model.board_id == board_id,
            )
        )
    ).all()

    summaries: QualitySummaryMap = {}
    for row in rows:
        subject_id = str(row.subject_id)
        current_edition = int(row.edition)
        subject_summaries = summaries.setdefault(subject_id, {})
        for assessment_kind in _SUBJECT_ASSESSMENT_KINDS[subject_type]:
            total_count = int(
                getattr(row, f"{assessment_kind}_total_count")
            )
            current_score = getattr(
                row,
                f"{assessment_kind}_current_score",
            )
            if current_score is None:
                subject_summaries[assessment_kind] = {
                    "edition": current_edition,
                    "state": "not_started",
                    "previous_count": total_count,
                    "current_result": None,
                }
                continue
            subject_summaries[assessment_kind] = {
                "edition": current_edition,
                "state": "current",
                "previous_count": max(total_count - 1, 0),
                "current_result": {
                    "score": int(current_score),
                    "scale": {
                        "kind": str(
                            getattr(
                                row,
                                f"{assessment_kind}_current_scale_kind",
                            )
                        ),
                        "min": int(
                            getattr(
                                row,
                                f"{assessment_kind}_current_scale_minimum",
                            )
                        ),
                        "max": int(
                            getattr(
                                row,
                                f"{assessment_kind}_current_scale_maximum",
                            )
                        ),
                        "direction": str(
                            getattr(
                                row,
                                f"{assessment_kind}_current_scale_direction",
                            )
                        ),
                    },
                },
            }
    return summaries


__all__ = [
    "QualitySummaryMap",
    "load_quality_summaries_for_page",
    "quality_summary_field",
]
