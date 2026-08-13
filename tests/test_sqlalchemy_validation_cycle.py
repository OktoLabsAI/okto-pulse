"""Focused lifecycle-edition validation-cycle adapter contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import sqlalchemy_quality_assessment
from okto_pulse.community.adapters import sqlalchemy_validation_cycle
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    ChecklistValidationBindingSnapshotRow,
    Ideation,
    QualityAssessmentHeadRow,
    QualityAssessmentReceiptRow,
    Refinement,
    SemanticGuidelineSkipRow,
    SemanticGuidelineWaiverEventRow,
    SemanticGuidelineWaiverRow,
    Spec,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
    CommunitySqlAlchemyQualityAssessmentPreflightReader,
)
from okto_pulse.community.adapters.sqlalchemy_validation_cycle import (
    CommunitySqlAlchemyValidationCycleReader,
    _spec_remaining_actions,
)
from okto_pulse.core.domain.enums import IdeationStatus, RefinementStatus, SpecStatus
from okto_pulse.core.domain.quality_assessment import (
    AssessmentSubjectType,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.validation_cycle import (
    ValidationCycleResultType,
    ValidationCycleSubjectRef,
)
from okto_pulse.core.models.validation_cycle import (
    project_validation_cycle,
    project_validation_technical_audit,
)
from okto_pulse.core.ports.validation_cycle import (
    ValidationCycleReadAccessDenied,
    ValidationCycleResultNotFound,
    ValidationCycleSubjectNotFound,
)

pytestmark = pytest.mark.asyncio

BOARD_ID = "board-validation-cycle"
NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


class _AllQualityPermissions:
    def has(self, _permission: str) -> bool:
        return True


class _SelectedPermissions:
    def __init__(self, *permissions: str) -> None:
        self._permissions = frozenset(permissions)

    def has(self, permission: str) -> bool:
        return permission in self._permissions


def _permit_only(monkeypatch, *permissions: str) -> None:
    resolved = _SelectedPermissions(*permissions)

    async def permit(*_args, **_kwargs):
        return resolved, "viewer"

    monkeypatch.setattr(
        sqlalchemy_validation_cycle,
        "_quality_actor_permissions",
        permit,
    )


def _spec(index: int) -> Spec:
    return Spec(
        id=f"spec-cycle-{index:02d}",
        board_id=BOARD_ID,
        title=f"Validation cycle {index}",
        description="A governed validation-cycle subject.",
        context="Human lifecycle",
        functional_requirements=[],
        technical_requirements=[],
        acceptance_criteria=[],
        test_scenarios=[],
        business_rules=[],
        api_contracts=[],
        integration_requirements=[],
        observability_requirements=[],
        decisions=[],
        status=SpecStatus.APPROVED,
        edition=1,
        version=1,
        created_by="owner",
    )


def _ambiguity_receipt(
    *,
    receipt_id: str,
    subject_id: str,
    edition: int,
    score: float,
    head_revision: int,
    subject_type: str = "ideation",
) -> QualityAssessmentReceiptRow:
    digest = "d" * 64
    return QualityAssessmentReceiptRow(
        id=receipt_id,
        board_id=BOARD_ID,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_version=7,
        subject_edition=edition,
        assessment_kind="ambiguity",
        origin="human_or_agent",
        source="native",
        channel="rest:ambiguity",
        outcome="recorded",
        scale_kind="ambiguity_score",
        scale_minimum=1,
        scale_maximum=5,
        scale_direction="lower_better",
        score=score,
        justification="Current-edition ambiguity assessment.",
        content_digest=digest,
        clarification_digest=digest,
        ruleset_digest=digest,
        taxonomy_digest=digest,
        policy_digest=digest,
        input_digest=digest,
        canonicalization_version="quality-canonicalization/v1",
        ruleset_version="ambiguity/v1",
        taxonomy_version="ambiguity-taxonomy/v1",
        analyzer_version="test-agent",
        policy_version="quality-policy/v1",
        run_identity_digest=digest,
        authority_digest=digest,
        idempotency_key=receipt_id,
        request_digest=digest,
        created_by="test-agent",
        created_at=NOW,
        predecessor_receipt_id=None,
        contract_version="quality-assessment/v1",
        event_id=f"{receipt_id}-event",
        history_id=f"{receipt_id}-history",
        outbox_id=f"{receipt_id}-outbox",
        head_revision=head_revision,
    )


def _requirement_lint_receipt(
    *,
    receipt_id: str,
    spec_id: str,
    edition: int,
    score: float = 0,
) -> QualityAssessmentReceiptRow:
    digest = "e" * 64
    return QualityAssessmentReceiptRow(
        id=receipt_id,
        board_id=BOARD_ID,
        subject_type="spec",
        subject_id=spec_id,
        subject_version=4,
        subject_edition=edition,
        assessment_kind="requirement_lint",
        origin="human_or_agent",
        source="native",
        channel="rest:requirement_lint",
        outcome="advisory",
        scale_kind="finding_count",
        scale_minimum=0,
        scale_maximum=100,
        scale_direction="lower_better",
        score=score,
        justification="Current-edition requirement lint.",
        content_digest=digest,
        clarification_digest=digest,
        ruleset_digest=digest,
        taxonomy_digest=digest,
        policy_digest=digest,
        input_digest=digest,
        canonicalization_version="quality-canonicalization/v1",
        ruleset_version="requirement-lint/v1",
        taxonomy_version="ambiguity-taxonomy/v1",
        analyzer_version="test-agent",
        policy_version="quality-policy/v1",
        run_identity_digest=digest,
        authority_digest=digest,
        idempotency_key=receipt_id,
        request_digest=digest,
        created_by="test-agent",
        created_at=NOW,
        predecessor_receipt_id=None,
        contract_version="quality-assessment/v1",
        event_id=f"{receipt_id}-event",
        history_id=f"{receipt_id}-history",
        outbox_id=f"{receipt_id}-outbox",
        head_revision=1,
    )


@pytest.fixture
async def cycle_rig(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'validation-cycle.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        session.add(Board(id=BOARD_ID, name="Cycle", owner_id="owner"))
        session.add_all(_spec(index) for index in range(50))
        await session.commit()

    async def permit(*_args, **_kwargs):
        return _AllQualityPermissions(), "owner"

    monkeypatch.setattr(
        sqlalchemy_validation_cycle,
        "_quality_actor_permissions",
        permit,
    )
    monkeypatch.setattr(
        sqlalchemy_quality_assessment,
        "_quality_actor_permissions",
        permit,
    )
    try:
        yield SimpleNamespace(engine=engine, factory=factory)
    finally:
        await engine.dispose()


async def _count_selects(engine, operation):
    statements: list[str] = []

    def record(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        result = await operation
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    return result, statements


async def test_batch_of_fifty_is_summary_only_and_has_constant_select_budget(
    cycle_rig,
) -> None:
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)
    one = (ValidationCycleSubjectRef(AssessmentSubjectType.SPEC, "spec-cycle-00"),)
    fifty = tuple(
        ValidationCycleSubjectRef(
            AssessmentSubjectType.SPEC,
            f"spec-cycle-{index:02d}",
        )
        for index in range(50)
    )

    one_result, one_selects = await _count_selects(
        cycle_rig.engine,
        reader.get_validation_cycles(
            subjects=one,
            actor_id="owner",
            realm_scope=RealmScope.local(),
        ),
    )
    fifty_result, fifty_selects = await _count_selects(
        cycle_rig.engine,
        reader.get_validation_cycles(
            subjects=fifty,
            actor_id="owner",
            realm_scope=RealmScope.local(),
        ),
    )

    assert len(one_result) == 1
    assert len(fifty_result) == 50
    assert len(fifty_selects) == len(one_selects) == 6
    assert all(item.previous_results == () for item in fifty_result)
    assert [item.subject_id for item in fifty_result] == [
        item.subject_id for item in fifty
    ]


async def test_cycle_keeps_history_findings_and_audit_lazy(cycle_rig) -> None:
    current_digest = {
        name: character * 64
        for name, character in zip(
            (
                "content",
                "clarification",
                "ruleset",
                "taxonomy",
                "policy",
                "input",
            ),
            "abcdef",
            strict=True,
        )
    }
    async with cycle_rig.factory() as session:
        spec = await session.get(Spec, "spec-cycle-00")
        assert spec is not None
        spec.edition = 2
        spec.version = 3
        spec.current_validation_id = "validation-current"
        spec.validations = [
            {
                "id": "validation-previous",
                "receipt_id": "validation-previous",
                "edition": 1,
                "subject_version": 1,
                "head_revision": 1,
                "score": 72,
                "summary": "Previous human assessment.",
                "outcome": "success",
                "digests": current_digest,
                "findings": [{"large": "technical history" * 100}],
            },
            {
                "id": "validation-current",
                "receipt_id": "validation-current",
                "edition": 2,
                "subject_version": 3,
                "head_revision": 2,
                "score": 91,
                "summary": "Current human assessment.",
                "outcome": "success",
                "digests": current_digest,
                "findings": [{"large": "technical current" * 100}],
            },
        ]
        await session.commit()

    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)
    cycle, statements = await _count_selects(
        cycle_rig.engine,
        reader.get_validation_cycle(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id="spec-cycle-00",
            include_previous=False,
            offset=0,
            limit=25,
            actor_id="owner",
            realm_scope=RealmScope.local(),
        ),
    )
    payload = project_validation_cycle(cycle)
    encoded = json.dumps(payload)

    assert payload["previous_result_count"] == 1
    assert payload["previous_results"] == []
    assert "digests" not in encoded
    assert "findings" not in encoded
    assert all("quality_findings" not in statement.lower() for statement in statements)

    audit = await reader.get_result_technical_audit(
        subject_type=AssessmentSubjectType.SPEC,
        subject_id="spec-cycle-00",
        result_id="validation-current",
        result_type=ValidationCycleResultType.SPEC_VALIDATION,
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )
    audit_payload = project_validation_technical_audit(audit)
    assert audit_payload["technical_audit"]["digests"] == current_digest


async def test_current_ambiguity_projects_real_edition_gate_outcomes(
    cycle_rig,
) -> None:
    failed_id = "ideation-gate-failed"
    skipped_id = "ideation-gate-skipped"
    async with cycle_rig.factory() as session:
        board = await session.get(Board, BOARD_ID)
        assert board is not None
        board.settings = {
            "require_ideation_ambiguity_gate": True,
            "max_ideation_ambiguity": 2,
        }
        session.add_all(
            (
                Ideation(
                    id=failed_id,
                    board_id=BOARD_ID,
                    title="Failed current ambiguity gate",
                    status=IdeationStatus.EVALUATING,
                    edition=2,
                    version=7,
                    skip_ambiguity_gate=True,
                    skip_ambiguity_gate_edition=1,
                    created_by="owner",
                ),
                Ideation(
                    id=skipped_id,
                    board_id=BOARD_ID,
                    title="Skipped current ambiguity gate",
                    status=IdeationStatus.EVALUATING,
                    edition=2,
                    version=7,
                    skip_ambiguity_gate=True,
                    skip_ambiguity_gate_edition=2,
                    created_by="owner",
                ),
                _ambiguity_receipt(
                    receipt_id="ambiguity-failed-previous",
                    subject_id=failed_id,
                    edition=1,
                    score=1,
                    head_revision=1,
                ),
                _ambiguity_receipt(
                    receipt_id="ambiguity-failed-current",
                    subject_id=failed_id,
                    edition=2,
                    score=4,
                    head_revision=2,
                ),
                _ambiguity_receipt(
                    receipt_id="ambiguity-skipped-current",
                    subject_id=skipped_id,
                    edition=2,
                    score=5,
                    head_revision=1,
                ),
                QualityAssessmentHeadRow(
                    board_id=BOARD_ID,
                    subject_type="ideation",
                    subject_id=failed_id,
                    assessment_kind="ambiguity",
                    receipt_id="ambiguity-failed-current",
                    revision=2,
                    updated_at=NOW,
                ),
                QualityAssessmentHeadRow(
                    board_id=BOARD_ID,
                    subject_type="ideation",
                    subject_id=skipped_id,
                    assessment_kind="ambiguity",
                    receipt_id="ambiguity-skipped-current",
                    revision=1,
                    updated_at=NOW,
                ),
            )
        )
        await session.commit()

    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)
    failed_cycle = await reader.get_validation_cycle(
        subject_type=AssessmentSubjectType.IDEATION,
        subject_id=failed_id,
        include_previous=True,
        offset=0,
        limit=25,
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )
    failed_payload = project_validation_cycle(failed_cycle)

    assert failed_payload["current_result"]["status"] == "failed"
    assert failed_payload["current_result"]["summary"]["score"] == 4
    assert failed_payload["current_result"]["summary"]["threshold"] == 2
    assert failed_payload["current_result"]["summary"]["enabled"] is True
    assert failed_payload["current_result"]["summary"]["allowed"] is False
    assert failed_payload["current_result"]["summary"]["skipped"] is False
    assert failed_payload["current_result"]["summary"]["reason_code"] == (
        "ambiguity_score_exceeds_threshold"
    )
    assert failed_payload["previous_result_count"] == 1
    assert failed_payload["previous_results"][0]["subject_edition"] == 1
    assert failed_payload["previous_results"][0]["status"] == "completed"

    batch = await reader.get_validation_cycles(
        subjects=(
            ValidationCycleSubjectRef(AssessmentSubjectType.IDEATION, failed_id),
            ValidationCycleSubjectRef(AssessmentSubjectType.IDEATION, skipped_id),
        ),
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )
    batch_payload = tuple(project_validation_cycle(item) for item in batch)

    assert batch_payload[0]["current_result"]["status"] == "failed"
    assert batch_payload[0]["current_result"]["summary"]["threshold"] == 2
    assert batch_payload[1]["current_result"]["status"] == "passed"
    assert batch_payload[1]["current_result"]["summary"]["skipped"] is True
    assert batch_payload[1]["current_result"]["summary"]["reason_code"] == (
        "ambiguity_gate_skipped"
    )


async def test_refinement_cycle_loads_board_settings_for_single_and_batch(
    cycle_rig,
) -> None:
    refinement_id = "refinement-current-ambiguity"
    async with cycle_rig.factory() as session:
        board = await session.get(Board, BOARD_ID)
        assert board is not None
        board.settings = {
            "require_refinement_ambiguity_gate": True,
            "max_refinement_ambiguity": 2,
        }
        session.add_all(
            (
                Refinement(
                    id=refinement_id,
                    ideation_id="ideation-refinement-parent",
                    board_id=BOARD_ID,
                    title="Current refinement ambiguity",
                    status=RefinementStatus.APPROVED,
                    edition=2,
                    version=7,
                    created_by="owner",
                ),
                Ideation(
                    id="ideation-refinement-parent",
                    board_id=BOARD_ID,
                    title="Refinement parent",
                    status=IdeationStatus.DONE,
                    edition=1,
                    version=1,
                    created_by="owner",
                ),
                _ambiguity_receipt(
                    receipt_id="refinement-ambiguity-current",
                    subject_id=refinement_id,
                    edition=2,
                    score=4,
                    head_revision=1,
                    subject_type="refinement",
                ),
                QualityAssessmentHeadRow(
                    board_id=BOARD_ID,
                    subject_type="refinement",
                    subject_id=refinement_id,
                    assessment_kind="ambiguity",
                    receipt_id="refinement-ambiguity-current",
                    revision=1,
                    updated_at=NOW,
                ),
            )
        )
        await session.commit()

    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)
    single = await reader.get_validation_cycle(
        subject_type=AssessmentSubjectType.REFINEMENT,
        subject_id=refinement_id,
        include_previous=False,
        offset=0,
        limit=25,
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )
    batch = await reader.get_validation_cycles(
        subjects=(
            ValidationCycleSubjectRef(
                AssessmentSubjectType.REFINEMENT,
                refinement_id,
            ),
        ),
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )

    for cycle in (single, batch[0]):
        payload = project_validation_cycle(cycle)
        assert payload["current_result"]["status"] == "failed"
        assert payload["current_result"]["summary"]["enabled"] is True
        assert payload["current_result"]["summary"]["threshold"] == 2
        assert payload["current_result"]["summary"]["reason_code"] == (
            "ambiguity_score_exceeds_threshold"
        )


async def test_requirement_lint_result_has_subject_scoped_technical_audit(
    cycle_rig,
) -> None:
    digest = "a" * 64
    async with cycle_rig.factory() as session:
        spec = await session.get(Spec, "spec-cycle-01")
        assert spec is not None
        spec.edition = 2
        spec.version = 4
        session.add(
            QualityAssessmentReceiptRow(
                id="lint-result-1",
                board_id=BOARD_ID,
                subject_type="spec",
                subject_id=spec.id,
                subject_version=4,
                subject_edition=2,
                assessment_kind="requirement_lint",
                origin="human_or_agent",
                source="native",
                channel="rest:requirement_lint",
                outcome="advisory",
                scale_kind="finding_count",
                scale_minimum=0,
                scale_maximum=100,
                scale_direction="lower_better",
                score=2,
                justification="Two findings require human attention.",
                content_digest=digest,
                clarification_digest=digest,
                ruleset_digest=digest,
                taxonomy_digest=digest,
                policy_digest=digest,
                input_digest=digest,
                canonicalization_version="quality-canonicalization/v1",
                ruleset_version="requirement-lint/v1",
                taxonomy_version="ambiguity-taxonomy/v1",
                analyzer_version="external-agent",
                policy_version="quality-policy/v1",
                run_identity_digest=digest,
                authority_digest=digest,
                idempotency_key="lint-result-1",
                request_digest=digest,
                created_by="external-agent",
                created_at=NOW,
                predecessor_receipt_id=None,
                contract_version="quality-assessment/v1",
                event_id="lint-event-1",
                history_id="lint-history-1",
                outbox_id="lint-outbox-1",
                head_revision=1,
            )
        )
        await session.commit()

    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)
    audit = await reader.get_result_technical_audit(
        subject_type=AssessmentSubjectType.SPEC,
        subject_id="spec-cycle-01",
        result_id="lint-result-1",
        result_type=ValidationCycleResultType.REQUIREMENT_LINT,
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )

    assert audit.result_id == "lint-result-1"
    assert audit.subject_edition == 2
    assert audit.technical_audit.receipt_id == "lint-result-1"
    assert audit.technical_audit.subject_version == 4


async def test_legacy_null_edition_ambiguity_is_previous_only_and_auditable(
    cycle_rig,
) -> None:
    digest = "b" * 64
    async with cycle_rig.factory() as session:
        session.add(
            Ideation(
                id="ideation-legacy-ambiguity",
                board_id=BOARD_ID,
                title="Legacy ambiguity",
                created_by="owner",
                edition=2,
                version=3,
            )
        )
        session.add(
            QualityAssessmentReceiptRow(
                id="ambiguity-legacy-null",
                board_id=BOARD_ID,
                subject_type="ideation",
                subject_id="ideation-legacy-ambiguity",
                subject_version=1,
                subject_edition=None,
                assessment_kind="ambiguity",
                origin="legacy_import",
                source="legacy_migration",
                channel="migration",
                outcome="recorded",
                scale_kind="ambiguity_score",
                scale_minimum=1,
                scale_maximum=5,
                scale_direction="lower_better",
                score=2,
                justification="Imported before lifecycle editions existed.",
                content_digest=digest,
                clarification_digest=digest,
                ruleset_digest=digest,
                taxonomy_digest=digest,
                policy_digest=digest,
                input_digest=digest,
                canonicalization_version="quality-canonicalization/v1",
                ruleset_version="ambiguity/v1",
                taxonomy_version="ambiguity-taxonomy/v1",
                analyzer_version="legacy-import",
                policy_version="quality-policy/v1",
                run_identity_digest=digest,
                authority_digest=digest,
                idempotency_key="ambiguity-legacy-null",
                request_digest=digest,
                created_by="legacy-import",
                created_at=NOW,
                predecessor_receipt_id=None,
                contract_version="quality-assessment/v1",
                event_id="ambiguity-legacy-event",
                history_id="ambiguity-legacy-history",
                outbox_id="ambiguity-legacy-outbox",
                head_revision=1,
            )
        )
        session.add(
            QualityAssessmentHeadRow(
                board_id=BOARD_ID,
                subject_type="ideation",
                subject_id="ideation-legacy-ambiguity",
                assessment_kind="ambiguity",
                receipt_id="ambiguity-legacy-null",
                revision=1,
                updated_at=NOW,
            )
        )
        await session.commit()

    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)
    cycle = await reader.get_validation_cycle(
        subject_type=AssessmentSubjectType.IDEATION,
        subject_id="ideation-legacy-ambiguity",
        include_previous=True,
        offset=0,
        limit=25,
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )
    batch = await reader.get_validation_cycles(
        subjects=(
            ValidationCycleSubjectRef(
                AssessmentSubjectType.IDEATION,
                "ideation-legacy-ambiguity",
            ),
        ),
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )

    assert cycle.current_result is None
    assert cycle.previous_result_count == 1
    assert cycle.previous_results[0].subject_edition is None
    assert batch[0].current_result is None
    assert batch[0].previous_result_count == 1
    assert (
        project_validation_cycle(cycle)["previous_results"][0]["subject_edition"]
        is None
    )

    audit = await reader.get_result_technical_audit(
        subject_type=AssessmentSubjectType.IDEATION,
        subject_id="ideation-legacy-ambiguity",
        result_id="ambiguity-legacy-null",
        result_type=ValidationCycleResultType.AMBIGUITY_ASSESSMENT,
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )
    assert audit.subject_edition is None
    assert audit.technical_audit.exceptions == ()
    assert project_validation_technical_audit(audit)["subject_edition"] is None


async def test_legacy_null_edition_spec_validation_is_previous_only_and_auditable(
    cycle_rig,
) -> None:
    digests = {
        name: character * 64
        for name, character in zip(
            ("content", "clarification", "ruleset", "taxonomy", "policy", "input"),
            "cdefab",
            strict=True,
        )
    }
    async with cycle_rig.factory() as session:
        spec = await session.get(Spec, "spec-cycle-04")
        assert spec is not None
        spec.edition = 2
        spec.version = 4
        spec.current_validation_id = "validation-legacy-null"
        spec.validations = [
            {
                "id": "validation-legacy-null",
                "receipt_id": "validation-legacy-null",
                "edition": None,
                "subject_version": 1,
                "head_revision": 1,
                "score": 80,
                "summary": "Imported before lifecycle editions existed.",
                "outcome": "success",
                "digests": digests,
            }
        ]
        session.add_all(
            (
                _requirement_lint_receipt(
                    receipt_id="lint-current-for-legacy-validation",
                    spec_id=spec.id,
                    edition=2,
                ),
                QualityAssessmentHeadRow(
                    board_id=BOARD_ID,
                    subject_type="spec",
                    subject_id=spec.id,
                    assessment_kind="requirement_lint",
                    receipt_id="lint-current-for-legacy-validation",
                    revision=1,
                    updated_at=NOW,
                ),
                ChecklistValidationBindingSnapshotRow(
                    board_id=BOARD_ID,
                    spec_id=spec.id,
                    spec_edition=2,
                    target_type="spec",
                    phase="spec_validation",
                    template_version="checklist-off/v1",
                    mode="off",
                    binding_version=1,
                    binding_revision=0,
                    binding_digest="f" * 64,
                    captured_at=NOW,
                ),
            )
        )
        await session.commit()

    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)
    cycle = await reader.get_validation_cycle(
        subject_type=AssessmentSubjectType.SPEC,
        subject_id="spec-cycle-04",
        include_previous=True,
        offset=0,
        limit=25,
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )
    batch = await reader.get_validation_cycles(
        subjects=(
            ValidationCycleSubjectRef(
                AssessmentSubjectType.SPEC,
                "spec-cycle-04",
            ),
        ),
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )

    assert cycle.current_result is None
    assert cycle.previous_result_count == 1
    assert cycle.previous_results[0].subject_edition is None
    assert tuple(item.status for item in cycle.checks) == ("passed", "off", "off")
    assert cycle.remaining_actions == ("submit_spec_validation",)
    assert batch[0].current_result is None
    assert batch[0].previous_result_count == 1
    assert tuple(item.status for item in batch[0].checks) == ("passed", "off", "off")
    assert batch[0].remaining_actions == ("submit_spec_validation",)

    audit = await reader.get_result_technical_audit(
        subject_type=AssessmentSubjectType.SPEC,
        subject_id="spec-cycle-04",
        result_id="validation-legacy-null",
        result_type=ValidationCycleResultType.SPEC_VALIDATION,
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )
    assert audit.subject_edition is None
    assert audit.technical_audit.exceptions == ()
    assert project_validation_technical_audit(audit)["subject_edition"] is None


async def test_requirement_lint_preflight_closes_transaction_before_agent_wait(
    cycle_rig,
) -> None:
    sessions: list[AsyncSession] = []

    def tracked_factory() -> AsyncSession:
        session = cycle_rig.factory()
        sessions.append(session)
        return session

    reader = CommunitySqlAlchemyQualityAssessmentPreflightReader(tracked_factory)
    preflight = await reader.resolve_requirement_lint_preflight(
        spec_id="spec-cycle-02",
        actor_id="owner",
        realm_scope=RealmScope.local(),
    )

    assert preflight.subject_edition == 1
    assert sessions and all(not session.in_transaction() for session in sessions)
    await asyncio.sleep(0)
    assert all(not session.in_transaction() for session in sessions)


async def test_twenty_concurrent_cycle_reads_do_not_lock_sqlite(cycle_rig) -> None:
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                reader.get_validation_cycle(
                    subject_type=AssessmentSubjectType.SPEC,
                    subject_id="spec-cycle-03",
                    include_previous=False,
                    offset=0,
                    limit=25,
                    actor_id="owner",
                    realm_scope=RealmScope.local(),
                )
                for _ in range(20)
            )
        ),
        timeout=20,
    )

    assert len(results) == 20
    assert {item.subject_id for item in results} == {"spec-cycle-03"}


async def test_lint_findings_are_advisory_not_a_request_to_run_lint_again() -> None:
    assert _spec_remaining_actions(
        lint_status="needs_attention",
        checklist_status="passed",
        policy_status="off",
        has_current_validation=False,
    ) == ("submit_spec_validation",)


async def _seed_authorization_results(cycle_rig, spec_id: str) -> None:
    digests = {
        name: character * 64
        for name, character in zip(
            ("content", "clarification", "ruleset", "taxonomy", "policy", "input"),
            "abcdef",
            strict=True,
        )
    }
    async with cycle_rig.factory() as session:
        spec = await session.get(Spec, spec_id)
        assert spec is not None
        spec.edition = 2
        spec.version = 4
        spec.current_validation_id = f"{spec_id}-validation"
        spec.validations = [
            {
                "id": f"{spec_id}-validation",
                "receipt_id": f"{spec_id}-validation",
                "edition": 2,
                "subject_version": 4,
                "head_revision": 1,
                "score": 91,
                "outcome": "success",
                "general_justification": "Restricted validation justification.",
                "digests": digests,
            }
        ]
        lint = _requirement_lint_receipt(
            receipt_id=f"{spec_id}-lint",
            spec_id=spec_id,
            edition=2,
            score=2,
        )
        session.add(lint)
        session.add(
            QualityAssessmentHeadRow(
                board_id=BOARD_ID,
                subject_type="spec",
                subject_id=spec_id,
                assessment_kind="requirement_lint",
                receipt_id=lint.id,
                revision=1,
                updated_at=NOW,
            )
        )
        await session.commit()


async def _seed_policy_exception_rows(cycle_rig, spec_id: str) -> None:
    digest = "9" * 64
    skip_event_id = "8" * 64
    waiver_event_id = "7" * 64
    async with cycle_rig.factory() as session:
        session.add(
            SemanticGuidelineSkipRow(
                event_id=skip_event_id,
                predecessor_event_id=None,
                skip_id="skip-authz",
                skip_revision=1,
                event_type="create",
                from_status=None,
                status="active",
                board_id=BOARD_ID,
                subject_type="spec",
                subject_id=spec_id,
                subject_version=4,
                validation_edition=2,
                subject_content_digest=digest,
                guideline_id="guideline-authz",
                revision_id="revision-authz",
                revision_digest=digest,
                binding_id="binding-authz",
                binding_revision=1,
                configuration_digest=digest,
                scope_digest=digest,
                reason="Policy skip visible only to assessment readers.",
                created_by="owner",
                created_at=NOW,
                actor_id="owner",
                actor_kind="human",
                occurred_at=NOW,
                revoked_by=None,
                revoked_at=None,
                revocation_reason=None,
                skip_digest=digest,
                idempotency_key="skip-authz-key",
                request_digest=digest,
            )
        )
        waiver = SemanticGuidelineWaiverRow(
            waiver_id="waiver-authz",
            board_id=BOARD_ID,
            metric_result_id="metric-result-authz",
            finding_id="finding-authz",
            receipt_id="policy-receipt-authz",
            subject_type="spec",
            subject_id=spec_id,
            subject_version=4,
            validation_edition=2,
            subject_content_digest=digest,
            receipt_digest=digest,
            guideline_id="guideline-authz",
            revision_id="revision-authz",
            revision_digest=digest,
            binding_id="binding-authz",
            binding_revision=1,
            configuration_digest=digest,
            metric_id="metric-authz",
            metric_code="metric.authz",
            metric_result_digest=digest,
            finding_digest=digest,
            scope_digest=digest,
            justification="Restricted waiver reason.",
            evidence_refs=[],
            requested_by="owner",
            requested_at=NOW,
            original_expires_at=None,
            status="requested",
            waiver_revision=1,
            expires_at=None,
            last_event_id=waiver_event_id,
            last_event_type="request",
            last_event_at=NOW,
            reviewed_by=None,
            reviewed_at=None,
            review_reason=None,
            revoked_by=None,
            revoked_at=None,
            expire_reason_code=None,
            head_digest=digest,
            idempotency_key="waiver-authz-key",
            request_digest=digest,
            assessment_assessor_id="assessor-authz",
            last_event_idempotency_key="waiver-event-authz-key",
            last_revalidation_status=None,
            last_revalidation_current=None,
            last_revalidation_reason_code=None,
            last_revalidation_evaluated_at=None,
            last_revalidation_currentness_reasons=[],
            last_revalidation_scheduled_expiry_observed=False,
        )
        waiver_event = SemanticGuidelineWaiverEventRow(
            event_id=waiver_event_id,
            predecessor_event_id=None,
            waiver_id=waiver.waiver_id,
            board_id=BOARD_ID,
            validation_edition=2,
            waiver_revision=1,
            event_type="request",
            from_status=None,
            to_status="requested",
            actor_id="owner",
            occurred_at=NOW,
            reason="Restricted waiver reason.",
            evidence_refs=[],
            expires_at=None,
            scope_digest=digest,
            waiver_digest=digest,
            reviewed_by=None,
            reviewed_at=None,
            review_reason=None,
            revoked_by=None,
            revoked_at=None,
            expire_reason_code=None,
            idempotency_key="waiver-event-authz-key",
            request_digest=digest,
            evaluated_at=None,
            revalidation_status=None,
            revalidation_current=None,
            revalidation_reason_code=None,
            currentness_reasons=[],
            scheduled_expiry_observed=False,
        )
        session.add_all((waiver, waiver_event))
        await session.commit()


@pytest.mark.parametrize(
    ("leaf", "expected_sections", "expected_checks", "primary_visible"),
    (
        (
            "spec.validation.read",
            ["spec_validation"],
            [],
            True,
        ),
        (
            "spec.quality.read",
            ["requirement_lint"],
            ["requirement_lint"],
            False,
        ),
        (
            "spec.checklist.read",
            ["curated_checklist"],
            ["curated_checklist"],
            False,
        ),
        (
            "guidelines.assessments.read",
            ["policy_compliance"],
            ["policy_compliance"],
            False,
        ),
    ),
)
async def test_single_spec_cycle_projects_only_the_authorized_leaf(
    cycle_rig,
    monkeypatch,
    leaf: str,
    expected_sections: list[str],
    expected_checks: list[str],
    primary_visible: bool,
) -> None:
    await _seed_authorization_results(cycle_rig, "spec-cycle-05")
    _permit_only(monkeypatch, "spec.entity.read", leaf)
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)

    payload = project_validation_cycle(
        await reader.get_validation_cycle(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id="spec-cycle-05",
            include_previous=True,
            offset=0,
            limit=25,
            actor_id="partial-reader",
            realm_scope=RealmScope.local(),
        )
    )

    assert payload["visible_sections"] == expected_sections
    assert [item["result_type"] for item in payload["checks"]] == expected_checks
    primary_keys = {
        "cycle_state",
        "current_result",
        "previous_result_count",
        "previous_results",
        "submission_fence",
    }
    if primary_visible:
        assert primary_keys <= payload.keys()
        assert payload["current_result"]["summary"]["general_justification"] == (
            "Restricted validation justification."
        )
    else:
        assert primary_keys.isdisjoint(payload)
        assert "Restricted validation justification." not in json.dumps(payload)


async def test_spec_cycle_projects_an_authorized_combination_without_cross_leaks(
    cycle_rig,
    monkeypatch,
) -> None:
    await _seed_authorization_results(cycle_rig, "spec-cycle-06")
    _permit_only(
        monkeypatch,
        "spec.entity.read",
        "spec.quality.read",
        "guidelines.assessments.read",
    )
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)

    payload = project_validation_cycle(
        await reader.get_validation_cycle(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id="spec-cycle-06",
            include_previous=True,
            offset=0,
            limit=25,
            actor_id="partial-reader",
            realm_scope=RealmScope.local(),
        )
    )

    assert payload["visible_sections"] == [
        "requirement_lint",
        "policy_compliance",
    ]
    assert [item["result_type"] for item in payload["checks"]] == [
        "requirement_lint",
        "policy_compliance",
    ]
    assert "current_result" not in payload
    assert "curated_checklist" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("permissions", "expected_error"),
    (
        (("spec.entity.read",), ValidationCycleReadAccessDenied),
        (("guidelines.assessments.read",), ValidationCycleSubjectNotFound),
    ),
)
async def test_spec_cycle_requires_entity_read_and_at_least_one_section(
    cycle_rig,
    monkeypatch,
    permissions: tuple[str, ...],
    expected_error: type[Exception],
) -> None:
    _permit_only(monkeypatch, *permissions)
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)

    with pytest.raises(expected_error):
        await reader.get_validation_cycle(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id="spec-cycle-07",
            include_previous=False,
            offset=0,
            limit=25,
            actor_id="no-visible-section",
            realm_scope=RealmScope.local(),
        )


async def test_entity_invisibility_matches_random_id_for_single_audit_and_batch(
    cycle_rig,
    monkeypatch,
) -> None:
    _permit_only(monkeypatch, "spec.validation.read")
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)

    async def single(subject_id: str) -> None:
        await reader.get_validation_cycle(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id=subject_id,
            include_previous=False,
            offset=0,
            limit=25,
            actor_id="entity-blind-reader",
            realm_scope=RealmScope.local(),
        )

    async def audit(subject_id: str) -> None:
        await reader.get_result_technical_audit(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id=subject_id,
            result_id="opaque-result",
            result_type=ValidationCycleResultType.SPEC_VALIDATION,
            actor_id="entity-blind-reader",
            realm_scope=RealmScope.local(),
        )

    async def batch(subject_id: str) -> None:
        await reader.get_validation_cycles(
            subjects=(
                ValidationCycleSubjectRef(AssessmentSubjectType.SPEC, subject_id),
            ),
            actor_id="entity-blind-reader",
            realm_scope=RealmScope.local(),
        )

    for operation in (single, audit, batch):
        with pytest.raises(ValidationCycleSubjectNotFound) as hidden:
            await operation("spec-cycle-07")
        with pytest.raises(ValidationCycleSubjectNotFound) as missing:
            await operation("spec-random-missing")
        assert type(hidden.value) is type(missing.value)


async def test_batch_applies_the_same_field_visibility_to_every_spec(
    cycle_rig,
    monkeypatch,
) -> None:
    await _seed_authorization_results(cycle_rig, "spec-cycle-08")
    await _seed_authorization_results(cycle_rig, "spec-cycle-09")
    _permit_only(monkeypatch, "spec.entity.read", "spec.quality.read")
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)

    payloads = tuple(
        project_validation_cycle(item)
        for item in await reader.get_validation_cycles(
            subjects=(
                ValidationCycleSubjectRef(
                    AssessmentSubjectType.SPEC,
                    "spec-cycle-08",
                ),
                ValidationCycleSubjectRef(
                    AssessmentSubjectType.SPEC,
                    "spec-cycle-09",
                ),
            ),
            actor_id="batch-quality-reader",
            realm_scope=RealmScope.local(),
        )
    )

    assert [item["subject_id"] for item in payloads] == [
        "spec-cycle-08",
        "spec-cycle-09",
    ]
    assert all(item["visible_sections"] == ["requirement_lint"] for item in payloads)
    assert all("current_result" not in item for item in payloads)
    assert all(
        [check["result_type"] for check in item["checks"]] == ["requirement_lint"]
        for item in payloads
    )


async def test_technical_audit_is_result_typed_and_hides_result_existence(
    cycle_rig,
    monkeypatch,
) -> None:
    spec_id = "spec-cycle-10"
    validation_id = f"{spec_id}-validation"
    lint_id = f"{spec_id}-lint"
    await _seed_authorization_results(cycle_rig, spec_id)
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)

    _permit_only(monkeypatch, "spec.entity.read", "spec.validation.read")
    validation_audit = await reader.get_result_technical_audit(
        subject_type=AssessmentSubjectType.SPEC,
        subject_id=spec_id,
        result_id=validation_id,
        result_type=ValidationCycleResultType.SPEC_VALIDATION,
        actor_id="validation-reader",
        realm_scope=RealmScope.local(),
    )
    assert validation_audit.technical_audit.visible_exception_types == ()
    with pytest.raises(ValidationCycleResultNotFound) as hidden_lint:
        await reader.get_result_technical_audit(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id=spec_id,
            result_id=lint_id,
            result_type=ValidationCycleResultType.REQUIREMENT_LINT,
            actor_id="validation-reader",
            realm_scope=RealmScope.local(),
        )

    _permit_only(monkeypatch, "spec.entity.read", "spec.quality.read")
    lint_audit = await reader.get_result_technical_audit(
        subject_type=AssessmentSubjectType.SPEC,
        subject_id=spec_id,
        result_id=lint_id,
        result_type=ValidationCycleResultType.REQUIREMENT_LINT,
        actor_id="quality-reader",
        realm_scope=RealmScope.local(),
    )
    assert lint_audit.technical_audit.visible_exception_types == ()
    with pytest.raises(ValidationCycleResultNotFound) as hidden_validation:
        await reader.get_result_technical_audit(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id=spec_id,
            result_id=validation_id,
            result_type=ValidationCycleResultType.SPEC_VALIDATION,
            actor_id="quality-reader",
            realm_scope=RealmScope.local(),
        )
    with pytest.raises(ValidationCycleResultNotFound) as missing:
        await reader.get_result_technical_audit(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id=spec_id,
            result_id="does-not-exist",
            result_type=ValidationCycleResultType.REQUIREMENT_LINT,
            actor_id="quality-reader",
            realm_scope=RealmScope.local(),
        )

    assert (
        type(hidden_lint.value) is type(hidden_validation.value) is type(missing.value)
    )
    assert hidden_lint.value.code == hidden_validation.value.code == missing.value.code


@pytest.mark.parametrize(
    ("extra_permissions", "expected_types"),
    (
        ((), []),
        (("guidelines.assessments.read",), ["policy_skip"]),
        (("guidelines.waiver.read",), ["policy_waiver"]),
        (
            ("guidelines.assessments.read", "guidelines.waiver.read"),
            ["policy_skip", "policy_waiver"],
        ),
    ),
)
async def test_technical_audit_redacts_real_policy_exceptions_by_leaf(
    cycle_rig,
    monkeypatch,
    extra_permissions: tuple[str, ...],
    expected_types: list[str],
) -> None:
    spec_id = "spec-cycle-11"
    await _seed_authorization_results(cycle_rig, spec_id)
    await _seed_policy_exception_rows(cycle_rig, spec_id)
    _permit_only(
        monkeypatch,
        "spec.entity.read",
        "spec.validation.read",
        *extra_permissions,
    )
    reader = CommunitySqlAlchemyValidationCycleReader(cycle_rig.factory)

    payload = project_validation_technical_audit(
        await reader.get_result_technical_audit(
            subject_type=AssessmentSubjectType.SPEC,
            subject_id=spec_id,
            result_id=f"{spec_id}-validation",
            result_type=ValidationCycleResultType.SPEC_VALIDATION,
            actor_id="validation-reader",
            realm_scope=RealmScope.local(),
        )
    )

    assert payload["technical_audit"]["visible_exception_types"] == expected_types
    assert sorted(
        item["exception_type"] for item in payload["technical_audit"]["exceptions"]
    ) == sorted(expected_types)
    encoded = json.dumps(payload)
    assert ("Policy skip visible" in encoded) is ("policy_skip" in expected_types)
    assert ("Restricted waiver reason" in encoded) is (
        "policy_waiver" in expected_types
    )
