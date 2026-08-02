"""Community transaction-bound hook for automatic A1a requirement lint."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    QualityAssessmentHeadRow,
    QualityAssessmentReceiptRow,
    SpecQAItem,
)
from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
    CommunitySqlAlchemyQualityAssessment,
)
from okto_pulse.core.domain.quality_assessment import (
    AssessmentKind,
    AssessmentSubjectType,
)
from okto_pulse.core.domain.quality_canonicalization import (
    clarification_digest_v1,
    semantic_content_digest_v1,
)
from okto_pulse.core.domain.requirement_lint import RequirementLocale
from okto_pulse.core.ports.requirement_lint import (
    RequirementLintWriteCommand,
    RequirementLintWriteResult,
    RequirementLintWriterHook,
)
from okto_pulse.core.services.requirement_lint_assessment import (
    REQUIREMENT_LINT_CHANNEL_PREFIX,
    RequirementLintAssessmentInput,
    build_requirement_lint_assessment_bundle,
    commit_requirement_lint_assessment,
    requirement_lint_authority_snapshot_v1,
    requirement_lint_natural_idempotency_key,
    requirement_lint_normative_digests_v1,
    resolve_lint_language_profile,
)
from okto_pulse.core.services.ambiguity_assessment import (
    ambiguity_qa_payload,
)


async def _board_lint_locales(
    session: AsyncSession,
    board_id: str,
) -> tuple[RequirementLocale, ...]:
    """Resolve BoardSettings.lint_languages to the analyzer profile.

    Delegates the code->locale mapping to the core resolver shared with the
    read-side currentness evaluator — the two sides MUST resolve identically
    or every profiled receipt is born stale (policy_changed).
    """

    settings = (
        await session.execute(
            select(Board.settings).where(Board.id == board_id)
        )
    ).scalar_one_or_none()
    return resolve_lint_language_profile(
        settings if isinstance(settings, dict) else None
    )


def _digests_resolver_for_profile(
    default_locales: tuple[RequirementLocale, ...],
):
    """Rederive normative digests under the SAME profile as the bundle."""

    def _resolve(_session: AsyncSession, bundle: Any):
        digests = bundle.receipt.digests
        return requirement_lint_normative_digests_v1(
            content_digest=digests.content_digest,
            clarification_digest=digests.clarification_digest,
            default_locale=RequirementLocale.UNKNOWN,
            default_locales=default_locales,
        )

    return _resolve


def _authority_from_bundle(_session: AsyncSession, bundle: Any):
    receipt = bundle.receipt
    subject = receipt.subject
    return requirement_lint_authority_snapshot_v1(
        board_id=subject.board_id,
        spec_id=subject.subject_id,
        spec_version=subject.subject_version,
        actor_id=receipt.created_by,
        channel=receipt.channel,
    )


class CommunityRequirementLintWriterHook(RequirementLintWriterHook):
    """Stage A1a through the caller-owned SQLAlchemy transaction.

    The hook deliberately exposes no ``commit`` or ``rollback`` operation.
    Every semantic Spec writer passes its live ``AsyncSession`` as context, so
    a failure in analysis, CAS, Q&A, history, event or outbox aborts the same
    unit of work as the Spec mutation.
    """

    async def stage_requirement_lint(
        self,
        context: object,
        command: RequirementLintWriteCommand,
    ) -> RequirementLintWriteResult:
        if not isinstance(context, AsyncSession):
            raise TypeError("requirement_lint_context_must_be_async_session")
        if not isinstance(command, RequirementLintWriteCommand):
            raise TypeError("requirement_lint_command_invalid")

        qa_rows = list(
            (
                await context.execute(
                    select(SpecQAItem)
                    .where(SpecQAItem.spec_id == command.spec_id)
                    .order_by(SpecQAItem.id)
                )
            )
            .scalars()
            .all()
        )
        qa_items = tuple(ambiguity_qa_payload(qa_rows))
        default_locales = await _board_lint_locales(
            context,
            command.board_id,
        )
        normative_digests = requirement_lint_normative_digests_v1(
            content_digest=semantic_content_digest_v1(
                AssessmentSubjectType.SPEC,
                command.spec_payload,
            ),
            clarification_digest=clarification_digest_v1(qa_items),
            default_locale=RequirementLocale.UNKNOWN,
            default_locales=default_locales,
        )
        natural_key = requirement_lint_natural_idempotency_key(
            command,
            input_digest=normative_digests.input_digest or "",
        )
        prior = (
            await context.execute(
                select(QualityAssessmentReceiptRow).where(
                    QualityAssessmentReceiptRow.board_id == command.board_id,
                    QualityAssessmentReceiptRow.idempotency_key == natural_key,
                )
            )
        ).scalar_one_or_none()
        head = (
            await context.execute(
                select(QualityAssessmentHeadRow).where(
                    QualityAssessmentHeadRow.board_id == command.board_id,
                    QualityAssessmentHeadRow.subject_type
                    == AssessmentSubjectType.SPEC.value,
                    QualityAssessmentHeadRow.subject_id == command.spec_id,
                    QualityAssessmentHeadRow.assessment_kind
                    == AssessmentKind.REQUIREMENT_LINT.value,
                )
            )
        ).scalar_one_or_none()
        if prior is not None:
            # Reconstruct the original command fingerprint. The persisted
            # receipt stores the resulting revision and predecessor, while the
            # request fingerprint binds the pre-CAS revision. Generated IDs and
            # timestamps are deliberately irrelevant to natural replay.
            current_head_revision = int(prior.head_revision) - 1
            current_head_receipt_id = prior.predecessor_receipt_id
        else:
            current_head_revision = 0 if head is None else int(head.revision)
            current_head_receipt_id = None if head is None else head.receipt_id
        channel = f"{REQUIREMENT_LINT_CHANNEL_PREFIX}:{command.writer.value}"
        assessment_input = RequirementLintAssessmentInput(
            command=command,
            authority=requirement_lint_authority_snapshot_v1(
                board_id=command.board_id,
                spec_id=command.spec_id,
                spec_version=command.spec_version,
                actor_id=command.actor_id,
                channel=channel,
            ),
            qa_items=qa_items,
            default_locale=RequirementLocale.UNKNOWN,
            default_locales=default_locales,
            current_head_revision=current_head_revision,
            current_head_receipt_id=current_head_receipt_id,
        )
        bundle = build_requirement_lint_assessment_bundle(assessment_input)
        persistence = CommunitySqlAlchemyQualityAssessment(
            context,
            authority_resolver=_authority_from_bundle,
            input_digest_resolver=_digests_resolver_for_profile(
                default_locales
            ),
        )
        return await commit_requirement_lint_assessment(
            bundle,
            persistence=persistence,
        )


__all__ = ["CommunityRequirementLintWriterHook"]
