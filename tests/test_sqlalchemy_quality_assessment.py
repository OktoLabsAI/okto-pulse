"""Real-SQLite contract tests for the Community D0 quality adapter."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Import the service before requirement_lint's port module.  Core's broad
# services package exports otherwise make direct cold imports order-sensitive.
from okto_pulse.core.services.quality_assessment import QualityAssessmentService
from okto_pulse.core.application.domain_event_delivery import (
    event_from_stored,
)

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Base,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
    Ideation,
    IdeationHistory,
    IdeationQAItem,
    QualityAssessmentHeadRow,
    QualityAssessmentOutboxRow,
    QualityAssessmentReceiptRow,
    QualityFindingRow,
    QualityProposedQuestionRow,
    Refinement,
    RefinementHistory,
    RefinementQAItem,
    Spec,
    SpecHistory,
    SpecQAItem,
)
from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
    CommunitySqlAlchemyQualityAssessment,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.api.quality_summary_projection import (
    load_quality_summaries_for_page,
    quality_summary_field,
)
from okto_pulse.core.domain.enums import (
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
)
from okto_pulse.core.domain.quality_assessment import (
    AssessmentAnchorCatalog,
    AssessmentAuthoritySnapshot,
    AssessmentDigestSet,
    AssessmentKind,
    AssessmentOrigin,
    AssessmentPreflight,
    AssessmentReceiptState,
    AssessmentScale,
    AssessmentScaleKind,
    AssessmentSubjectIdentity,
    AssessmentSubjectRef,
    AssessmentSubjectType,
    AssessmentSubmission,
    AssessmentVersionSet,
    EvidenceRef,
    FindingAnchor,
    FindingAnchorType,
    FindingSeverity,
    ProposedQuestionDraft,
    QualityFindingDraft,
    ScoreDirection,
)
from okto_pulse.core.domain.quality_canonicalization import (
    SEMANTIC_FIELD_MANIFEST_V1,
    canonical_sha256,
    clarification_digest_v1,
    semantic_content_digest_v1,
)
from okto_pulse.core.domain.requirement_lint import RequirementLocale
from okto_pulse.core.ports.quality_assessment import (
    AssessmentAuthorityConflict,
    AssessmentHeadRevisionConflict,
    AssessmentIdempotencyConflict,
    AssessmentInputDigestConflict,
    AssessmentListQuery,
    AssessmentSubjectLifecycleConflict,
    AssessmentSubjectStatusConflict,
    AssessmentSubjectVersionConflict,
    FindingListQuery,
    QualityAssessmentPersistencePort,
)
from okto_pulse.core.ports.requirement_lint import (
    RequirementLintWriteCommand,
    RequirementLintWriter,
)
from okto_pulse.core.ports.domain_event_delivery import StoredDomainEvent
from okto_pulse.core.services.requirement_lint_assessment import (
    RequirementLintAssessmentInput,
    build_requirement_lint_assessment_bundle,
)
from okto_pulse.core.services.ska_observability import (
    reset_ska_metric_samples_for_tests,
    ska_metric_samples,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _quality_adapter_isolated_from_semantic_listeners():
    """Suspend Community semantic versioning callbacks for fixed-chain tests.

    These are ADAPTER-layer unit tests of the quality-assessment persistence
    contract built on fixed subject-version chains (seed version=7, bundles
    pinned to exact versions). Its local factory uses the production
    CommunitySemanticSession, under which every question-proposing bundle also
    bumps the owning spec's version (SpecQAItem insert => semantic change),
    turning the fixed chains stale mid-test. Version-advance staleness is
    covered explicitly by test_consolidation_projection_omits_head_stale_by_
    subject_version; here the subclass listeners are removed for each test and
    always reinstalled, so isolation is deterministic in any suite order.
    """

    from okto_pulse.community.adapters import (
        sqlalchemy_policy_subject_versioning as _psv,
    )

    pairs = (
        ("before_flush", _psv._before_flush),
        ("after_flush", _psv._after_flush_collect_new_subjects),
        ("after_commit", _psv._mark_transaction_committed),
        ("after_transaction_end", _psv._finish_transaction_markers),
    )
    removed = []
    for hook, listener in pairs:
        if event.contains(_psv.CommunitySemanticSession, hook, listener):
            event.remove(_psv.CommunitySemanticSession, hook, listener)
            removed.append((hook, listener))
    try:
        yield
    finally:
        for hook, listener in removed:
            event.listen(_psv.CommunitySemanticSession, hook, listener)


NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)
BOARD_ID = "board-quality"
OTHER_BOARD_ID = "board-other"
SPEC_ID = "spec-quality"


class _Ids:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._counter = 0

    def __call__(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._namespace}_{self._counter}"


def _spec_payload() -> dict[str, Any]:
    return {
        "id": SPEC_ID,
        "board_id": BOARD_ID,
        "version": 7,
        "title": "Quality persistence",
        "description": "Persist the complete D0 contract.",
        "context": "SK-A",
        "functional_requirements": [
            {
                "id": "fr-vague",
                "text": "O fluxo deve ser fácil.",
                "status": "active",
                "locale": "pt",
            }
        ],
        "technical_requirements": [],
        "acceptance_criteria": [],
        "test_scenarios": [],
        "business_rules": [],
        "api_contracts": [],
        "integration_requirements": [],
        "observability_requirements": [],
        "decisions": [],
    }


def _lint_authority(actor: str = "agent-quality") -> AssessmentAuthoritySnapshot:
    return AssessmentAuthoritySnapshot(
        domain_write=True,
        quality_assess=False,
        qa_ask=False,
        reviewer_separation_satisfied=True,
        authority_digest=canonical_sha256(
            {"authority": "semantic-writer", "actor": actor}
        ),
    )


def _lint_bundle(
    *,
    namespace: str,
    actor: str = "agent-quality",
    locale: RequirementLocale = RequirementLocale.UNKNOWN,
    payload: dict[str, Any] | None = None,
    spec_version: int = 7,
    head_revision: int = 0,
    head_receipt_id: str | None = None,
    now: datetime = NOW,
):
    payload = _spec_payload() if payload is None else payload
    command = RequirementLintWriteCommand(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        spec_version=spec_version,
        actor_id=actor,
        writer=RequirementLintWriter.BULK_UPDATE,
        spec_status="in_progress",
        spec_archived=False,
        changed_fields=("functional_requirements",),
        spec_payload=payload,
    )
    return build_requirement_lint_assessment_bundle(
        RequirementLintAssessmentInput(
            command=command,
            authority=_lint_authority(actor),
            qa_items=(),
            default_locale=locale,
            current_head_revision=head_revision,
            current_head_receipt_id=head_receipt_id,
        ),
        quality_service=QualityAssessmentService(
            id_factory=_Ids(namespace),
            clock=lambda: now,
        ),
    )


def _adapter(
    session: AsyncSession,
) -> CommunitySqlAlchemyQualityAssessment:
    return CommunitySqlAlchemyQualityAssessment(
        session,
        authority_resolver=lambda _session, bundle: (
            _lint_authority(bundle.receipt.created_by)
            if bundle.receipt.assessment_kind is AssessmentKind.REQUIREMENT_LINT
            else _manual_authority()
        ),
        input_digest_resolver=lambda _session, bundle: bundle.receipt.digests,
    )


def _manual_authority() -> AssessmentAuthoritySnapshot:
    return AssessmentAuthoritySnapshot(
        domain_write=True,
        quality_assess=True,
        qa_ask=True,
        authority_digest=canonical_sha256("manual-quality-authority"),
    )


def _manual_bundle(
    *,
    subject_type: AssessmentSubjectType,
    subject_id: str,
    status: str,
    payload: dict[str, Any],
    namespace: str,
    now: datetime,
):
    digests = AssessmentDigestSet(
        content_digest=semantic_content_digest_v1(subject_type, payload),
        clarification_digest=clarification_digest_v1(()),
        ruleset_digest=canonical_sha256("manual-ruleset"),
        taxonomy_digest=canonical_sha256("manual-taxonomy"),
        policy_digest=canonical_sha256("manual-policy"),
    )
    versions = AssessmentVersionSet(
        ruleset_version="manual-ruleset/v1",
        taxonomy_version="manual-taxonomy/v1",
        analyzer_version="manual-analyzer/v1",
        policy_version="manual-policy/v1",
    )
    subject = AssessmentSubjectRef(
        board_id=BOARD_ID,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_version=3,
    )
    category = "functional_scope_behavior"
    finding_key = f"manual:{subject_type.value}:{subject_id}"
    submission = AssessmentSubmission(
        board_id=BOARD_ID,
        subject_type=subject_type,
        subject_id=subject_id,
        assessment_kind=AssessmentKind.AMBIGUITY,
        idempotency_key=f"manual:{namespace}",
        expected_subject_version=3,
        expected_head_revision=0,
        score=2,
        justification="One pinpointed ambiguity remains.",
        scale=AssessmentScale(
            kind=AssessmentScaleKind.AMBIGUITY_SCORE,
            minimum=1,
            maximum=5,
            direction=ScoreDirection.LOWER_BETTER,
        ),
        findings=(
            QualityFindingDraft(
                finding_key=finding_key,
                category_code=category,
                severity=FindingSeverity.HIGH,
                confidence=0.91,
                deterministic=False,
                blocking_eligible=False,
                title="Ambiguous behavior",
                detail="The expected behavior is not explicit.",
                remediation="Clarify the expected observable result.",
                rule_code="manual_behavior",
                anchor=FindingAnchor(
                    board_id=BOARD_ID,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    subject_version=3,
                    input_digest=digests.input_digest or "",
                    anchor_type=FindingAnchorType.FIELD,
                    anchor_ref="description",
                    excerpt_hash=canonical_sha256("ambiguous excerpt"),
                ),
                evidence_refs=(
                    EvidenceRef(
                        source_type="knowledge_base",
                        source_id="kb-source",
                        source_version=2,
                        content_hash=canonical_sha256("source content"),
                    ),
                ),
            ),
        ),
        proposed_questions=(
            ProposedQuestionDraft(
                client_key=f"question:{namespace}",
                question="Which observable result is required?",
                question_type="choice",
                choices=("Result A", "Result B"),
                allow_free_text=True,
                category_code=category,
                finding_keys=(finding_key,),
            ),
        ),
    )
    return QualityAssessmentService(
        id_factory=_Ids(namespace),
        clock=lambda: now,
    ).prepare_submission(
        submission,
        actor_id="reviewer-quality",
        preflight=AssessmentPreflight(
            subject=subject,
            status=status,
            current_head_revision=0,
            current_head_receipt_id=None,
            channel="mcp:quality_assess",
            expected_scale=submission.scale,
            digests=digests,
            versions=versions,
            anchors=AssessmentAnchorCatalog(
                fields=frozenset(SEMANTIC_FIELD_MANIFEST_V1[subject_type.value]),
            ),
            allowed_category_codes=frozenset({category}),
            authority=_manual_authority(),
            origin=AssessmentOrigin.HUMAN_OR_AGENT,
        ),
    )


async def _schema_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    install_community_sqlite_pragmas(engine)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine


async def test_parent_summary_permission_denial_is_omission_without_quality_io() -> (
    None
):
    reset_ska_metric_samples_for_tests()
    services = SimpleNamespace(
        resolve_user_permissions=AsyncMock(return_value=[]),
    )
    summaries = await load_quality_summaries_for_page(
        uow=SimpleNamespace(services=services),
        user_id="reader-without-quality",
        board_id=BOARD_ID,
        subject_type="spec",
        subject_ids=(SPEC_ID,),
    )

    assert summaries is None
    assert quality_summary_field(SPEC_ID, summaries) == {}
    assert quality_summary_field(SPEC_ID, {}) == {"quality_summaries": {}}
    services.resolve_user_permissions.assert_awaited_once_with(
        "reader-without-quality",
        BOARD_ID,
    )
    sample = ska_metric_samples()[-1]
    assert sample["metric_name"] == "pulse_ska_projection_queries_total"
    assert sample["value"] == 0
    assert sample["surface"] == "parent_summary"
    assert sample["subject_type"] == "spec"
    assert sample["outcome"] == "success"
    assert sample["duration_ms"] >= 0
    assert sample["payload_bytes"] == 0


@pytest.fixture
async def rig(tmp_path: Path):
    engine = await _schema_engine(tmp_path / "quality.db")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    payload = _spec_payload()
    async with factory() as session:
        session.add_all(
            [
                Board(
                    id=BOARD_ID,
                    name="Quality",
                    owner_id="owner",
                ),
                Board(
                    id=OTHER_BOARD_ID,
                    name="Other",
                    owner_id="owner",
                ),
                Spec(
                    id=SPEC_ID,
                    board_id=BOARD_ID,
                    title=payload["title"],
                    description=payload["description"],
                    context=payload["context"],
                    functional_requirements=payload["functional_requirements"],
                    technical_requirements=payload["technical_requirements"],
                    acceptance_criteria=payload["acceptance_criteria"],
                    test_scenarios=payload["test_scenarios"],
                    business_rules=payload["business_rules"],
                    api_contracts=payload["api_contracts"],
                    integration_requirements=payload["integration_requirements"],
                    observability_requirements=payload["observability_requirements"],
                    decisions=payload["decisions"],
                    status=SpecStatus.IN_PROGRESS,
                    version=7,
                    created_by="owner",
                ),
            ]
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_parent_summary_projects_not_started_for_subject_without_receipts(
    rig,
) -> None:
    async with rig() as session:
        statement_count = 0

        def count_statement(*_args) -> None:
            nonlocal statement_count
            statement_count += 1

        event.listen(
            session.bind.sync_engine,
            "before_cursor_execute",
            count_statement,
        )
        try:
            summaries = await load_quality_summaries_for_page(
                uow=SimpleNamespace(
                    services=SimpleNamespace(
                        resolve_user_permissions=AsyncMock(
                            return_value=["spec.quality.read"]
                        ),
                        cards=SimpleNamespace(db=session),
                    )
                ),
                user_id="quality-reader",
                board_id=BOARD_ID,
                subject_type="spec",
                subject_ids=(SPEC_ID,),
            )
        finally:
            event.remove(
                session.bind.sync_engine,
                "before_cursor_execute",
                count_statement,
            )

    assert statement_count == 1
    assert summaries == {
        SPEC_ID: {
            "requirement_lint": {
                "edition": 1,
                "state": "not_started",
                "previous_count": 0,
                "current_result": None,
            },
            "spec_validation": {
                "edition": 1,
                "state": "not_started",
                "previous_count": 0,
                "current_result": None,
            },
        }
    }


async def test_semantic_spec_writes_never_run_requirement_lint_in_community(
    rig,
) -> None:
    """External evidence submission is the only Community lint write path."""

    async with rig() as session:
        for version in (8, 9, 10):
            spec = await session.get(Spec, SPEC_ID)
            assert spec is not None
            spec.version = version
            spec.description = f"Semantic revision {version}"
            await session.commit()
        assert await _count(session, QualityAssessmentReceiptRow) == 0
        assert await _count(session, QualityAssessmentHeadRow) == 0
        assert await _count(session, QualityFindingRow) == 0


async def _count(session: AsyncSession, model: type) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
    )


async def test_round_trip_audit_projection_pagination_and_board_isolation(
    rig,
) -> None:
    first = _lint_bundle(namespace="first")
    second = _lint_bundle(
        namespace="second",
        locale=RequirementLocale.EN,
        head_revision=1,
        head_receipt_id=first.receipt.id,
        # Deliberate timestamp tie: list order must fall back to id DESC.
        now=NOW,
    )
    async with rig() as session:
        adapter = _adapter(session)
        first_result = await adapter.apply_bundle_cas(first)
        assert not first_result.replayed
        assert session.in_transaction()
        await session.commit()

    async with rig() as session:
        reset_ska_metric_samples_for_tests()
        statement_count = 0

        def _count_summary_statements(*_args) -> None:
            nonlocal statement_count
            statement_count += 1

        event.listen(
            session.bind.sync_engine,
            "before_cursor_execute",
            _count_summary_statements,
        )
        try:
            summaries = await load_quality_summaries_for_page(
                uow=SimpleNamespace(
                    services=SimpleNamespace(
                        resolve_user_permissions=AsyncMock(
                            return_value=["spec.quality.read"]
                        ),
                        cards=SimpleNamespace(db=session),
                    )
                ),
                user_id="quality-reader",
                board_id=BOARD_ID,
                subject_type="spec",
                subject_ids=(SPEC_ID,),
            )
        finally:
            event.remove(
                session.bind.sync_engine,
                "before_cursor_execute",
                _count_summary_statements,
            )
        assert statement_count == 1
        sample = ska_metric_samples()[-1]
        assert sample["surface"] == "parent_summary"
        assert sample["subject_type"] == "spec"
        assert sample["outcome"] == "success"
        assert sample["value"] == 1
        assert sample["duration_ms"] >= 0
        assert sample["payload_bytes"] > 0
        assert summaries == {
            SPEC_ID: {
                "requirement_lint": {
                    "edition": 1,
                    "state": "not_started",
                    # The NULL-edition receipt is legacy history, never the
                    # current lifecycle result.
                    "previous_count": 1,
                    "current_result": None,
                },
                "spec_validation": {
                    "edition": 1,
                    "state": "not_started",
                    "previous_count": 0,
                    "current_result": None,
                },
            }
        }

    async with rig() as session:
        adapter = _adapter(session)
        second_result = await adapter.apply_bundle_cas(second)
        assert second_result.head_revision == 2
        assert session.in_transaction()
        await session.commit()

    async with rig() as session:
        adapter = _adapter(session)
        detail = await adapter.get_receipt_detail(
            board_id=BOARD_ID,
            receipt_id=first.receipt.id,
        )
        assert detail is not None
        assert detail.receipt == first.receipt
        assert detail.findings == first.findings
        assert detail.proposed_questions == first.proposed_questions
        assert detail.finding_qa_links == first.finding_qa_links
        assert detail.findings[0].anchor.excerpt_hash is not None

        current = await adapter.get_current(
            board_id=BOARD_ID,
            subject_type=AssessmentSubjectType.SPEC,
            subject_id=SPEC_ID,
            assessment_kind=AssessmentKind.REQUIREMENT_LINT,
        )
        assert current is not None
        assert current[0] == second.receipt
        assert current[1] == second.next_head

        page = await adapter.list_assessments(
            AssessmentListQuery(
                subject=AssessmentSubjectIdentity(
                    board_id=BOARD_ID,
                    subject_type=AssessmentSubjectType.SPEC,
                    subject_id=SPEC_ID,
                ),
                offset=0,
                limit=1,
                current_subject_version=7,
                current_digests=second.receipt.digests,
            )
        )
        assert page.total_overall == page.total_filtered == 2
        assert page.items[0].receipt.id == second.receipt.id
        assert page.items[0].state is AssessmentReceiptState.CURRENT
        adjacent = await adapter.list_assessments(
            replace(
                AssessmentListQuery(
                    subject=AssessmentSubjectIdentity(
                        board_id=BOARD_ID,
                        subject_type=AssessmentSubjectType.SPEC,
                        subject_id=SPEC_ID,
                    ),
                    offset=0,
                    limit=1,
                    current_subject_version=7,
                    current_digests=second.receipt.digests,
                ),
                offset=1,
            )
        )
        assert adjacent.items[0].receipt.id == first.receipt.id
        assert adjacent.items[0].state is AssessmentReceiptState.SUPERSEDED

        current_only = await adapter.list_assessments(
            AssessmentListQuery(
                subject=AssessmentSubjectIdentity(
                    board_id=BOARD_ID,
                    subject_type=AssessmentSubjectType.SPEC,
                    subject_id=SPEC_ID,
                ),
                offset=0,
                limit=10,
                state=AssessmentReceiptState.CURRENT,
                current_subject_version=7,
                current_digests=second.receipt.digests,
            )
        )
        assert current_only.total_filtered == 1
        assert current_only.total_overall == 2
        changed_digests = AssessmentDigestSet(
            content_digest=canonical_sha256("changed semantic content"),
            clarification_digest=second.receipt.digests.clarification_digest,
            ruleset_digest=second.receipt.digests.ruleset_digest,
            taxonomy_digest=second.receipt.digests.taxonomy_digest,
            policy_digest=second.receipt.digests.policy_digest,
        )
        stale_only = await adapter.list_assessments(
            AssessmentListQuery(
                subject=AssessmentSubjectIdentity(
                    board_id=BOARD_ID,
                    subject_type=AssessmentSubjectType.SPEC,
                    subject_id=SPEC_ID,
                ),
                offset=0,
                limit=10,
                state=AssessmentReceiptState.STALE,
                current_subject_version=7,
                current_digests=changed_digests,
            )
        )
        assert stale_only.total_filtered == 1
        assert stale_only.items[0].receipt.id == second.receipt.id
        assert stale_only.items[0].freshness.stale_reasons

        finding_page = await adapter.list_findings(
            FindingListQuery(
                board_id=BOARD_ID,
                subject_type=AssessmentSubjectType.SPEC,
                subject_id=SPEC_ID,
                receipt_id=first.receipt.id,
                offset=0,
                limit=10,
            )
        )
        assert finding_page.items == first.findings
        assert finding_page.total_filtered == len(first.findings)
        assert finding_page.total_overall == (
            len(first.findings) + len(second.findings)
        )
        severity_page = await adapter.list_findings(
            FindingListQuery(
                board_id=BOARD_ID,
                subject_type=AssessmentSubjectType.SPEC,
                subject_id=SPEC_ID,
                category_code=first.findings[0].category_code,
                severity=first.findings[0].severity,
                offset=0,
                limit=1,
            )
        )
        assert severity_page.total_filtered == severity_page.total_overall == 2
        assert severity_page.items[0].receipt_id == second.receipt.id
        severity_adjacent = await adapter.list_findings(
            FindingListQuery(
                board_id=BOARD_ID,
                subject_type=AssessmentSubjectType.SPEC,
                subject_id=SPEC_ID,
                category_code=first.findings[0].category_code,
                severity=first.findings[0].severity,
                offset=1,
                limit=1,
            )
        )
        assert severity_adjacent.items[0].receipt_id == first.receipt.id

        assert (
            await adapter.get_receipt(
                board_id=OTHER_BOARD_ID,
                receipt_id=first.receipt.id,
            )
            is None
        )
        isolated = await adapter.list_assessments(
            AssessmentListQuery(
                subject=AssessmentSubjectIdentity(
                    board_id=OTHER_BOARD_ID,
                    subject_type=AssessmentSubjectType.SPEC,
                    subject_id=SPEC_ID,
                ),
                offset=0,
                limit=10,
                current_subject_version=7,
                current_digests=second.receipt.digests,
            )
        )
        assert isolated.total_overall == isolated.total_filtered == 0

        qa_row = await session.get(
            SpecQAItem,
            first.proposed_questions[0].qa_id,
        )
        # Requirement Lint records proposed-question evidence, but Community
        # must not materialize it as an owned Spec QA item automatically.
        assert qa_row is None
        receipt_row = await session.get(
            QualityAssessmentReceiptRow,
            first.receipt.id,
        )
        assert receipt_row is not None
        event_row = await session.get(DomainEventRow, receipt_row.event_id)
        assert event_row is not None
        assert event_row.payload_json["event_schema_version"] == 1
        assert "board_id" not in event_row.payload_json
        reconstructed = event_from_stored(
            StoredDomainEvent(
                event_id=event_row.id,
                event_type=event_row.event_type,
                board_id=event_row.board_id,
                actor_id=event_row.actor_id,
                actor_type=event_row.actor_type,
                occurred_at=event_row.occurred_at,
                payload=dict(event_row.payload_json),
            )
        )
        assert reconstructed.subject_id == SPEC_ID
        execution = await session.scalar(
            select(DomainEventHandlerExecution).where(
                DomainEventHandlerExecution.event_id == receipt_row.event_id,
                DomainEventHandlerExecution.handler_name == "ConsolidationEnqueuer",
            )
        )
        assert execution is not None
        assert execution.status == "pending"
        assert await session.get(ActivityLog, receipt_row.history_id)
        assert await session.get(SpecHistory, receipt_row.history_id)
        assert await session.get(
            QualityAssessmentOutboxRow,
            receipt_row.outbox_id,
        )


async def test_idempotent_replay_and_fingerprint_conflict(rig) -> None:
    bundle = _lint_bundle(namespace="replay")
    async with rig() as session:
        await _adapter(session).apply_bundle_cas(bundle)
        await session.commit()

    rebuilt = _lint_bundle(
        namespace="rebuilt",
        now=NOW + timedelta(hours=1),
    )
    assert rebuilt.idempotency_key == bundle.idempotency_key
    assert rebuilt.request_fingerprint == bundle.request_fingerprint
    assert rebuilt.receipt.id != bundle.receipt.id
    assert rebuilt.audit_intent.event_id != bundle.audit_intent.event_id
    assert rebuilt.proposed_questions[0].qa_id != (bundle.proposed_questions[0].qa_id)
    async with rig() as session:
        result = await _adapter(session).apply_bundle_cas(rebuilt)
        assert result.replayed
        assert result.receipt_id == bundle.receipt.id
        assert result.event_id == bundle.audit_intent.event_id
        assert result.qa_ids == {
            item.client_key: item.qa_id for item in bundle.proposed_questions
        }
        assert await _count(session, QualityAssessmentReceiptRow) == 1
        assert await _count(session, QualityAssessmentHeadRow) == 1
        assert await _count(session, SpecQAItem) == 0
        head = (await session.execute(select(QualityAssessmentHeadRow))).scalar_one()
        assert head.revision == 1
        assert head.receipt_id == bundle.receipt.id

    conflicting = _lint_bundle(
        namespace="conflict",
        actor="other-agent",
    )
    assert conflicting.idempotency_key == bundle.idempotency_key
    assert conflicting.request_fingerprint != bundle.request_fingerprint
    async with rig() as session:
        with pytest.raises(AssessmentIdempotencyConflict):
            await _adapter(session).apply_bundle_cas(conflicting)


async def test_fail_closed_authority_input_and_subject_fences(rig) -> None:
    bundle = _lint_bundle(namespace="fences")

    async with rig() as session:
        with pytest.raises(AssessmentAuthorityConflict):
            await CommunitySqlAlchemyQualityAssessment(
                session,
                input_digest_resolver=lambda _session, value: value.receipt.digests,
            ).apply_bundle_cas(bundle)
        await session.rollback()

    async with rig() as session:
        with pytest.raises(AssessmentAuthorityConflict):
            await CommunitySqlAlchemyQualityAssessment(
                session,
                authority_resolver=lambda _session, _value: replace(
                    _lint_authority(),
                    authority_digest=canonical_sha256("revoked authority"),
                ),
                input_digest_resolver=lambda _session, value: value.receipt.digests,
            ).apply_bundle_cas(bundle)
        await session.rollback()

    async with rig() as session:
        with pytest.raises(AssessmentInputDigestConflict):
            await CommunitySqlAlchemyQualityAssessment(
                session,
                authority_resolver=lambda _session, value: _lint_authority(
                    value.receipt.created_by
                ),
            ).apply_bundle_cas(bundle)
        await session.rollback()

    async with rig() as session:
        await session.execute(update(Spec).where(Spec.id == SPEC_ID).values(version=8))
        await session.commit()
        with pytest.raises(AssessmentSubjectVersionConflict):
            await _adapter(session).apply_bundle_cas(bundle)
        await session.rollback()
        await session.execute(update(Spec).where(Spec.id == SPEC_ID).values(version=7))
        await session.commit()

        await session.execute(
            update(Spec).where(Spec.id == SPEC_ID).values(status=SpecStatus.DONE)
        )
        await session.commit()
        with pytest.raises(AssessmentSubjectStatusConflict):
            await _adapter(session).apply_bundle_cas(bundle)
        await session.rollback()
        await session.execute(
            update(Spec).where(Spec.id == SPEC_ID).values(status=SpecStatus.IN_PROGRESS)
        )
        await session.commit()

        await session.execute(
            update(Spec).where(Spec.id == SPEC_ID).values(archived=True)
        )
        await session.commit()
        with pytest.raises(AssessmentSubjectLifecycleConflict):
            await _adapter(session).apply_bundle_cas(bundle)
        await session.rollback()
        await session.execute(
            update(Spec).where(Spec.id == SPEC_ID).values(archived=False)
        )
        await session.commit()

        await session.execute(
            update(Spec)
            .where(Spec.id == SPEC_ID)
            .values(description="Changed without a version bump")
        )
        await session.commit()
        with pytest.raises(AssessmentInputDigestConflict):
            await _adapter(session).apply_bundle_cas(bundle)


async def test_lifecycle_winner_rejects_stale_quality_writer_with_zero_rows(
    rig,
) -> None:
    bundle = _lint_bundle(namespace="lifecycle-winner")
    quality_reached_fence = asyncio.Event()

    class _ObservedQualityAdapter(CommunitySqlAlchemyQualityAssessment):
        async def _fence_subject(self, candidate):
            quality_reached_fence.set()
            return await super()._fence_subject(candidate)

    async with rig() as quality_session, rig() as lifecycle_session:
        preloaded = await quality_session.get(Spec, SPEC_ID)
        assert preloaded is not None
        assert preloaded.status is SpecStatus.IN_PROGRESS

        # Hold the production-WAL writer mutex with the lifecycle winner while
        # the quality command, built from the preloaded state, reaches its own
        # subject fence.  The quality task must wait, refresh after the winner
        # commits, and reject before staging any append-only rows.
        await lifecycle_session.execute(
            update(Spec)
            .where(Spec.id == SPEC_ID)
            .values(status=SpecStatus.DONE)
        )

        adapter = _ObservedQualityAdapter(
            quality_session,
            authority_resolver=lambda _session, value: _lint_authority(
                value.receipt.created_by
            ),
            input_digest_resolver=lambda _session, value: value.receipt.digests,
        )

        async def _write_stale_quality_bundle():
            try:
                return await adapter.apply_bundle_cas(bundle)
            except (
                AssessmentSubjectStatusConflict,
                AssessmentSubjectVersionConflict,
            ) as exc:
                await quality_session.rollback()
                return exc

        quality_task = asyncio.create_task(_write_stale_quality_bundle())
        await asyncio.wait_for(quality_reached_fence.wait(), timeout=2)
        assert not quality_task.done()

        await lifecycle_session.commit()
        outcome = await asyncio.wait_for(quality_task, timeout=5)
        assert isinstance(outcome, AssessmentSubjectStatusConflict)

    async with rig() as verify:
        assert await _count(verify, QualityAssessmentReceiptRow) == 0
        assert await _count(verify, QualityAssessmentHeadRow) == 0
        assert await _count(verify, QualityFindingRow) == 0
        assert await _count(verify, QualityProposedQuestionRow) == 0
        assert await _count(verify, QualityAssessmentOutboxRow) == 0
        assert await _count(verify, DomainEventRow) == 0
        assert await _count(verify, DomainEventHandlerExecution) == 0
        assert await _count(verify, ActivityLog) == 0
        assert await _count(verify, SpecHistory) == 0


async def test_stale_head_and_real_head_cas_rollback(rig) -> None:
    first = _lint_bundle(namespace="head-first")
    stale = _lint_bundle(
        namespace="head-stale",
        locale=RequirementLocale.PT,
    )
    async with rig() as session:
        await _adapter(session).apply_bundle_cas(first)
        await session.commit()

    async with rig() as session:
        with pytest.raises(AssessmentHeadRevisionConflict):
            await _adapter(session).apply_bundle_cas(stale)
        await session.rollback()
        head = (await session.execute(select(QualityAssessmentHeadRow))).scalar_one()
        assert head.receipt_id == first.receipt.id
        assert head.revision == 1
        assert await _count(session, QualityAssessmentReceiptRow) == 1


async def test_concurrent_initial_writers_produce_one_head_winner(rig) -> None:
    first = _lint_bundle(namespace="race-first")
    second = _lint_bundle(
        namespace="race-second",
        locale=RequirementLocale.EN,
        now=NOW + timedelta(microseconds=1),
    )

    async def _write(bundle):
        async with rig() as session:
            try:
                result = await _adapter(session).apply_bundle_cas(bundle)
            except AssessmentHeadRevisionConflict as exc:
                await session.rollback()
                return exc
            await session.commit()
            return result

    outcomes = await asyncio.gather(_write(first), _write(second))
    assert (
        sum(isinstance(item, AssessmentHeadRevisionConflict) for item in outcomes) == 1
    )
    assert (
        sum(not isinstance(item, AssessmentHeadRevisionConflict) for item in outcomes)
        == 1
    )
    async with rig() as session:
        assert await _count(session, QualityAssessmentReceiptRow) == 1
        assert await _count(session, QualityAssessmentHeadRow) == 1
        assert await _count(session, QualityAssessmentOutboxRow) == 1


async def test_caller_rollback_and_fault_injection_are_atomic(rig) -> None:
    bundle = _lint_bundle(namespace="rollback")
    async with rig() as session:
        await _adapter(session).apply_bundle_cas(bundle)
        assert await _count(session, QualityAssessmentReceiptRow) == 1
        await session.rollback()
        assert await _count(session, QualityAssessmentReceiptRow) == 0
        assert await _count(session, QualityAssessmentHeadRow) == 0
        assert await _count(session, QualityFindingRow) == 0
        assert await _count(session, QualityProposedQuestionRow) == 0
        assert await _count(session, SpecQAItem) == 0
        assert await _count(session, ActivityLog) == 0
        assert await _count(session, DomainEventRow) == 0
        assert await _count(session, DomainEventHandlerExecution) == 0
        assert await _count(session, QualityAssessmentOutboxRow) == 0

    class _FaultingAdapter(CommunitySqlAlchemyQualityAssessment):
        def _stage_audit_rows(self, bundle) -> None:
            del bundle
            raise RuntimeError("injected_after_head")

    async with rig() as session:
        adapter = _FaultingAdapter(
            session,
            authority_resolver=lambda _session, value: _lint_authority(
                value.receipt.created_by
            ),
            input_digest_resolver=lambda _session, value: value.receipt.digests,
        )
        with pytest.raises(RuntimeError, match="injected_after_head"):
            await adapter.apply_bundle_cas(bundle)
        await session.rollback()
        assert await _count(session, QualityAssessmentReceiptRow) == 0
        assert await _count(session, QualityAssessmentHeadRow) == 0
        assert await _count(session, SpecQAItem) == 0

    class _ExecutionFaultingAdapter(CommunitySqlAlchemyQualityAssessment):
        def _stage_handler_execution(self, bundle) -> None:
            del bundle
            raise RuntimeError("injected_after_event_outbox")

    async with rig() as session:
        adapter = _ExecutionFaultingAdapter(
            session,
            authority_resolver=lambda _session, value: _lint_authority(
                value.receipt.created_by
            ),
            input_digest_resolver=lambda _session, value: value.receipt.digests,
        )
        with pytest.raises(RuntimeError, match="injected_after_event_outbox"):
            await adapter.apply_bundle_cas(bundle)
        await session.rollback()
        assert await _count(session, QualityAssessmentReceiptRow) == 0
        assert await _count(session, QualityAssessmentHeadRow) == 0
        assert await _count(session, DomainEventRow) == 0
        assert await _count(session, DomainEventHandlerExecution) == 0
        assert await _count(session, QualityAssessmentOutboxRow) == 0


async def test_materializes_ideation_and_refinement_qa_losslessly(rig) -> None:
    ideation_payload = {
        "title": "Ambiguous idea",
        "description": "Something useful",
        "problem_statement": "A broad problem",
        "proposed_approach": "An open approach",
    }
    refinement_payload = {
        "title": "Ambiguous refinement",
        "description": "Refine behavior",
        "in_scope": ["happy path"],
        "out_of_scope": ["migration"],
        "analysis": "Behavior needs clarification",
        "decisions": [],
    }
    async with rig() as session:
        session.add_all(
            [
                Ideation(
                    id="ideation-quality",
                    board_id=BOARD_ID,
                    status=IdeationStatus.EVALUATING,
                    version=3,
                    created_by="owner",
                    **ideation_payload,
                ),
                Refinement(
                    id="refinement-quality",
                    ideation_id="ideation-quality",
                    board_id=BOARD_ID,
                    status=RefinementStatus.APPROVED,
                    version=3,
                    created_by="owner",
                    **refinement_payload,
                ),
            ]
        )
        await session.commit()

    ideation_bundle = _manual_bundle(
        subject_type=AssessmentSubjectType.IDEATION,
        subject_id="ideation-quality",
        status="evaluating",
        payload=ideation_payload,
        namespace="ideation",
        now=NOW,
    )
    refinement_bundle = _manual_bundle(
        subject_type=AssessmentSubjectType.REFINEMENT,
        subject_id="refinement-quality",
        status="approved",
        payload=refinement_payload,
        namespace="refinement",
        now=NOW + timedelta(seconds=1),
    )
    async with rig() as session:
        adapter = _adapter(session)
        await adapter.apply_bundle_cas(ideation_bundle)
        await adapter.apply_bundle_cas(refinement_bundle)
        await session.commit()

    async with rig() as session:
        ideation_qa = await session.get(
            IdeationQAItem,
            ideation_bundle.proposed_questions[0].qa_id,
        )
        refinement_qa = await session.get(
            RefinementQAItem,
            refinement_bundle.proposed_questions[0].qa_id,
        )
        assert ideation_qa is not None
        assert refinement_qa is not None
        assert ideation_qa.ideation_id == "ideation-quality"
        assert refinement_qa.refinement_id == "refinement-quality"
        assert ideation_qa.question_type == refinement_qa.question_type == "choice"
        assert (
            ideation_qa.choices
            == refinement_qa.choices
            == [
                {
                    "id": "opt_1",
                    "label": "Result A",
                    "recommended": False,
                    "tradeoff": None,
                },
                {
                    "id": "opt_2",
                    "label": "Result B",
                    "recommended": False,
                    "tradeoff": None,
                },
            ]
        )
        assert await session.get(
            IdeationHistory,
            ideation_bundle.audit_intent.history_id,
        )
        assert await session.get(
            RefinementHistory,
            refinement_bundle.audit_intent.history_id,
        )

        detail = await _adapter(session).get_receipt_detail(
            board_id=BOARD_ID,
            receipt_id=ideation_bundle.receipt.id,
        )
        assert detail is not None
        assert detail.findings == ideation_bundle.findings
        assert detail.findings[0].evidence_refs == (
            EvidenceRef(
                source_type="knowledge_base",
                source_id="kb-source",
                source_version=2,
                content_hash=canonical_sha256("source content"),
            ),
        )
        assert detail.findings[0].anchor == (ideation_bundle.findings[0].anchor)
        assert detail.finding_qa_links == (ideation_bundle.finding_qa_links)


async def test_relational_application_seam_returns_concrete_adapter(rig) -> None:
    async with rig() as session:
        seam = CommunityRelationalApplicationAdapter(
            quality_authority_resolver=lambda _session, bundle: _lint_authority(
                bundle.receipt.created_by
            ),
            quality_input_digest_resolver=lambda _session, bundle: (
                bundle.receipt.digests
            ),
        )
        assert isinstance(
            seam.quality_assessments(session),
            CommunitySqlAlchemyQualityAssessment,
        )
        assert isinstance(
            seam.quality_assessments(session),
            QualityAssessmentPersistencePort,
        )


async def test_consolidation_projection_loads_spec_inputs_in_three_bounded_queries(
    rig,
) -> None:
    first = _lint_bundle(namespace="projection-first")
    current_payload = _spec_payload()
    current_payload["version"] = 8
    current_payload["description"] = "Persist the current D0 projection contract."
    second = _lint_bundle(
        namespace="projection-second",
        payload=current_payload,
        spec_version=8,
        head_revision=1,
        head_receipt_id=first.receipt.id,
        now=NOW + timedelta(seconds=1),
    )
    async with rig() as session:
        await _adapter(session).apply_bundle_cas(first)
        await session.commit()
    async with rig() as session:
        await session.execute(
            update(Spec)
            .where(
                Spec.id == SPEC_ID,
                Spec.board_id == BOARD_ID,
            )
            .values(
                version=8,
                description=current_payload["description"],
            )
        )
        await _adapter(session).apply_bundle_cas(second)
        await session.commit()

    async with rig() as session:
        artifact = await session.get(Spec, SPEC_ID)
        assert artifact is not None
        statements: list[str] = []

        def record_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement)

        assert session.bind is not None
        database_path = Path(str(session.bind.url.database))
        event.listen(
            session.bind.sync_engine,
            "before_cursor_execute",
            record_statement,
        )
        try:
            projection = await CommunitySqlAlchemyConsolidationPersistence().load_projection_inputs(
                session,
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=SPEC_ID,
                artifact=artifact,
            )
        finally:
            event.remove(
                session.bind.sync_engine,
                "before_cursor_execute",
                record_statement,
            )

        # The spec projection has three independent, bounded rowsets: the
        # current quality heads, their board/Q&A context, and the complete
        # dependency snapshot joined to prerequisite Specs.  Keeping the
        # dependency load separate avoids a multiplicative head x Q&A x
        # dependency join while still remaining constant for every page size.
        assert len(statements) == 3
        assert sum(
            "from spec_dependencies" in " ".join(statement.lower().split())
            for statement in statements
        ) == 1
        assert projection.research_decisions == ()
        # Legacy editionless evidence remains navigable history but cannot be
        # projected as current without inventing a human lifecycle edition.
        assert projection.quality_assessments == ()

    from okto_pulse.community.adapters.board_source_reader import (
        _current_quality_head_fingerprints,
    )
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rebuild_fingerprints = _current_quality_head_fingerprints(
            connection,
            board_id=BOARD_ID,
        )
    projection_key = (BOARD_ID, "spec", SPEC_ID)
    assert projection_key not in rebuild_fingerprints


async def test_consolidation_projection_omits_head_stale_by_subject_version(
    rig,
) -> None:
    """A CAS head is not current after its owning subject version advances."""

    bundle = _lint_bundle(namespace="projection-stale-subject-version")
    async with rig() as session:
        await _adapter(session).apply_bundle_cas(bundle)
        await session.commit()
    async with rig() as session:
        await session.execute(
            update(Spec)
            .where(
                Spec.id == SPEC_ID,
                Spec.board_id == BOARD_ID,
            )
            .values(version=8)
        )
        await session.commit()

    async with rig() as session:
        artifact = await session.get(Spec, SPEC_ID)
        assert artifact is not None
        projection = (
            await CommunitySqlAlchemyConsolidationPersistence().load_projection_inputs(
                session,
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=SPEC_ID,
                artifact=artifact,
            )
        )

    assert projection.quality_assessments == ()


async def test_consolidation_projection_omits_head_stale_by_clarification_digest(
    rig,
) -> None:
    """Answering a projected Q&A invalidates the assessment's input identity."""

    bundle = _lint_bundle(namespace="projection-stale-clarification")
    assert bundle.proposed_questions
    question_id = bundle.proposed_questions[0].qa_id
    async with rig() as session:
        await _adapter(session).apply_bundle_cas(bundle)
        await session.commit()
    async with rig() as session:
        await session.execute(
            update(SpecQAItem)
            .where(
                SpecQAItem.id == question_id,
                SpecQAItem.spec_id == SPEC_ID,
            )
            .values(
                answer="The API returns a stable validation error.",
                answered_by="reviewer-quality",
                answered_at=NOW + timedelta(minutes=1),
                revision=2,
            )
        )
        await session.commit()

    async with rig() as session:
        artifact = await session.get(Spec, SPEC_ID)
        assert artifact is not None
        projection = (
            await CommunitySqlAlchemyConsolidationPersistence().load_projection_inputs(
                session,
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=SPEC_ID,
                artifact=artifact,
            )
        )

    assert projection.quality_assessments == ()


async def test_consolidation_projection_omits_head_stale_by_policy_digest(
    rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid historical receipt under a retired policy is not current."""

    from okto_pulse.core.domain.quality_assessment import AssessmentDigestSet
    from okto_pulse.core.services import requirement_lint_assessment

    current_digest_set = (
        requirement_lint_assessment.requirement_lint_normative_digests_v1
    )

    def retired_policy_digest_set(**kwargs) -> AssessmentDigestSet:
        current = current_digest_set(**kwargs)
        return AssessmentDigestSet(
            content_digest=current.content_digest,
            clarification_digest=current.clarification_digest,
            ruleset_digest=current.ruleset_digest,
            taxonomy_digest=current.taxonomy_digest,
            policy_digest=canonical_sha256("retired-requirement-lint-policy"),
        )

    monkeypatch.setattr(
        requirement_lint_assessment,
        "requirement_lint_normative_digests_v1",
        retired_policy_digest_set,
    )
    bundle = _lint_bundle(namespace="projection-stale-policy")
    monkeypatch.setattr(
        requirement_lint_assessment,
        "requirement_lint_normative_digests_v1",
        current_digest_set,
    )
    assert (
        bundle.receipt.digests.policy_digest
        != current_digest_set(
            content_digest=bundle.receipt.digests.content_digest,
            clarification_digest=bundle.receipt.digests.clarification_digest,
            default_locale=RequirementLocale.UNKNOWN,
        ).policy_digest
    )

    async with rig() as session:
        await _adapter(session).apply_bundle_cas(bundle)
        await session.commit()

    async with rig() as session:
        artifact = await session.get(Spec, SPEC_ID)
        assert artifact is not None
        projection = (
            await CommunitySqlAlchemyConsolidationPersistence().load_projection_inputs(
                session,
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=SPEC_ID,
                artifact=artifact,
            )
        )

    assert projection.quality_assessments == ()


async def test_board_source_root_hash_ignores_subject_version_stale_quality_head(
    rig,
) -> None:
    """Rebuild fingerprints must use the same current-only quality selector."""

    from okto_pulse.community.adapters.board_source_reader import (
        CommunityBoardSourceReader,
    )

    bundle = _lint_bundle(namespace="source-reader-stale-subject-version")
    async with rig() as session:
        await _adapter(session).apply_bundle_cas(bundle)
        await session.execute(
            update(Spec)
            .where(
                Spec.id == SPEC_ID,
                Spec.board_id == BOARD_ID,
            )
            .values(version=8)
        )
        await session.commit()
        assert session.bind is not None
        database_path = Path(str(session.bind.url.database))

    first_snapshot = CommunityBoardSourceReader(database_path).fetch(BOARD_ID)
    assert first_snapshot.complete is True
    first_root_hash = next(
        row["content_hash"]
        for row in first_snapshot.rows
        if row["source_ref"] == f"spec:{SPEC_ID}"
    )

    async with rig() as session:
        await session.execute(
            update(QualityAssessmentReceiptRow)
            .where(
                QualityAssessmentReceiptRow.id == bundle.receipt.id,
                QualityAssessmentReceiptRow.board_id == BOARD_ID,
            )
            .values(justification="Changed stale receipt must not rehash the root.")
        )
        await session.commit()

    second_snapshot = CommunityBoardSourceReader(database_path).fetch(BOARD_ID)
    assert second_snapshot.complete is True
    second_root_hash = next(
        row["content_hash"]
        for row in second_snapshot.rows
        if row["source_ref"] == f"spec:{SPEC_ID}"
    )

    assert second_root_hash == first_root_hash
async def test_board_source_root_hash_ignores_clarification_stale_quality_head(
    rig,
) -> None:
    """Rebuild projection must not retain a head invalidated only by Q&A."""

    from okto_pulse.community.adapters.board_source_reader import (
        CommunityBoardSourceReader,
    )

    bundle = _lint_bundle(namespace="source-reader-stale-clarification")
    assert bundle.proposed_questions
    async with rig() as session:
        await _adapter(session).apply_bundle_cas(bundle)
        await session.execute(
            update(SpecQAItem)
            .where(
                SpecQAItem.id == bundle.proposed_questions[0].qa_id,
                SpecQAItem.spec_id == SPEC_ID,
            )
            .values(
                answer="Use the response contract defined by AC-1.",
                answered_by="reviewer-quality",
                answered_at=NOW + timedelta(minutes=1),
                revision=2,
            )
        )
        await session.commit()
        assert session.bind is not None
        database_path = Path(str(session.bind.url.database))

    first_snapshot = CommunityBoardSourceReader(database_path).fetch(BOARD_ID)
    assert first_snapshot.complete is True
    first_root_hash = next(
        row["content_hash"]
        for row in first_snapshot.rows
        if row["source_ref"] == f"spec:{SPEC_ID}"
    )

    async with rig() as session:
        await session.execute(
            update(QualityAssessmentReceiptRow)
            .where(
                QualityAssessmentReceiptRow.id == bundle.receipt.id,
                QualityAssessmentReceiptRow.board_id == BOARD_ID,
            )
            .values(justification="A stale clarification receipt is historical.")
        )
        await session.commit()

    second_snapshot = CommunityBoardSourceReader(database_path).fetch(BOARD_ID)
    assert second_snapshot.complete is True
    second_root_hash = next(
        row["content_hash"]
        for row in second_snapshot.rows
        if row["source_ref"] == f"spec:{SPEC_ID}"
    )

    assert second_root_hash == first_root_hash
    # End-to-end guard: legacy history must not perturb the current root hash.
