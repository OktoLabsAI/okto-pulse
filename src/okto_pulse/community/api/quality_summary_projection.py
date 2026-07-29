"""Permission-aware, batched Quality summaries for parent page envelopes.

The paginated Ideation, Refinement and Spec surfaces expose only one compact
summary per current assessment kind.  This adapter keeps the projection out of
the legacy array routes, resolves the dedicated read leaf before touching
Quality rows, and uses two page-bounded statements (heads/receipts/subjects +
Q&A) instead of one query per parent.  The nested Ideation→Refinements route
therefore moves from its six-statement historical ceiling to seven statements
for direct permissions (at most eight when preset lineage adds one read), still
constant for page sizes 1..200.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from time import perf_counter
from typing import Any, Literal, TypeAlias

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Ideation,
    IdeationQAItem,
    QualityAssessmentHeadRow,
    QualityAssessmentReceiptRow,
    Refinement,
    RefinementQAItem,
    Spec,
    SpecQAItem,
)
from okto_pulse.core.domain.permissions import check_permission
from okto_pulse.core.domain.quality_assessment import AssessmentDigestSet
from okto_pulse.core.services.quality_projection_currentness import (
    QualityProjectionCurrentnessError,
    evaluate_quality_projection_currentness,
)
from okto_pulse.core.services.ska_observability import (
    observe_ska_projection_queries,
)

QualitySubjectType: TypeAlias = Literal["ideation", "refinement", "spec"]
QualitySummary: TypeAlias = dict[str, object]
QualitySummaryMap: TypeAlias = dict[str, dict[str, QualitySummary]]


@dataclass(frozen=True, slots=True)
class _SubjectBinding:
    model: type
    qa_model: type
    qa_subject_fk: str


_SUBJECT_BINDINGS: dict[str, _SubjectBinding] = {
    "ideation": _SubjectBinding(
        model=Ideation,
        qa_model=IdeationQAItem,
        qa_subject_fk="ideation_id",
    ),
    "refinement": _SubjectBinding(
        model=Refinement,
        qa_model=RefinementQAItem,
        qa_subject_fk="refinement_id",
    ),
    "spec": _SubjectBinding(
        model=Spec,
        qa_model=SpecQAItem,
        qa_subject_fk="spec_id",
    ),
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
                query_count = 2
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
    joined_rows = (
        await session.execute(
            select(
                QualityAssessmentReceiptRow,
                QualityAssessmentHeadRow,
                binding.model,
            )
            .join(
                QualityAssessmentHeadRow,
                QualityAssessmentHeadRow.receipt_id
                == QualityAssessmentReceiptRow.id,
            )
            .join(
                binding.model,
                binding.model.id == QualityAssessmentHeadRow.subject_id,
            )
            .where(
                QualityAssessmentHeadRow.board_id == board_id,
                QualityAssessmentHeadRow.subject_type == subject_type,
                QualityAssessmentHeadRow.subject_id.in_(subject_ids),
                QualityAssessmentReceiptRow.board_id == board_id,
                binding.model.board_id == board_id,
            )
            .order_by(
                QualityAssessmentHeadRow.subject_id,
                QualityAssessmentHeadRow.assessment_kind,
            )
        )
    ).all()
    qa_rows = (
        (
            await session.execute(
                select(binding.qa_model)
                .where(
                    getattr(binding.qa_model, binding.qa_subject_fk).in_(
                        subject_ids
                    )
                )
                .order_by(
                    getattr(binding.qa_model, binding.qa_subject_fk),
                    binding.qa_model.id,
                )
            )
        )
        .scalars()
        .all()
    )
    qa_by_subject: dict[str, list[object]] = defaultdict(list)
    for qa_row in qa_rows:
        qa_by_subject[
            str(getattr(qa_row, binding.qa_subject_fk))
        ].append(qa_row)

    summaries: QualitySummaryMap = {}
    for receipt, head, subject in joined_rows:
        currentness = "stale"
        try:
            freshness = evaluate_quality_projection_currentness(
                board_id=board_id,
                subject_type=subject_type,
                subject_id=receipt.subject_id,
                assessed_subject_version=receipt.subject_version,
                assessed_digests=AssessmentDigestSet(
                    content_digest=receipt.content_digest,
                    clarification_digest=receipt.clarification_digest,
                    ruleset_digest=receipt.ruleset_digest,
                    taxonomy_digest=receipt.taxonomy_digest,
                    policy_digest=receipt.policy_digest,
                ),
                assessment_kind=receipt.assessment_kind,
                origin=receipt.origin,
                source=receipt.source,
                current_subject=subject,
                qa_items=qa_by_subject.get(receipt.subject_id, ()),
                board_settings=None,
            )
            currentness = "current" if freshness.current else "stale"
        except QualityProjectionCurrentnessError:
            # Unsupported persisted identities are never presented as current.
            currentness = "stale"

        summaries.setdefault(receipt.subject_id, {})[
            receipt.assessment_kind
        ] = {
            "receipt_id": receipt.id,
            "subject_version": receipt.subject_version,
            "currentness": currentness,
            "score": receipt.score,
            "scale": {
                "kind": receipt.scale_kind,
                "min": receipt.scale_minimum,
                "max": receipt.scale_maximum,
                "direction": receipt.scale_direction,
            },
            "head_revision": head.revision,
        }
    return summaries


__all__ = [
    "QualitySummaryMap",
    "load_quality_summaries_for_page",
    "quality_summary_field",
]
