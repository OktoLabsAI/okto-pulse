"""SQLAlchemy source-of-truth adapter for SK-A quality assessments.

The adapter is deliberately transaction-bound.  It flushes staged work so
database constraints and CAS predicates are observed, but transaction
ownership remains with the caller.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import TypeAlias

from sqlalchemy import and_, func, not_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Agent,
    Board,
    BoardShare,
    DomainEventHandlerExecution,
    DomainEventRow,
    Ideation,
    IdeationHistory,
    IdeationQAItem,
    QualityAssessmentHeadRow,
    QualityAssessmentOutboxRow,
    QualityAssessmentReceiptRow,
    QualityFindingQaLinkRow,
    QualityFindingRow,
    QualityProposedQuestionRow,
    Refinement,
    RefinementHistory,
    RefinementQAItem,
    Spec,
    SpecHistory,
    SpecQAItem,
)
from okto_pulse.core.domain.quality_assessment import (
    AssessmentAuthoritySnapshot,
    AssessmentCommitResult,
    AssessmentDigestSet,
    AssessmentKind,
    AssessmentOrigin,
    AssessmentOutcome,
    AssessmentPreflight,
    AssessmentPreflightRequest,
    AssessmentReceipt,
    AssessmentReceiptDetail,
    AssessmentReceiptState,
    AssessmentReceiptView,
    AssessmentScale,
    AssessmentScaleKind,
    AssessmentSource,
    AssessmentSubmission,
    AssessmentSubjectHead,
    AssessmentSubjectRef,
    AssessmentSubjectType,
    AssessmentVersionSet,
    AssessmentWriteBundle,
    EvidenceRef,
    FindingAnchor,
    FindingAnchorType,
    FindingLifecycle,
    FindingQaLink,
    FindingSeverity,
    ProposedQuestion,
    QualityFinding,
    QualityPage,
    ScoreDirection,
    project_assessment_receipt_view,
)
from okto_pulse.core.domain.quality_canonicalization import (
    SEMANTIC_FIELD_MANIFEST_V1,
    clarification_digest_v1,
    semantic_content_digest_v1,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID, RealmScope
from okto_pulse.core.ports.quality_assessment import (
    AssessmentAuthorityConflict,
    AssessmentCurrentnessInput,
    AssessmentHeadRevisionConflict,
    AssessmentIdempotencyConflict,
    AssessmentInputDigestConflict,
    AssessmentListQuery,
    AssessmentReadAccessDenied,
    AssessmentReceiptNotFound,
    AssessmentSubjectNotFound,
    AssessmentSubjectLifecycleConflict,
    AssessmentSubjectStatusConflict,
    AssessmentSubjectVersionConflict,
    FindingListQuery,
    QualityAssessmentPersistenceError,
    QualityAssessmentReadContext,
    QualityGateInput,
)
from okto_pulse.core.services.ambiguity_assessment import (
    AMBIGUITY_SCALE,
    AMBIGUITY_TAXONOMY_CATEGORY_ID_SET,
    ambiguity_anchor_catalog,
    ambiguity_authority_snapshot_v1,
    ambiguity_digest_set,
    ambiguity_versions_v1,
    resolve_ambiguity_gate_configuration,
)
from okto_pulse.core.services.quality_projection_currentness import (
    current_quality_projection_digests,
)
from okto_pulse.core.services.ska_observability import (
    observe_ska_projection_queries,
)

AuthorityDigestResolver: TypeAlias = Callable[
    [AsyncSession, AssessmentWriteBundle],
    AssessmentAuthoritySnapshot | Awaitable[AssessmentAuthoritySnapshot | None] | None,
]
InputDigestResolver: TypeAlias = Callable[
    [AsyncSession, AssessmentWriteBundle],
    AssessmentDigestSet | Awaitable[AssessmentDigestSet | None] | None,
]

_CONSOLIDATION_HANDLER_NAME = "ConsolidationEnqueuer"


@dataclass(frozen=True, slots=True)
class _SubjectBinding:
    model: type
    qa_model: type
    subject_fk: str
    history_model: type
    history_fk: str


@dataclass(frozen=True, slots=True)
class _QualitySubjectContext:
    board: Board
    subject: object
    qa_items: tuple[object, ...]
    head: QualityAssessmentHeadRow | None


_SUBJECT_BINDINGS: dict[AssessmentSubjectType, _SubjectBinding] = {
    AssessmentSubjectType.IDEATION: _SubjectBinding(
        model=Ideation,
        qa_model=IdeationQAItem,
        subject_fk="ideation_id",
        history_model=IdeationHistory,
        history_fk="ideation_id",
    ),
    AssessmentSubjectType.REFINEMENT: _SubjectBinding(
        model=Refinement,
        qa_model=RefinementQAItem,
        subject_fk="refinement_id",
        history_model=RefinementHistory,
        history_fk="refinement_id",
    ),
    AssessmentSubjectType.SPEC: _SubjectBinding(
        model=Spec,
        qa_model=SpecQAItem,
        subject_fk="spec_id",
        history_model=SpecHistory,
        history_fk="spec_id",
    ),
}


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _require_local_realm(realm_scope: RealmScope) -> None:
    if not isinstance(realm_scope, RealmScope) or realm_scope.realm_id != LOCAL_REALM_ID:
        raise QualityAssessmentPersistenceError(
            "quality_assessment_realm_scope_mismatch"
        )


async def _load_quality_subject_context(
    session: AsyncSession,
    *,
    board_id: str,
    subject_type: AssessmentSubjectType,
    subject_id: str,
    assessment_kind: AssessmentKind,
) -> _QualitySubjectContext | None:
    binding = _SUBJECT_BINDINGS[subject_type]
    row = (
        await session.execute(
            select(binding.model, Board)
            .join(Board, Board.id == binding.model.board_id)
            .where(
                binding.model.id == subject_id,
                binding.model.board_id == board_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    subject, board = row
    qa_items = tuple(
        (
            (
                await session.execute(
                    select(binding.qa_model)
                    .where(
                        getattr(binding.qa_model, binding.subject_fk) == subject_id
                    )
                    .order_by(binding.qa_model.id)
                )
            )
            .scalars()
            .all()
        )
    )
    head = (
        await session.execute(
            select(QualityAssessmentHeadRow).where(
                QualityAssessmentHeadRow.board_id == board_id,
                QualityAssessmentHeadRow.subject_type == subject_type.value,
                QualityAssessmentHeadRow.subject_id == subject_id,
                QualityAssessmentHeadRow.assessment_kind == assessment_kind.value,
            )
        )
    ).scalar_one_or_none()
    return _QualitySubjectContext(
        board=board,
        subject=subject,
        qa_items=qa_items,
        head=head,
    )


async def _ambiguity_authority_for(
    session: AsyncSession,
    *,
    actor_id: str,
    board_id: str,
    subject_type: AssessmentSubjectType,
    subject_id: str,
    subject_version: int,
    channel: str,
) -> AssessmentAuthoritySnapshot:
    # Import lazily to avoid coupling model module initialization to the broad
    # application-persistence catalog.
    from okto_pulse.core.domain.permissions import (
        SKA_PERMISSION_INTRODUCTION_V1,
    )

    permissions, access_level = await _quality_actor_permissions(
        session,
        actor_id=actor_id,
        board_id=board_id,
    )
    quality_leaf = f"{subject_type.value}.quality.assess"
    historical_leaf = SKA_PERMISSION_INTRODUCTION_V1.historical_authority_for(
        quality_leaf
    )
    domain_write = bool(
        access_level in {"owner", "editor", "admin", "agent"}
        and historical_leaf is not None
        and permissions is not None
        and permissions.has(historical_leaf)
    )
    return ambiguity_authority_snapshot_v1(
        actor_id=actor_id,
        board_id=board_id,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_version=subject_version,
        channel=channel,
        domain_write=domain_write,
        quality_assess=bool(
            permissions is not None
            and access_level in {"owner", "editor", "admin", "agent"}
            and permissions.has(quality_leaf)
        ),
        qa_ask=bool(
            permissions is not None
            and access_level in {"owner", "editor", "admin", "agent"}
            and permissions.has(f"{subject_type.value}.qa.ask")
        ),
    )


async def _quality_actor_permissions(
    session: AsyncSession,
    *,
    actor_id: str,
    board_id: str,
) -> tuple[object | None, str | None]:
    """Resolve one actor without confusing MCP agent IDs with REST owner IDs."""

    from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
        CommunitySqlAlchemyApplicationPersistence,
    )
    from okto_pulse.community.adapters.relational_application import (
        CommunityAgentAuthenticationGateway,
    )

    agent = await session.get(Agent, actor_id)
    if agent is not None:
        context = await CommunityAgentAuthenticationGateway(
            session
        ).resolve_agent_permission_context(actor_id, board_id=board_id)
        return (
            (None, None)
            if context is None
            else (context.permissions, "agent")
        )

    access = (
        await session.execute(
            select(Board.owner_id, BoardShare.permission)
            .outerjoin(
                BoardShare,
                and_(
                    BoardShare.board_id == Board.id,
                    BoardShare.user_id == actor_id,
                ),
            )
            .where(Board.id == board_id)
        )
    ).one_or_none()
    if access is None:
        return None, None
    owner_id, share_permission = access
    if owner_id == actor_id:
        access_level = "owner"
    elif share_permission in {"viewer", "editor", "admin"}:
        access_level = str(share_permission)
    else:
        return None, None
    permissions = (
        await CommunitySqlAlchemyApplicationPersistence().resolve_user_permissions(
            session,
            user_id=actor_id,
            board_id=board_id,
        )
    )
    return permissions, access_level


async def resolve_quality_assessment_authority(
    session: AsyncSession,
    bundle: AssessmentWriteBundle,
) -> AssessmentAuthoritySnapshot:
    """Rederive the native ambiguity authority fence inside the write UoW."""

    receipt = bundle.receipt
    if (
        receipt.assessment_kind is not AssessmentKind.AMBIGUITY
        or receipt.subject.subject_type
        not in {
            AssessmentSubjectType.IDEATION,
            AssessmentSubjectType.REFINEMENT,
        }
        or receipt.origin is not AssessmentOrigin.HUMAN_OR_AGENT
        or receipt.source is not AssessmentSource.NATIVE
    ):
        raise AssessmentAuthorityConflict(
            "assessment_authority_identity_unsupported"
        )
    return await _ambiguity_authority_for(
        session,
        actor_id=receipt.created_by,
        board_id=receipt.subject.board_id,
        subject_type=receipt.subject.subject_type,
        subject_id=receipt.subject.subject_id,
        subject_version=receipt.subject.subject_version,
        channel=receipt.channel,
    )


async def resolve_quality_assessment_input_digests(
    session: AsyncSession,
    bundle: AssessmentWriteBundle,
) -> AssessmentDigestSet:
    """Rederive the complete normative input fence inside the write UoW."""

    receipt = bundle.receipt
    subject = receipt.subject
    context = await _load_quality_subject_context(
        session,
        board_id=subject.board_id,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        assessment_kind=receipt.assessment_kind,
    )
    if context is None:
        raise AssessmentInputDigestConflict("assessment_subject_missing")
    try:
        return current_quality_projection_digests(
            subject_type=subject.subject_type,
            assessment_kind=receipt.assessment_kind,
            origin=receipt.origin,
            source=receipt.source,
            subject=context.subject,
            qa_items=context.qa_items,
            board_settings=context.board.settings,
        )
    except (TypeError, ValueError) as exc:
        raise AssessmentInputDigestConflict(
            "assessment_input_identity_unsupported"
        ) from exc


async def _resolved(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _receipt_from_row(row: QualityAssessmentReceiptRow) -> AssessmentReceipt:
    return AssessmentReceipt(
        id=row.id,
        subject=AssessmentSubjectRef(
            board_id=row.board_id,
            subject_type=AssessmentSubjectType(row.subject_type),
            subject_id=row.subject_id,
            subject_version=row.subject_version,
        ),
        assessment_kind=AssessmentKind(row.assessment_kind),
        origin=AssessmentOrigin(row.origin),
        source=AssessmentSource(row.source),
        channel=row.channel,
        outcome=AssessmentOutcome(row.outcome),
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
        run_identity_digest=row.run_identity_digest,
        authority_digest=row.authority_digest,
        idempotency_key=row.idempotency_key,
        request_digest=row.request_digest,
        created_by=row.created_by,
        created_at=row.created_at,
        predecessor_receipt_id=row.predecessor_receipt_id,
        contract_version=row.contract_version,
    )


def _finding_from_row(row: QualityFindingRow) -> QualityFinding:
    evidence_refs = tuple(
        EvidenceRef(
            source_type=item["source_type"],
            source_id=item["source_id"],
            source_version=item["source_version"],
            content_hash=item["content_hash"],
        )
        for item in (row.evidence_refs or [])
    )
    return QualityFinding(
        id=row.id,
        receipt_id=row.receipt_id,
        assessment_kind=AssessmentKind(row.assessment_kind),
        finding_key=row.finding_key,
        category_code=row.category_code,
        taxonomy_version=row.taxonomy_version,
        severity=FindingSeverity(row.severity),
        confidence=row.confidence,
        deterministic=bool(row.deterministic),
        blocking_eligible=bool(row.blocking_eligible),
        title=row.title,
        detail=row.detail,
        anchor=FindingAnchor(
            board_id=row.anchor_board_id,
            subject_type=AssessmentSubjectType(row.anchor_subject_type),
            subject_id=row.anchor_subject_id,
            subject_version=row.anchor_subject_version,
            input_digest=row.anchor_input_digest,
            anchor_type=FindingAnchorType(row.anchor_type),
            anchor_ref=row.anchor_ref,
            excerpt_hash=row.excerpt_hash,
        ),
        evidence_refs=evidence_refs,
        lifecycle=FindingLifecycle(row.lifecycle),
        created_at=row.created_at,
        remediation=row.remediation,
        rule_code=row.rule_code,
    )


def _question_from_row(row: QualityProposedQuestionRow) -> ProposedQuestion:
    return ProposedQuestion(
        qa_id=row.qa_id,
        receipt_id=row.receipt_id,
        client_key=row.client_key,
        question=row.question,
        question_type=row.question_type,
        choices=tuple(row.choices or ()),
        allow_free_text=bool(row.allow_free_text),
        category_code=row.category_code,
        created_at=row.created_at,
    )


def _head_from_row(row: QualityAssessmentHeadRow) -> AssessmentSubjectHead:
    return AssessmentSubjectHead(
        board_id=row.board_id,
        subject_type=AssessmentSubjectType(row.subject_type),
        subject_id=row.subject_id,
        assessment_kind=AssessmentKind(row.assessment_kind),
        receipt_id=row.receipt_id,
        revision=row.revision,
        updated_at=row.updated_at,
    )


def _receipt_replay_signature(receipt: AssessmentReceipt) -> tuple[object, ...]:
    """Return immutable command semantics, excluding server-issued identity."""

    return (
        receipt.subject,
        receipt.assessment_kind,
        receipt.origin,
        receipt.source,
        receipt.channel,
        receipt.outcome,
        receipt.scale,
        receipt.score,
        receipt.justification,
        receipt.digests,
        receipt.versions,
        receipt.run_identity_digest,
        receipt.authority_digest,
        receipt.idempotency_key,
        receipt.request_digest,
        receipt.created_by,
        receipt.predecessor_receipt_id,
        receipt.contract_version,
    )


def _finding_replay_signature(finding: QualityFinding) -> tuple[object, ...]:
    return (
        finding.assessment_kind,
        finding.finding_key,
        finding.category_code,
        finding.taxonomy_version,
        finding.severity,
        finding.confidence,
        finding.deterministic,
        finding.blocking_eligible,
        finding.title,
        finding.detail,
        finding.anchor,
        finding.evidence_refs,
        finding.lifecycle,
        finding.remediation,
        finding.rule_code,
    )


def _question_replay_signature(question: ProposedQuestion) -> tuple[object, ...]:
    return (
        question.client_key,
        question.question,
        question.question_type,
        question.choices,
        question.allow_free_text,
        question.category_code,
    )


def _semantic_links(detail: AssessmentReceiptDetail) -> set[tuple[str, str]]:
    finding_keys = {finding.id: finding.finding_key for finding in detail.findings}
    question_keys = {
        question.qa_id: question.client_key for question in detail.proposed_questions
    }
    return {
        (finding_keys[link.finding_id], question_keys[link.qa_id])
        for link in detail.finding_qa_links
    }


def _currentness_inputs_for(
    *,
    context: _QualitySubjectContext,
    subject_type: AssessmentSubjectType,
) -> tuple[AssessmentCurrentnessInput, ...]:
    identities: tuple[
        tuple[AssessmentKind, AssessmentOrigin, AssessmentSource], ...
    ]
    if subject_type in {
        AssessmentSubjectType.IDEATION,
        AssessmentSubjectType.REFINEMENT,
    }:
        identities = (
            (
                AssessmentKind.AMBIGUITY,
                AssessmentOrigin.HUMAN_OR_AGENT,
                AssessmentSource.NATIVE,
            ),
            (
                AssessmentKind.AMBIGUITY,
                AssessmentOrigin.LEGACY_IMPORT,
                AssessmentSource.LEGACY_MIGRATION,
            ),
        )
    else:
        identities = (
            (
                AssessmentKind.SPEC_VALIDATION,
                AssessmentOrigin.LEGACY_IMPORT,
                AssessmentSource.LEGACY_MIGRATION,
            ),
            (
                AssessmentKind.REQUIREMENT_LINT,
                AssessmentOrigin.SEMANTIC_WRITER,
                AssessmentSource.NATIVE,
            ),
        )
    return tuple(
        AssessmentCurrentnessInput(
            assessment_kind=assessment_kind,
            origin=origin,
            source=source,
            digests=current_quality_projection_digests(
                subject_type=subject_type,
                assessment_kind=assessment_kind,
                origin=origin,
                source=source,
                subject=context.subject,
                qa_items=context.qa_items,
                board_settings=context.board.settings,
            ),
        )
        for assessment_kind, origin, source in identities
    )


def _gate_inputs_for(
    *,
    context: _QualitySubjectContext,
    subject_type: AssessmentSubjectType,
) -> tuple[QualityGateInput, ...]:
    if subject_type not in {
        AssessmentSubjectType.IDEATION,
        AssessmentSubjectType.REFINEMENT,
    }:
        return ()
    configuration = resolve_ambiguity_gate_configuration(
        subject_type,
        context.board.settings,
    )
    return (
        QualityGateInput(
            assessment_kind=AssessmentKind.AMBIGUITY,
            applicable=True,
            enabled=configuration.required,
            threshold=configuration.maximum_score,
            skipped=bool(
                getattr(context.subject, "skip_ambiguity_gate", False)
            ),
        ),
    )


class CommunitySqlAlchemyQualityAssessmentPreflightReader:
    """Session-factory-backed preflight/read boundary for REST and MCP."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        if not callable(session_factory):
            raise TypeError("quality_assessment_session_factory_invalid")
        self._session_factory = session_factory

    async def resolve_subject_board_id(
        self,
        *,
        subject_type: AssessmentSubjectType,
        subject_id: str,
        realm_scope: RealmScope,
    ) -> str:
        """Resolve a literal REST path identity before actor board binding."""

        _require_local_realm(realm_scope)
        binding = _SUBJECT_BINDINGS[subject_type]
        async with self._session_factory() as session:
            board_id = (
                await session.execute(
                    select(binding.model.board_id).where(
                        binding.model.id == subject_id
                    )
                )
            ).scalar_one_or_none()
        if board_id is None:
            raise AssessmentSubjectNotFound()
        return str(board_id)

    async def lookup_assessment_replay(
        self,
        *,
        board_id: str,
        idempotency_key: str,
        actor_id: str,
        realm_scope: RealmScope,
    ) -> AssessmentCommitResult | None:
        _require_local_realm(realm_scope)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(QualityAssessmentReceiptRow).where(
                        QualityAssessmentReceiptRow.board_id == board_id,
                        QualityAssessmentReceiptRow.idempotency_key
                        == idempotency_key,
                        # A colliding key owned by another actor is handled by
                        # the write CAS; never expose its immutable receipt here.
                        QualityAssessmentReceiptRow.created_by == actor_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            qa_id_map = tuple(
                (question.client_key, question.qa_id)
                for question in (
                    (
                        await session.execute(
                            select(QualityProposedQuestionRow)
                            .where(
                                QualityProposedQuestionRow.receipt_id == row.id
                            )
                            .order_by(
                                QualityProposedQuestionRow.created_at,
                                QualityProposedQuestionRow.qa_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            return AssessmentCommitResult(
                board_id=row.board_id,
                subject_type=AssessmentSubjectType(row.subject_type),
                subject_id=row.subject_id,
                subject_version=row.subject_version,
                assessment_kind=AssessmentKind(row.assessment_kind),
                request_fingerprint=row.request_digest,
                receipt_id=row.id,
                head_revision=row.head_revision,
                event_id=row.event_id,
                history_id=row.history_id,
                outbox_id=row.outbox_id,
                qa_id_map=qa_id_map,
                replayed=True,
            )

    async def resolve_assessment_preflight(
        self,
        submission: AssessmentSubmission,
        *,
        actor_id: str,
        realm_scope: RealmScope,
    ) -> AssessmentPreflight:
        """Compatibility seam for the pre-C9 materialized command."""

        return await self.resolve_assessment_preflight_request(
            AssessmentPreflightRequest(
                board_id=submission.board_id,
                subject_type=submission.subject_type,
                subject_id=submission.subject_id,
                assessment_kind=submission.assessment_kind,
                expected_subject_version=submission.expected_subject_version,
                expected_head_revision=submission.expected_head_revision,
                channel="mcp:quality_assess",
            ),
            actor_id=actor_id,
            realm_scope=realm_scope,
        )

    async def resolve_assessment_preflight_request(
        self,
        request: AssessmentPreflightRequest,
        *,
        actor_id: str,
        realm_scope: RealmScope,
    ) -> AssessmentPreflight:
        _require_local_realm(realm_scope)
        if (
            request.assessment_kind is not AssessmentKind.AMBIGUITY
            or request.subject_type
            not in {
                AssessmentSubjectType.IDEATION,
                AssessmentSubjectType.REFINEMENT,
            }
        ):
            raise QualityAssessmentPersistenceError(
                "assessment_preflight_identity_unsupported"
            )
        async with self._session_factory() as session:
            context = await _load_quality_subject_context(
                session,
                board_id=request.board_id,
                subject_type=request.subject_type,
                subject_id=request.subject_id,
                assessment_kind=request.assessment_kind,
            )
            if context is None:
                raise AssessmentSubjectNotFound()
            configuration = resolve_ambiguity_gate_configuration(
                request.subject_type,
                context.board.settings,
            )
            subject_version = int(getattr(context.subject, "version"))
            authority = await _ambiguity_authority_for(
                session,
                actor_id=actor_id,
                board_id=request.board_id,
                subject_type=request.subject_type,
                subject_id=request.subject_id,
                subject_version=subject_version,
                channel=request.channel,
            )
            return AssessmentPreflight(
                subject=AssessmentSubjectRef(
                    board_id=request.board_id,
                    subject_type=request.subject_type,
                    subject_id=request.subject_id,
                    subject_version=subject_version,
                ),
                status=_enum_value(getattr(context.subject, "status")),
                current_head_revision=(
                    0 if context.head is None else int(context.head.revision)
                ),
                current_head_receipt_id=(
                    None if context.head is None else context.head.receipt_id
                ),
                channel=request.channel,
                expected_scale=AMBIGUITY_SCALE,
                digests=ambiguity_digest_set(
                    subject_type=request.subject_type,
                    subject=context.subject,
                    qa_items=context.qa_items,
                    configuration=configuration,
                ),
                versions=ambiguity_versions_v1(),
                anchors=ambiguity_anchor_catalog(
                    subject_type=request.subject_type,
                    subject=context.subject,
                    qa_items=context.qa_items,
                ),
                allowed_category_codes=AMBIGUITY_TAXONOMY_CATEGORY_ID_SET,
                authority=authority,
                origin=AssessmentOrigin.HUMAN_OR_AGENT,
                subject_archived=bool(getattr(context.subject, "archived")),
            )

    async def resolve_assessment_read_context(
        self,
        *,
        board_id: str,
        subject_type: AssessmentSubjectType,
        subject_id: str,
        actor_id: str,
        realm_scope: RealmScope,
    ) -> QualityAssessmentReadContext:
        _require_local_realm(realm_scope)
        async with self._session_factory() as session:
            context = await _load_quality_subject_context(
                session,
                board_id=board_id,
                subject_type=subject_type,
                subject_id=subject_id,
                # The subject/board/Q&A read is kind-independent.
                assessment_kind=AssessmentKind.AMBIGUITY,
            )
            if context is None:
                raise AssessmentSubjectNotFound()
            permissions, access_level = await _quality_actor_permissions(
                session,
                actor_id=actor_id,
                board_id=board_id,
            )
            if (
                permissions is None
                or access_level is None
                or not permissions.has(f"{subject_type.value}.quality.read")
            ):
                raise AssessmentReadAccessDenied()
            return QualityAssessmentReadContext(
                subject=AssessmentSubjectRef(
                    board_id=board_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    subject_version=int(getattr(context.subject, "version")),
                ),
                currentness_inputs=_currentness_inputs_for(
                    context=context,
                    subject_type=subject_type,
                ),
                gate_inputs=_gate_inputs_for(
                    context=context,
                    subject_type=subject_type,
                ),
            )

    async def resolve_receipt_read_context(
        self,
        *,
        receipt_id: str,
        board_id: str | None,
        actor_id: str,
        realm_scope: RealmScope,
    ) -> QualityAssessmentReadContext:
        _require_local_realm(realm_scope)
        async with self._session_factory() as session:
            receipt = (
                await session.execute(
                    select(QualityAssessmentReceiptRow).where(
                        QualityAssessmentReceiptRow.id == receipt_id
                    )
                )
            ).scalar_one_or_none()
        if receipt is None or (
            board_id is not None and receipt.board_id != board_id
        ):
            raise AssessmentReceiptNotFound()
        try:
            return await self.resolve_assessment_read_context(
                board_id=receipt.board_id,
                subject_type=AssessmentSubjectType(receipt.subject_type),
                subject_id=receipt.subject_id,
                actor_id=actor_id,
                realm_scope=realm_scope,
            )
        except (AssessmentReadAccessDenied, AssessmentSubjectNotFound) as exc:
            # The global immutable-receipt lookup must not reveal whether an
            # otherwise inaccessible receipt or its parent subject exists.
            raise AssessmentReceiptNotFound() from exc


class CommunitySqlAlchemyQualityAssessment:
    """Real relational implementation of the D0 assessment persistence port.

    Write resolvers are intentionally mandatory. They are composition-owned
    trust boundaries and must rederive the current authority and normative
    input identities (locking mutable source rows when applicable). The
    adapter independently rederives semantic content and answered Q&A from the
    subject tables, so merely echoing a bundle cannot bypass those fences.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        authority_resolver: AuthorityDigestResolver | None = None,
        input_digest_resolver: InputDigestResolver | None = None,
    ) -> None:
        self._session = session
        self._authority_resolver = authority_resolver
        self._input_digest_resolver = input_digest_resolver

    async def apply_bundle_cas(
        self,
        bundle: AssessmentWriteBundle,
    ) -> AssessmentCommitResult:
        """Stage a bundle after replay, authority, subject, input and head CAS."""

        try:
            # Callers may have staged the subject mutation that produced this
            # assessment.  Make that state visible before repeating replay and
            # fences, while retaining ownership of the surrounding transaction.
            await self._session.flush()

            prior = await self._receipt_by_idempotency(
                board_id=bundle.receipt.subject.board_id,
                idempotency_key=bundle.idempotency_key,
            )
            if prior is not None:
                return await self._replay_result(prior, bundle)

            await self._validate_authority(bundle)
            subject_row = await self._fence_subject(bundle)
            await self._validate_input(bundle, subject_row)
            await self._validate_predecessor(bundle)

            self._session.add(self._receipt_row(bundle))
            await self._session.flush()

            await self._advance_head(bundle)
            self._stage_children(bundle)
            await self._session.flush()
            self._stage_links(bundle)
            self._stage_audit_rows(bundle)
            await self._session.flush()
            self._stage_handler_execution(bundle)
            await self._session.flush()
        except (
            AssessmentAuthorityConflict,
            AssessmentHeadRevisionConflict,
            AssessmentIdempotencyConflict,
            AssessmentInputDigestConflict,
            AssessmentSubjectLifecycleConflict,
            AssessmentSubjectStatusConflict,
            AssessmentSubjectVersionConflict,
            QualityAssessmentPersistenceError,
        ):
            raise
        except IntegrityError as exc:
            raise self._translate_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            raise QualityAssessmentPersistenceError(
                "quality_assessment_sql_failure"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise QualityAssessmentPersistenceError(
                "quality_assessment_persisted_contract_corrupt"
            ) from exc

        return self._result_from_bundle(bundle, replayed=False)

    async def _receipt_by_idempotency(
        self,
        *,
        board_id: str,
        idempotency_key: str,
    ) -> QualityAssessmentReceiptRow | None:
        return (
            await self._session.execute(
                select(QualityAssessmentReceiptRow).where(
                    QualityAssessmentReceiptRow.board_id == board_id,
                    QualityAssessmentReceiptRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()

    async def _replay_result(
        self,
        row: QualityAssessmentReceiptRow,
        bundle: AssessmentWriteBundle,
    ) -> AssessmentCommitResult:
        if row.request_digest != bundle.request_fingerprint:
            raise AssessmentIdempotencyConflict(
                "assessment_idempotency_fingerprint_mismatch"
            )
        expected_subject = bundle.receipt.subject
        if (
            row.subject_type != expected_subject.subject_type.value
            or row.subject_id != expected_subject.subject_id
            or row.subject_version != expected_subject.subject_version
            or row.assessment_kind != bundle.receipt.assessment_kind.value
        ):
            raise AssessmentIdempotencyConflict(
                "assessment_idempotency_identity_mismatch"
            )

        detail = await self._detail_for_row(row)
        incoming_detail = AssessmentReceiptDetail(
            receipt=bundle.receipt,
            findings=bundle.findings,
            proposed_questions=bundle.proposed_questions,
            finding_qa_links=bundle.finding_qa_links,
        )
        if (
            _receipt_replay_signature(detail.receipt)
            != _receipt_replay_signature(bundle.receipt)
            or {
                item.finding_key: _finding_replay_signature(item)
                for item in detail.findings
            }
            != {
                item.finding_key: _finding_replay_signature(item)
                for item in bundle.findings
            }
            or {
                item.client_key: _question_replay_signature(item)
                for item in detail.proposed_questions
            }
            != {
                item.client_key: _question_replay_signature(item)
                for item in bundle.proposed_questions
            }
            or _semantic_links(detail) != _semantic_links(incoming_detail)
        ):
            raise QualityAssessmentPersistenceError(
                "assessment_replay_aggregate_corrupt"
            )
        await self._verify_audit_rows(row)
        qa_map = tuple(
            (question.client_key, question.qa_id)
            for question in detail.proposed_questions
        )
        return AssessmentCommitResult(
            board_id=row.board_id,
            subject_type=AssessmentSubjectType(row.subject_type),
            subject_id=row.subject_id,
            subject_version=row.subject_version,
            assessment_kind=AssessmentKind(row.assessment_kind),
            request_fingerprint=row.request_digest,
            receipt_id=row.id,
            head_revision=row.head_revision,
            event_id=row.event_id,
            history_id=row.history_id,
            outbox_id=row.outbox_id,
            qa_id_map=qa_map,
            replayed=True,
        )

    async def _verify_audit_rows(
        self,
        receipt: QualityAssessmentReceiptRow,
    ) -> None:
        event_row, execution_row, activity_row, outbox_row = (
            (
                await self._session.execute(
                    select(DomainEventRow).where(
                        DomainEventRow.id == receipt.event_id,
                        DomainEventRow.board_id == receipt.board_id,
                    )
                )
            ).scalar_one_or_none(),
            (
                await self._session.execute(
                    select(DomainEventHandlerExecution).where(
                        DomainEventHandlerExecution.event_id
                        == receipt.event_id,
                        DomainEventHandlerExecution.handler_name
                        == _CONSOLIDATION_HANDLER_NAME,
                    )
                )
            ).scalar_one_or_none(),
            (
                await self._session.execute(
                    select(ActivityLog).where(
                        ActivityLog.id == receipt.history_id,
                        ActivityLog.board_id == receipt.board_id,
                    )
                )
            ).scalar_one_or_none(),
            (
                await self._session.execute(
                    select(QualityAssessmentOutboxRow).where(
                        QualityAssessmentOutboxRow.id == receipt.outbox_id,
                        QualityAssessmentOutboxRow.board_id == receipt.board_id,
                        QualityAssessmentOutboxRow.receipt_id == receipt.id,
                    )
                )
            ).scalar_one_or_none(),
        )
        binding = _SUBJECT_BINDINGS[AssessmentSubjectType(receipt.subject_type)]
        subject_history = (
            await self._session.execute(
                select(binding.history_model).where(
                    binding.history_model.id == receipt.history_id,
                    getattr(binding.history_model, binding.history_fk)
                    == receipt.subject_id,
                )
            )
        ).scalar_one_or_none()
        expected_payload = {
            "event_schema_version": 1,
            "receipt_id": receipt.id,
            "subject_type": receipt.subject_type,
            "subject_id": receipt.subject_id,
            "subject_version": receipt.subject_version,
            "assessment_kind": receipt.assessment_kind,
            "input_digest": receipt.input_digest,
            "request_fingerprint": receipt.request_digest,
            "authority_digest": receipt.authority_digest,
            "head_revision": receipt.head_revision,
            "predecessor_receipt_id": receipt.predecessor_receipt_id,
            "outcome": receipt.outcome,
        }
        if (
            event_row is None
            or execution_row is None
            or activity_row is None
            or outbox_row is None
            or subject_history is None
            or event_row.event_type != "quality.assessment_recorded.v1"
            or event_row.actor_id != receipt.created_by
            or event_row.payload_json != expected_payload
            or activity_row.action != "quality_assessment_recorded"
            or activity_row.actor_id != receipt.created_by
            or activity_row.details != expected_payload
            or outbox_row.event_id != receipt.event_id
            or outbox_row.event_type != "quality.assessment_recorded.v1"
            or outbox_row.payload_json != expected_payload
            or subject_history.action != "quality_assessment_recorded"
            or subject_history.actor_id != receipt.created_by
            or subject_history.version != receipt.subject_version
        ):
            raise QualityAssessmentPersistenceError("assessment_replay_audit_corrupt")

    async def _validate_authority(self, bundle: AssessmentWriteBundle) -> None:
        if self._authority_resolver is None:
            raise AssessmentAuthorityConflict("assessment_authority_resolver_missing")
        try:
            snapshot = await _resolved(self._authority_resolver(self._session, bundle))
        except AssessmentAuthorityConflict:
            raise
        except Exception as exc:
            raise AssessmentAuthorityConflict(
                "assessment_authority_resolution_failed"
            ) from exc
        if not isinstance(snapshot, AssessmentAuthoritySnapshot):
            raise AssessmentAuthorityConflict("assessment_authority_resolution_invalid")
        receipt = bundle.receipt
        if snapshot.authority_digest != bundle.expected_authority_digest:
            raise AssessmentAuthorityConflict("assessment_authority_fence_mismatch")
        semantic_lint = (
            receipt.subject.subject_type is AssessmentSubjectType.SPEC
            and receipt.assessment_kind is AssessmentKind.REQUIREMENT_LINT
            and receipt.origin is AssessmentOrigin.SEMANTIC_WRITER
        )
        if semantic_lint:
            if not snapshot.domain_write:
                raise AssessmentAuthorityConflict("assessment_authority_fence_mismatch")
            return
        if (
            not snapshot.domain_write
            or not snapshot.quality_assess
            or (bundle.proposed_questions and not snapshot.qa_ask)
            or (
                receipt.subject.subject_type is AssessmentSubjectType.SPEC
                and (
                    not snapshot.spec_validation_submit
                    or not snapshot.reviewer_separation_satisfied
                )
            )
        ):
            raise AssessmentAuthorityConflict("assessment_authority_fence_mismatch")

    async def _fence_subject(self, bundle: AssessmentWriteBundle) -> object:
        subject = bundle.receipt.subject
        binding = _SUBJECT_BINDINGS[subject.subject_type]
        current = (
            await self._session.execute(
                select(binding.model)
                .where(
                    binding.model.id == subject.subject_id,
                    binding.model.board_id == subject.board_id,
                    binding.model.version == bundle.expected_subject_version,
                    binding.model.status == bundle.expected_subject_status,
                    binding.model.archived.is_(bundle.expected_subject_archived),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is not None:
            return current

        diagnostic = (
            await self._session.execute(
                select(binding.model).where(
                    binding.model.id == subject.subject_id,
                    binding.model.board_id == subject.board_id,
                )
            )
        ).scalar_one_or_none()
        if diagnostic is None:
            raise AssessmentSubjectVersionConflict("assessment_subject_missing")
        if bool(diagnostic.archived) != bundle.expected_subject_archived:
            raise AssessmentSubjectLifecycleConflict(
                "assessment_subject_lifecycle_mismatch"
            )
        if _enum_value(diagnostic.status) != bundle.expected_subject_status:
            raise AssessmentSubjectStatusConflict("assessment_subject_status_mismatch")
        if diagnostic.version != bundle.expected_subject_version:
            raise AssessmentSubjectVersionConflict(
                "assessment_subject_version_mismatch"
            )
        raise AssessmentSubjectVersionConflict("assessment_subject_fence_lost")

    async def _validate_input(
        self,
        bundle: AssessmentWriteBundle,
        subject_row: object,
    ) -> None:
        if self._input_digest_resolver is None:
            raise AssessmentInputDigestConflict(
                "assessment_input_digest_resolver_missing"
            )
        subject = bundle.receipt.subject
        binding = _SUBJECT_BINDINGS[subject.subject_type]
        payload = {
            field: getattr(subject_row, field, None)
            for field in SEMANTIC_FIELD_MANIFEST_V1[subject.subject_type.value]
        }
        qa_rows = list(
            (
                await self._session.execute(
                    select(binding.qa_model)
                    .where(
                        getattr(binding.qa_model, binding.subject_fk)
                        == subject.subject_id
                    )
                    .order_by(binding.qa_model.id)
                )
            )
            .scalars()
            .all()
        )
        qa_payload = [
            {
                "id": row.id,
                "revision": row.revision,
                "question": row.question,
                "question_type": row.question_type,
                "choices": row.choices or [],
                "allow_free_text": bool(row.allow_free_text),
                "answer": row.answer,
                "selected": row.selected or [],
                "answered_at": row.answered_at,
                "lifecycle": row.lifecycle,
                "tombstoned": bool(row.tombstoned),
            }
            for row in qa_rows
        ]
        try:
            content_digest = semantic_content_digest_v1(
                subject.subject_type,
                payload,
            )
            clarification_digest = clarification_digest_v1(qa_payload)
            resolved_digests = await _resolved(
                self._input_digest_resolver(self._session, bundle)
            )
        except AssessmentInputDigestConflict:
            raise
        except Exception as exc:
            raise AssessmentInputDigestConflict(
                "assessment_input_digest_resolution_failed"
            ) from exc
        if not isinstance(resolved_digests, AssessmentDigestSet):
            raise AssessmentInputDigestConflict(
                "assessment_input_digest_resolution_invalid"
            )
        persisted = bundle.receipt.digests
        if (
            content_digest != persisted.content_digest
            or clarification_digest != persisted.clarification_digest
            or resolved_digests != persisted
            or bundle.expected_input_digest != persisted.input_digest
        ):
            raise AssessmentInputDigestConflict(
                "assessment_input_digest_fence_mismatch"
            )

    async def _validate_predecessor(
        self,
        bundle: AssessmentWriteBundle,
    ) -> None:
        subject = bundle.receipt.subject
        current = (
            await self._session.execute(
                select(QualityAssessmentHeadRow).where(
                    QualityAssessmentHeadRow.board_id == subject.board_id,
                    QualityAssessmentHeadRow.subject_type == subject.subject_type.value,
                    QualityAssessmentHeadRow.subject_id == subject.subject_id,
                    QualityAssessmentHeadRow.assessment_kind
                    == bundle.receipt.assessment_kind.value,
                )
            )
        ).scalar_one_or_none()
        if bundle.expected_head_revision == 0:
            if current is not None or bundle.expected_head_receipt_id is not None:
                raise AssessmentHeadRevisionConflict(
                    "assessment_head_initial_revision_conflict"
                )
            return
        if (
            current is None
            or current.revision != bundle.expected_head_revision
            or current.receipt_id != bundle.expected_head_receipt_id
            or bundle.receipt.predecessor_receipt_id != bundle.expected_head_receipt_id
        ):
            raise AssessmentHeadRevisionConflict("assessment_head_predecessor_conflict")

    def _receipt_row(
        self,
        bundle: AssessmentWriteBundle,
    ) -> QualityAssessmentReceiptRow:
        receipt = bundle.receipt
        return QualityAssessmentReceiptRow(
            id=receipt.id,
            board_id=receipt.subject.board_id,
            subject_type=receipt.subject.subject_type.value,
            subject_id=receipt.subject.subject_id,
            subject_version=receipt.subject.subject_version,
            assessment_kind=receipt.assessment_kind.value,
            origin=receipt.origin.value,
            source=receipt.source.value,
            channel=receipt.channel,
            outcome=receipt.outcome.value,
            scale_kind=receipt.scale.kind.value,
            scale_minimum=receipt.scale.minimum,
            scale_maximum=receipt.scale.maximum,
            scale_direction=receipt.scale.direction.value,
            score=receipt.score,
            justification=receipt.justification,
            content_digest=receipt.digests.content_digest,
            clarification_digest=receipt.digests.clarification_digest,
            ruleset_digest=receipt.digests.ruleset_digest,
            taxonomy_digest=receipt.digests.taxonomy_digest,
            policy_digest=receipt.digests.policy_digest,
            input_digest=receipt.digests.input_digest,
            canonicalization_version=receipt.digests.canonicalization_version,
            ruleset_version=receipt.versions.ruleset_version,
            taxonomy_version=receipt.versions.taxonomy_version,
            analyzer_version=receipt.versions.analyzer_version,
            policy_version=receipt.versions.policy_version,
            run_identity_digest=receipt.run_identity_digest,
            authority_digest=receipt.authority_digest,
            idempotency_key=receipt.idempotency_key,
            request_digest=receipt.request_digest,
            created_by=receipt.created_by,
            created_at=receipt.created_at,
            predecessor_receipt_id=receipt.predecessor_receipt_id,
            contract_version=receipt.contract_version,
            event_id=bundle.audit_intent.event_id,
            history_id=bundle.audit_intent.history_id,
            outbox_id=bundle.audit_intent.outbox_id,
            head_revision=bundle.next_head.revision,
        )

    async def _advance_head(self, bundle: AssessmentWriteBundle) -> None:
        head = bundle.next_head
        if bundle.expected_head_revision == 0:
            self._session.add(
                QualityAssessmentHeadRow(
                    board_id=head.board_id,
                    subject_type=head.subject_type.value,
                    subject_id=head.subject_id,
                    assessment_kind=head.assessment_kind.value,
                    receipt_id=head.receipt_id,
                    revision=head.revision,
                    updated_at=head.updated_at,
                )
            )
            await self._session.flush()
            return
        result = await self._session.execute(
            update(QualityAssessmentHeadRow)
            .where(
                QualityAssessmentHeadRow.board_id == head.board_id,
                QualityAssessmentHeadRow.subject_type == head.subject_type.value,
                QualityAssessmentHeadRow.subject_id == head.subject_id,
                QualityAssessmentHeadRow.assessment_kind == head.assessment_kind.value,
                QualityAssessmentHeadRow.revision == bundle.expected_head_revision,
                QualityAssessmentHeadRow.receipt_id == bundle.expected_head_receipt_id,
            )
            .values(
                receipt_id=head.receipt_id,
                revision=head.revision,
                updated_at=head.updated_at,
            )
        )
        if result.rowcount != 1:
            raise AssessmentHeadRevisionConflict("assessment_head_cas_conflict")

    def _stage_children(self, bundle: AssessmentWriteBundle) -> None:
        receipt = bundle.receipt
        subject = receipt.subject
        binding = _SUBJECT_BINDINGS[subject.subject_type]
        for finding in bundle.findings:
            self._session.add(
                QualityFindingRow(
                    id=finding.id,
                    receipt_id=finding.receipt_id,
                    board_id=subject.board_id,
                    subject_type=subject.subject_type.value,
                    subject_id=subject.subject_id,
                    assessment_kind=finding.assessment_kind.value,
                    finding_key=finding.finding_key,
                    category_code=finding.category_code,
                    taxonomy_version=finding.taxonomy_version,
                    severity=finding.severity.value,
                    confidence=finding.confidence,
                    deterministic=finding.deterministic,
                    blocking_eligible=finding.blocking_eligible,
                    title=finding.title,
                    detail=finding.detail,
                    anchor_board_id=finding.anchor.board_id,
                    anchor_subject_type=finding.anchor.subject_type.value,
                    anchor_subject_id=finding.anchor.subject_id,
                    anchor_subject_version=finding.anchor.subject_version,
                    anchor_input_digest=finding.anchor.input_digest,
                    anchor_type=finding.anchor.anchor_type.value,
                    anchor_ref=finding.anchor.anchor_ref,
                    excerpt_hash=finding.anchor.excerpt_hash,
                    evidence_refs=[
                        {
                            "source_type": evidence.source_type,
                            "source_id": evidence.source_id,
                            "source_version": evidence.source_version,
                            "content_hash": evidence.content_hash,
                        }
                        for evidence in finding.evidence_refs
                    ],
                    lifecycle=finding.lifecycle.value,
                    created_at=finding.created_at,
                    remediation=finding.remediation,
                    rule_code=finding.rule_code,
                )
            )
        for question in bundle.proposed_questions:
            choices = list(question.choices)
            # The immutable quality contract names free-form proposals
            # ``free_text`` and choice labels as strings.  The existing
            # subject Q&A contract names the interaction ``text`` and requires
            # stable option objects. Preserve the receipt-owned values and
            # adapt only the materialized operational Q&A row.
            qa_question_type = (
                "text"
                if question.question_type == "free_text"
                else question.question_type
            )
            qa_choices = [
                {
                    "id": f"opt_{index}",
                    "label": label,
                    "recommended": False,
                    "tradeoff": None,
                }
                for index, label in enumerate(question.choices, start=1)
            ]
            self._session.add(
                QualityProposedQuestionRow(
                    qa_id=question.qa_id,
                    receipt_id=question.receipt_id,
                    client_key=question.client_key,
                    question=question.question,
                    question_type=question.question_type,
                    choices=choices,
                    allow_free_text=question.allow_free_text,
                    category_code=question.category_code,
                    created_at=question.created_at,
                )
            )
            self._session.add(
                binding.qa_model(
                    id=question.qa_id,
                    **{binding.subject_fk: subject.subject_id},
                    question=question.question,
                    question_type=qa_question_type,
                    choices=qa_choices,
                    allow_free_text=question.allow_free_text,
                    answer=None,
                    selected=None,
                    asked_by=receipt.created_by,
                    answered_by=None,
                    created_at=question.created_at,
                    answered_at=None,
                    revision=1,
                    lifecycle="active",
                    tombstoned=False,
                )
            )

    def _stage_links(self, bundle: AssessmentWriteBundle) -> None:
        for link in bundle.finding_qa_links:
            self._session.add(
                QualityFindingQaLinkRow(
                    receipt_id=link.receipt_id,
                    finding_id=link.finding_id,
                    qa_id=link.qa_id,
                )
            )

    def _stage_audit_rows(self, bundle: AssessmentWriteBundle) -> None:
        receipt = bundle.receipt
        audit = bundle.audit_intent
        subject = receipt.subject
        binding = _SUBJECT_BINDINGS[subject.subject_type]
        payload = {
            "event_schema_version": audit.event_schema_version,
            "receipt_id": receipt.id,
            "subject_type": subject.subject_type.value,
            "subject_id": subject.subject_id,
            "subject_version": subject.subject_version,
            "assessment_kind": receipt.assessment_kind.value,
            "input_digest": receipt.digests.input_digest,
            "request_fingerprint": bundle.request_fingerprint,
            "authority_digest": receipt.authority_digest,
            "head_revision": bundle.next_head.revision,
            "predecessor_receipt_id": receipt.predecessor_receipt_id,
            "outcome": receipt.outcome.value,
        }
        actor_type = (
            "agent" if receipt.origin is AssessmentOrigin.SEMANTIC_WRITER else "user"
        )
        self._session.add(
            DomainEventRow(
                id=audit.event_id,
                event_type=audit.event_type,
                board_id=subject.board_id,
                actor_id=audit.actor_id,
                actor_type=actor_type,
                payload_json=dict(payload),
                occurred_at=audit.occurred_at,
            )
        )
        self._session.add(
            ActivityLog(
                id=audit.history_id,
                board_id=subject.board_id,
                card_id=None,
                action=audit.history_action,
                actor_type=actor_type,
                actor_id=audit.actor_id,
                actor_name=audit.actor_id,
                details=dict(payload),
                created_at=audit.occurred_at,
            )
        )
        self._session.add(
            binding.history_model(
                id=audit.history_id,
                **{binding.history_fk: subject.subject_id},
                action=audit.history_action,
                actor_type=actor_type,
                actor_id=audit.actor_id,
                actor_name=audit.actor_id,
                changes=[
                    {
                        "field": "quality_assessment_head",
                        "old": bundle.expected_head_receipt_id,
                        "new": receipt.id,
                    }
                ],
                summary=(
                    f"{receipt.assessment_kind.value} assessment recorded "
                    f"as {receipt.id}"
                ),
                version=subject.subject_version,
                created_at=audit.occurred_at,
            )
        )
        self._session.add(
            QualityAssessmentOutboxRow(
                id=audit.outbox_id,
                event_id=audit.event_id,
                receipt_id=receipt.id,
                board_id=subject.board_id,
                event_type=audit.event_type,
                payload_json=dict(payload),
                status="pending",
                occurred_at=audit.occurred_at,
            )
        )

    def _stage_handler_execution(self, bundle: AssessmentWriteBundle) -> None:
        """Stage delivery only after the event parent has been flushed."""

        self._session.add(
            DomainEventHandlerExecution(
                event_id=bundle.audit_intent.event_id,
                handler_name=_CONSOLIDATION_HANDLER_NAME,
                status="pending",
                attempts=0,
            )
        )

    def _result_from_bundle(
        self,
        bundle: AssessmentWriteBundle,
        *,
        replayed: bool,
    ) -> AssessmentCommitResult:
        receipt = bundle.receipt
        return AssessmentCommitResult(
            board_id=receipt.subject.board_id,
            subject_type=receipt.subject.subject_type,
            subject_id=receipt.subject.subject_id,
            subject_version=receipt.subject.subject_version,
            assessment_kind=receipt.assessment_kind,
            request_fingerprint=bundle.request_fingerprint,
            receipt_id=receipt.id,
            head_revision=bundle.next_head.revision,
            event_id=bundle.audit_intent.event_id,
            history_id=bundle.audit_intent.history_id,
            outbox_id=bundle.audit_intent.outbox_id,
            qa_id_map=tuple(
                (question.client_key, question.qa_id)
                for question in bundle.proposed_questions
            ),
            replayed=replayed,
        )

    @staticmethod
    def _translate_integrity_error(
        exc: IntegrityError,
    ) -> QualityAssessmentPersistenceError:
        detail = str(exc.orig).lower()
        if "uq_quality_receipt_board_idempotency" in detail or (
            "quality_assessment_receipts.board_id" in detail
            and "quality_assessment_receipts.idempotency_key" in detail
        ):
            return AssessmentIdempotencyConflict("assessment_idempotency_race")
        if (
            "quality_assessment_heads" in detail
            or "uq_quality_head" in detail
            or "fk_quality_head_receipt_subject" in detail
        ):
            return AssessmentHeadRevisionConflict("assessment_head_cas_conflict")
        return QualityAssessmentPersistenceError("quality_assessment_integrity_failure")

    async def get_current(
        self,
        *,
        board_id: str,
        subject_type: AssessmentSubjectType,
        subject_id: str,
        assessment_kind: AssessmentKind,
    ) -> tuple[AssessmentReceipt, AssessmentSubjectHead] | None:
        row = (
            await self._session.execute(
                select(
                    QualityAssessmentReceiptRow,
                    QualityAssessmentHeadRow,
                )
                .join(
                    QualityAssessmentHeadRow,
                    QualityAssessmentHeadRow.receipt_id
                    == QualityAssessmentReceiptRow.id,
                )
                .where(
                    QualityAssessmentHeadRow.board_id == board_id,
                    QualityAssessmentHeadRow.subject_type == subject_type.value,
                    QualityAssessmentHeadRow.subject_id == subject_id,
                    QualityAssessmentHeadRow.assessment_kind == assessment_kind.value,
                    QualityAssessmentReceiptRow.board_id == board_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        receipt_row, head_row = row
        return _receipt_from_row(receipt_row), _head_from_row(head_row)

    async def get_receipt(
        self,
        *,
        board_id: str,
        receipt_id: str,
    ) -> AssessmentReceipt | None:
        row = (
            await self._session.execute(
                select(QualityAssessmentReceiptRow).where(
                    QualityAssessmentReceiptRow.board_id == board_id,
                    QualityAssessmentReceiptRow.id == receipt_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _receipt_from_row(row)

    async def get_receipt_detail(
        self,
        *,
        board_id: str,
        receipt_id: str,
    ) -> AssessmentReceiptDetail | None:
        row = (
            await self._session.execute(
                select(QualityAssessmentReceiptRow).where(
                    QualityAssessmentReceiptRow.board_id == board_id,
                    QualityAssessmentReceiptRow.id == receipt_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else await self._detail_for_row(row)

    async def _detail_for_row(
        self,
        receipt_row: QualityAssessmentReceiptRow,
    ) -> AssessmentReceiptDetail:
        findings = tuple(
            _finding_from_row(row)
            for row in (
                (
                    await self._session.execute(
                        select(QualityFindingRow)
                        .where(
                            QualityFindingRow.receipt_id == receipt_row.id,
                            QualityFindingRow.board_id == receipt_row.board_id,
                        )
                        .order_by(
                            QualityFindingRow.created_at,
                            QualityFindingRow.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        questions = tuple(
            _question_from_row(row)
            for row in (
                (
                    await self._session.execute(
                        select(QualityProposedQuestionRow)
                        .where(QualityProposedQuestionRow.receipt_id == receipt_row.id)
                        .order_by(
                            QualityProposedQuestionRow.created_at,
                            QualityProposedQuestionRow.qa_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        links = tuple(
            FindingQaLink(
                finding_id=row.finding_id,
                qa_id=row.qa_id,
                receipt_id=row.receipt_id,
            )
            for row in (
                (
                    await self._session.execute(
                        select(QualityFindingQaLinkRow)
                        .where(QualityFindingQaLinkRow.receipt_id == receipt_row.id)
                        .order_by(
                            QualityFindingQaLinkRow.finding_id,
                            QualityFindingQaLinkRow.qa_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        return AssessmentReceiptDetail(
            receipt=_receipt_from_row(receipt_row),
            findings=findings,
            proposed_questions=questions,
            finding_qa_links=links,
        )

    async def list_assessments(
        self,
        query: AssessmentListQuery,
    ) -> QualityPage[AssessmentReceiptView]:
        started_at = perf_counter()
        try:
            page = await self._list_assessments_unobserved(query)
        except Exception:
            observe_ska_projection_queries(
                surface="quality_assessments",
                subject_type=query.subject.subject_type.value,
                query_count=4,
                duration_ms=(perf_counter() - started_at) * 1000,
                payload_bytes=0,
                outcome="error",
            )
            raise
        observe_ska_projection_queries(
            surface="quality_assessments",
            subject_type=query.subject.subject_type.value,
            query_count=4,
            duration_ms=(perf_counter() - started_at) * 1000,
            payload_bytes=0,
            outcome="success",
        )
        return page

    async def _list_assessments_unobserved(
        self,
        query: AssessmentListQuery,
    ) -> QualityPage[AssessmentReceiptView]:
        if query.current_subject_version is None or (
            query.current_digests is None and not query.currentness_inputs
        ):
            raise QualityAssessmentPersistenceError(
                "assessment_current_projection_inputs_required"
            )
        subject = query.subject
        base_filters = (
            QualityAssessmentReceiptRow.board_id == subject.board_id,
            QualityAssessmentReceiptRow.subject_type == subject.subject_type.value,
            QualityAssessmentReceiptRow.subject_id == subject.subject_id,
        )
        filtered_conditions = list(base_filters)
        if query.assessment_kind is not None:
            filtered_conditions.append(
                QualityAssessmentReceiptRow.assessment_kind
                == query.assessment_kind.value
            )

        # State is a projection over the append-only receipt and its current
        # per-kind head. Express that projection in SQL so offset/limit always
        # bounds the rows materialized by the adapter.
        head_join = and_(
            QualityAssessmentHeadRow.board_id
            == QualityAssessmentReceiptRow.board_id,
            QualityAssessmentHeadRow.subject_type
            == QualityAssessmentReceiptRow.subject_type,
            QualityAssessmentHeadRow.subject_id
            == QualityAssessmentReceiptRow.subject_id,
            QualityAssessmentHeadRow.assessment_kind
            == QualityAssessmentReceiptRow.assessment_kind,
        )
        is_head = QualityAssessmentHeadRow.receipt_id == QualityAssessmentReceiptRow.id
        if query.currentness_inputs:
            current_inputs_match = or_(
                *(
                    and_(
                        QualityAssessmentReceiptRow.assessment_kind
                        == item.assessment_kind.value,
                        QualityAssessmentReceiptRow.origin == item.origin.value,
                        QualityAssessmentReceiptRow.source == item.source.value,
                        QualityAssessmentReceiptRow.subject_version
                        == query.current_subject_version,
                        QualityAssessmentReceiptRow.content_digest
                        == item.digests.content_digest,
                        QualityAssessmentReceiptRow.clarification_digest
                        == item.digests.clarification_digest,
                        QualityAssessmentReceiptRow.ruleset_digest
                        == item.digests.ruleset_digest,
                        QualityAssessmentReceiptRow.taxonomy_digest
                        == item.digests.taxonomy_digest,
                        QualityAssessmentReceiptRow.policy_digest
                        == item.digests.policy_digest,
                    )
                    for item in query.currentness_inputs
                )
            )
        else:
            assert query.current_digests is not None
            current_inputs_match = and_(
                QualityAssessmentReceiptRow.subject_version
                == query.current_subject_version,
                QualityAssessmentReceiptRow.content_digest
                == query.current_digests.content_digest,
                QualityAssessmentReceiptRow.clarification_digest
                == query.current_digests.clarification_digest,
                QualityAssessmentReceiptRow.ruleset_digest
                == query.current_digests.ruleset_digest,
                QualityAssessmentReceiptRow.taxonomy_digest
                == query.current_digests.taxonomy_digest,
                QualityAssessmentReceiptRow.policy_digest
                == query.current_digests.policy_digest,
            )
        if query.state is AssessmentReceiptState.SUPERSEDED:
            filtered_conditions.append(
                or_(
                    QualityAssessmentHeadRow.receipt_id.is_(None),
                    not_(is_head),
                )
            )
        elif query.state is AssessmentReceiptState.CURRENT:
            filtered_conditions.extend((is_head, current_inputs_match))
        elif query.state is AssessmentReceiptState.STALE:
            filtered_conditions.extend((is_head, not_(current_inputs_match)))

        total_overall = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(QualityAssessmentReceiptRow)
                    .where(*base_filters)
                )
            ).scalar_one()
        )
        total_filtered = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(QualityAssessmentReceiptRow)
                    .outerjoin(QualityAssessmentHeadRow, head_join)
                    .where(*filtered_conditions)
                )
            ).scalar_one()
        )
        page_conditions = list(filtered_conditions)
        if query.cursor is not None:
            page_conditions.append(
                or_(
                    QualityAssessmentReceiptRow.created_at
                    < query.cursor.created_at,
                    and_(
                        QualityAssessmentReceiptRow.created_at
                        == query.cursor.created_at,
                        QualityAssessmentReceiptRow.id < query.cursor.item_id,
                    ),
                )
            )
        page_statement = (
            select(QualityAssessmentReceiptRow)
            .outerjoin(QualityAssessmentHeadRow, head_join)
            .where(*page_conditions)
            .order_by(
                QualityAssessmentReceiptRow.created_at.desc(),
                QualityAssessmentReceiptRow.id.desc(),
            )
        )
        if query.cursor is None:
            page_statement = page_statement.offset(query.offset)
        rows = list(
            (
                await self._session.execute(
                    page_statement.limit(query.limit)
                )
            )
            .scalars()
            .all()
        )
        heads = {
            AssessmentKind(row.assessment_kind): row.receipt_id
            for row in (
                (
                    await self._session.execute(
                        select(QualityAssessmentHeadRow).where(
                            QualityAssessmentHeadRow.board_id == subject.board_id,
                            QualityAssessmentHeadRow.subject_type
                            == subject.subject_type.value,
                            QualityAssessmentHeadRow.subject_id == subject.subject_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        current_subject = AssessmentSubjectRef(
            board_id=subject.board_id,
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
            subject_version=query.current_subject_version,
        )
        input_by_identity = {
            (item.assessment_kind, item.origin, item.source): item.digests
            for item in query.currentness_inputs
        }
        projected: list[AssessmentReceiptView] = []
        for receipt in map(_receipt_from_row, rows):
            current_digests = (
                input_by_identity.get(
                    (
                        receipt.assessment_kind,
                        receipt.origin,
                        receipt.source,
                    )
                )
                if input_by_identity
                else query.current_digests
            )
            if current_digests is None:
                raise QualityAssessmentPersistenceError(
                    "assessment_current_projection_identity_unsupported"
                )
            projected.append(
                project_assessment_receipt_view(
                    receipt,
                    head_receipt_id=heads.get(receipt.assessment_kind),
                    current_subject=current_subject,
                    current_digests=current_digests,
                )
            )
        items = tuple(projected)
        return QualityPage(
            items=items,
            total_filtered=total_filtered,
            total_overall=total_overall,
            offset=query.offset,
            limit=query.limit,
        )

    async def list_findings(
        self,
        query: FindingListQuery,
    ) -> QualityPage[QualityFinding]:
        started_at = perf_counter()
        try:
            page = await self._list_findings_unobserved(query)
        except Exception:
            observe_ska_projection_queries(
                surface="quality_findings",
                subject_type=query.subject_type.value,
                query_count=3,
                duration_ms=(perf_counter() - started_at) * 1000,
                payload_bytes=0,
                outcome="error",
            )
            raise
        observe_ska_projection_queries(
            surface="quality_findings",
            subject_type=query.subject_type.value,
            query_count=3,
            duration_ms=(perf_counter() - started_at) * 1000,
            payload_bytes=0,
            outcome="success",
        )
        return page

    async def _list_findings_unobserved(
        self,
        query: FindingListQuery,
    ) -> QualityPage[QualityFinding]:
        base_filters = (
            QualityFindingRow.board_id == query.board_id,
            QualityFindingRow.subject_type == query.subject_type.value,
            QualityFindingRow.subject_id == query.subject_id,
        )
        filters = list(base_filters)
        if query.receipt_id is not None:
            filters.append(QualityFindingRow.receipt_id == query.receipt_id)
        if query.assessment_kind is not None:
            filters.append(
                QualityFindingRow.assessment_kind == query.assessment_kind.value
            )
        if query.category_code is not None:
            filters.append(QualityFindingRow.category_code == query.category_code)
        if query.severity is not None:
            filters.append(QualityFindingRow.severity == query.severity.value)
        total_overall = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(QualityFindingRow)
                    .where(*base_filters)
                )
            ).scalar_one()
        )
        total_filtered = int(
            (
                await self._session.execute(
                    select(func.count()).select_from(QualityFindingRow).where(*filters)
                )
            ).scalar_one()
        )
        page_filters = list(filters)
        if query.cursor is not None:
            page_filters.append(
                or_(
                    QualityFindingRow.created_at < query.cursor.created_at,
                    and_(
                        QualityFindingRow.created_at == query.cursor.created_at,
                        QualityFindingRow.id < query.cursor.item_id,
                    ),
                )
            )
        page_statement = (
            select(QualityFindingRow)
            .where(*page_filters)
            .order_by(
                QualityFindingRow.created_at.desc(),
                QualityFindingRow.id.desc(),
            )
        )
        if query.cursor is None:
            page_statement = page_statement.offset(query.offset)
        rows = (
            (
                await self._session.execute(
                    page_statement.limit(query.limit)
                )
            )
            .scalars()
            .all()
        )
        return QualityPage(
            items=tuple(_finding_from_row(row) for row in rows),
            total_filtered=total_filtered,
            total_overall=total_overall,
            offset=query.offset,
            limit=query.limit,
        )


__all__ = [
    "AuthorityDigestResolver",
    "CommunitySqlAlchemyQualityAssessment",
    "CommunitySqlAlchemyQualityAssessmentPreflightReader",
    "InputDigestResolver",
    "resolve_quality_assessment_authority",
    "resolve_quality_assessment_input_digests",
]
