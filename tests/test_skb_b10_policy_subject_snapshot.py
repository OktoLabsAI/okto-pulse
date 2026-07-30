"""SK-B/B10 Community policy subjects, currentness and transition fencing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    guideline_revision_content_digest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    Card,
    CardDependency,
    Ideation,
    IdeationQAItem,
    Refinement,
    Spec,
    Sprint,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_snapshot import (
    CommunitySqlAlchemyPolicySubjectSnapshotResolver,
)
from okto_pulse.core.domain.enums import (
    CardPriority,
    CardStatus,
    CardType,
    IdeationComplexity,
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
    SprintStatus,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelinePredicate,
    GuidelineRevision,
    GuidelineRule,
    GuidelineScope,
    PolicyEntityType,
    PolicyCurrentness,
    PolicyWaiverEventType,
)
from okto_pulse.core.domain.guideline_policy_evaluator import (
    build_policy_evaluation_input_v1,
    evaluate_policy,
)
from okto_pulse.core.domain.guideline_compliance import (
    PolicyCurrentnessAssessment,
)
from okto_pulse.core.domain.guideline_waiver_lifecycle import (
    PolicyWaiverSource,
    request_policy_waiver,
    transition_policy_waiver,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicySubjectConflict,
)
from okto_pulse.core.ports.test_evidence import (
    TestEvidenceWriteVerification as EvidenceWriteVerification,
    register_test_evidence_write_verifier,
    reset_test_evidence_write_verifier_for_tests,
)
from okto_pulse.core.services.test_scenario_lifecycle import (
    compute_execution_attestation_sha256,
    compute_test_scenario_semantic_sha256,
)


NOW = datetime(2026, 7, 29, 18, tzinfo=timezone.utc)
BOARD_ID = "board-b10-subject"
IDEATION_ID = "ideation-b10-subject"
REFINEMENT_ID = "refinement-b10-subject"
SPEC_ID = "spec-b10-subject"
SPRINT_ID = "sprint-b10-subject"
CARD_ID = "card-b10-subject"


class _ReceiptVerifier:
    def verify(self, **request: Any) -> EvidenceWriteVerification:
        evidence = request.get("evidence")
        return EvidenceWriteVerification(
            verified=(
                isinstance(evidence, dict)
                and evidence.get("execution_receipt") == "receipt:trusted"
            ),
            reason_codes=(
                ()
                if isinstance(evidence, dict)
                and evidence.get("execution_receipt") == "receipt:trusted"
                else ("evidence_v2.receipt_not_current",)
            ),
        )


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def _scenario(
    scenario_id: str,
    *,
    receipt: str,
) -> dict[str, Any]:
    scenario: dict[str, Any] = {
        "id": scenario_id,
        "scenario_type": "e2e",
        "status": "passed",
        "given": "a policy subject",
        "when": "the current evidence is checked",
        "then": "only authenticated evidence is counted",
        "linked_criteria": ["ac-b10"],
    }
    scenario_sha256 = compute_test_scenario_semantic_sha256(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        scenario=scenario,
        acceptance_criteria=[{"id": "ac-b10", "text": "Evidence is current"}],
    )
    attestation = {
        "schema_version": 2,
        "run_id": f"run-{scenario_id}",
        "executed_at": NOW.isoformat(),
        "scenario_id": scenario_id,
        "scenario_sha256": scenario_sha256,
        "outcome": "passed",
        "product_runtime_exercised": True,
        "manifest_sha256": "sha256:" + "a" * 64,
        "assertions": [
            {
                "name": "policy evidence",
                "expected": True,
                "observed": True,
                "status": "passed",
                "message": None,
            }
        ],
        "provenance": {
            "producer": "pytest",
            "producer_version": "1",
            "adapter": "community",
            "environment": "test",
        },
    }
    attestation["attestation_sha256"] = compute_execution_attestation_sha256(
        attestation,
        manifest_ref=f"manifest:{scenario_id}",
    )
    scenario["evidence"] = {
        "evidence_class": "mcp_replay_manifest",
        "manifest_ref": f"manifest:{scenario_id}",
        "execution_attestation": attestation,
        "execution_receipt": receipt,
    }
    return scenario


async def _seed_subjects() -> None:
    trusted = _scenario("scenario-trusted", receipt="receipt:trusted")
    fake = _scenario("scenario-fake", receipt="receipt:caller-controlled")
    async with get_session_factory()() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="B10 policy subjects",
                owner_id="owner-b10",
            )
        )
        await session.flush()
        session.add(
            Ideation(
                id=IDEATION_ID,
                board_id=BOARD_ID,
                title="Policy ideation",
                status=IdeationStatus.EVALUATING,
                complexity=IdeationComplexity.MEDIUM,
                labels=["policy", "policy"],
                created_by="owner-b10",
            )
        )
        await session.flush()
        session.add_all(
            [
                IdeationQAItem(
                    id="qa-open-b10",
                    ideation_id=IDEATION_ID,
                    question="Still open?",
                    asked_by="owner-b10",
                ),
                IdeationQAItem(
                    id="qa-answered-b10",
                    ideation_id=IDEATION_ID,
                    question="Answered?",
                    answer="Yes",
                    answered_at=NOW,
                    asked_by="owner-b10",
                    answered_by="owner-b10",
                ),
                Refinement(
                    id=REFINEMENT_ID,
                    ideation_id=IDEATION_ID,
                    board_id=BOARD_ID,
                    title="Policy refinement",
                    status=RefinementStatus.APPROVED,
                    created_by="owner-b10",
                ),
                Spec(
                    id=SPEC_ID,
                    ideation_id=IDEATION_ID,
                    refinement_id=REFINEMENT_ID,
                    board_id=BOARD_ID,
                    title="Policy spec",
                    status=SpecStatus.DRAFT,
                    functional_requirements=[{"id": "fr-b10"}],
                    acceptance_criteria=[
                        {"id": "ac-b10", "text": "Evidence is current"}
                    ],
                    technical_requirements=[{"id": "tr-b10"}],
                    test_scenarios=[trusted, fake],
                    validations=[
                        {
                            "id": "validation-b10",
                            "outcome": "success",
                        }
                    ],
                    current_validation_id="validation-b10",
                    created_by="owner-b10",
                ),
            ]
        )
        await session.flush()
        session.add(
            Sprint(
                id=SPRINT_ID,
                spec_id=SPEC_ID,
                board_id=BOARD_ID,
                title="Policy sprint",
                status=SprintStatus.ACTIVE,
                test_scenario_ids=["scenario-trusted", "scenario-fake"],
                created_by="owner-b10",
            )
        )
        session.add_all(
            [
                Card(
                    id=CARD_ID,
                    board_id=BOARD_ID,
                    spec_id=SPEC_ID,
                    sprint_id=SPRINT_ID,
                    title="Policy test card",
                    status=CardStatus.IN_PROGRESS,
                    priority=CardPriority.HIGH,
                    card_type=CardType.TEST,
                    test_scenario_ids=[
                        "scenario-trusted",
                        "scenario-fake",
                    ],
                    created_by="owner-b10",
                ),
                Card(
                    id="card-upstream-open-b10",
                    board_id=BOARD_ID,
                    spec_id=SPEC_ID,
                    title="Open dependency",
                    status=CardStatus.IN_PROGRESS,
                    created_by="owner-b10",
                ),
                Card(
                    id="card-upstream-done-b10",
                    board_id=BOARD_ID,
                    spec_id=SPEC_ID,
                    title="Done dependency",
                    status=CardStatus.DONE,
                    created_by="owner-b10",
                ),
                Card(
                    id="card-archived-test-b10",
                    board_id=BOARD_ID,
                    spec_id=SPEC_ID,
                    title="Archived test",
                    status=CardStatus.DONE,
                    card_type=CardType.TEST,
                    test_scenario_ids=["scenario-trusted"],
                    archived=True,
                    created_by="owner-b10",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                CardDependency(
                    id="dependency-open-b10",
                    card_id=CARD_ID,
                    depends_on_id="card-upstream-open-b10",
                ),
                CardDependency(
                    id="dependency-done-b10",
                    card_id=CARD_ID,
                    depends_on_id="card-upstream-done-b10",
                ),
            ]
        )
        await session.commit()


def _facts(snapshot) -> dict[str, object]:
    return dict(snapshot.attributes)


@pytest.mark.asyncio
async def test_closed_facts_and_authenticated_evidence_are_server_owned(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b10-subject-facts.sqlite3")
    register_test_evidence_write_verifier(_ReceiptVerifier())
    try:
        await _seed_subjects()
        async with get_session_factory()() as session:
            resolver = CommunitySqlAlchemyPolicySubjectSnapshotResolver(
                session,
                resource_gate_ready_resolver=lambda *_args: True,
            )
            ideation = await resolver.resolve_subject_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.IDEATION,
                subject_id=IDEATION_ID,
            )
            refinement = await resolver.resolve_subject_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.REFINEMENT,
                subject_id=REFINEMENT_ID,
            )
            spec = await resolver.resolve_subject_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.SPEC,
                subject_id=SPEC_ID,
            )
            sprint = await resolver.resolve_subject_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.SPRINT,
                subject_id=SPRINT_ID,
            )
            card = await resolver.resolve_subject_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.CARD,
                subject_id=CARD_ID,
            )
            trusted = await resolver.resolve_subject_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.TEST_SCENARIO,
                subject_id="scenario-trusted",
            )
            fake = await resolver.resolve_subject_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.TEST_SCENARIO,
                subject_id="scenario-fake",
            )

        assert all(
            snapshot is not None
            for snapshot in (
                ideation,
                refinement,
                spec,
                sprint,
                card,
                trusted,
                fake,
            )
        )
        assert _facts(ideation) == {
            "complexity": "medium",
            "labels": ("policy",),
            "qa_open_count": 1,
            "resource_gate_ready": True,
            "status": "evaluating",
        }
        assert _facts(spec) == {
            "ac_count": 1,
            "coverage_percent": 100.0,
            "fr_count": 1,
            "resource_gate_ready": True,
            "status": "draft",
            "test_scenario_count": 2,
            "tr_count": 1,
            "validation_state": "success",
        }
        assert _facts(refinement) == {
            "research_open_count": 0,
            "research_resolved_count": 0,
            "resource_gate_ready": True,
            "status": "approved",
        }
        assert _facts(sprint)["test_scenario_count"] == 2
        assert _facts(sprint)["passed_scenario_count"] == 2
        assert _facts(sprint)["resource_gate_ready"] is False
        assert _facts(card)["dependency_open_count"] == 1
        assert _facts(card)["test_scenario_count"] == 2
        assert _facts(card)["evidence_count"] == 1
        assert _facts(trusted)["linked_test_card_count"] == 1
        assert _facts(trusted)["evidence_count"] == 1
        assert _facts(fake)["evidence_count"] == 0
    finally:
        reset_test_evidence_write_verifier_for_tests()


@pytest.mark.asyncio
async def test_duplicate_scenario_identity_fails_closed_across_board(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b10-duplicate-scenario.sqlite3")
    async with get_session_factory()() as session:
        session.add(Board(id=BOARD_ID, name="B10 duplicate", owner_id="owner-b10"))
        await session.flush()
        for index in range(2):
            session.add(
                Spec(
                    id=f"spec-duplicate-{index}",
                    board_id=BOARD_ID,
                    title=f"Duplicate {index}",
                    test_scenarios=[
                        {
                            "id": "scenario-duplicate",
                            "scenario_type": "e2e",
                            "status": "draft",
                        }
                    ],
                    created_by="owner-b10",
                )
            )
        await session.commit()

    async with get_session_factory()() as session:
        resolver = CommunitySqlAlchemyPolicySubjectSnapshotResolver(session)
        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="policy_test_scenario_subject_duplicate",
        ):
            await resolver.resolve_subject_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.TEST_SCENARIO,
                subject_id="scenario-duplicate",
            )


def _blocking_spec_rule() -> GuidelineRule:
    return GuidelineRule(
        rule_id="rule-b10-transition",
        code="policy.b10.status",
        title="Draft policy",
        description="The policy subject is a draft.",
        target_entity_types=(PolicyEntityType.SPEC,),
        predicates=(
            GuidelinePredicate(
                predicate_code="eq",
                parameters=(("fact", "status"), ("value", "draft")),
            ),
        ),
        enforcement=GuidelineEnforcement.BLOCKING,
    )


async def _install_policy(
    session,
    *,
    suffix: str,
    rule: GuidelineRule,
    created_at: datetime,
) -> tuple[GuidelineRevision, BoardGuidelineBinding]:
    revision = GuidelineRevision(
        revision_id=f"revision-{suffix}",
        guideline_id=f"guideline-{suffix}",
        revision_number=1,
        semantic_version="1.0.0",
        title=f"Policy {suffix}",
        content="Executable policy.",
        content_digest=guideline_revision_content_digest(
            title=f"Policy {suffix}",
            content="Executable policy.",
            rules=(rule,),
        ),
        rules=(rule,),
        created_by="owner-b10",
        created_at=created_at,
    )
    binding = BoardGuidelineBinding(
        binding_id=f"binding-{suffix}",
        board_id=BOARD_ID,
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.content_digest,
        priority=0,
        binding_revision=1,
        adopted_by="owner-b10",
        adopted_at=created_at,
        default_enforcement=GuidelineEnforcement.BLOCKING,
    )
    policy = CommunitySqlAlchemyGuidelinePolicy(session)
    await policy.create_guideline(
        guideline=Guideline(
            guideline_id=revision.guideline_id,
            owner_id="owner-b10",
            scope=GuidelineScope.GLOBAL,
            created_at=created_at,
        ),
        initial_revision=revision,
        initial_head=GuidelineHead(
            guideline_id=revision.guideline_id,
            revision_id=revision.revision_id,
            revision_number=1,
            semantic_version=revision.semantic_version,
            head_revision=1,
            updated_at=created_at,
        ),
        idempotency_key=f"create-{suffix}",
        request_digest="1" * 64,
    )
    await policy.append_binding_cas(
        binding=binding,
        expected_binding_revision=None,
        idempotency_key=f"bind-{suffix}",
        request_digest="2" * 64,
    )
    return revision, binding


@pytest.mark.asyncio
async def test_transition_snapshot_uses_live_counts_latest_receipt_and_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _fresh_database(tmp_path / "b10-transition.sqlite3")
    await _seed_subjects()
    rule = _blocking_spec_rule()
    revision = GuidelineRevision(
        revision_id="revision-b10-transition",
        guideline_id="guideline-b10-transition",
        revision_number=1,
        semantic_version="1.0.0",
        title="B10 transition",
        content="Executable transition policy.",
        content_digest=guideline_revision_content_digest(
            title="B10 transition",
            content="Executable transition policy.",
            rules=(rule,),
        ),
        rules=(rule,),
        created_by="owner-b10",
        created_at=NOW,
    )
    binding = BoardGuidelineBinding(
        binding_id="binding-b10-transition",
        board_id=BOARD_ID,
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.content_digest,
        priority=0,
        binding_revision=1,
        adopted_by="owner-b10",
        adopted_at=NOW,
        default_enforcement=GuidelineEnforcement.BLOCKING,
    )
    async with get_session_factory()() as session:
        bare = CommunitySqlAlchemyGuidelinePolicy(session)
        await bare.create_guideline(
            guideline=Guideline(
                guideline_id=revision.guideline_id,
                owner_id="owner-b10",
                scope=GuidelineScope.GLOBAL,
                created_at=NOW,
            ),
            initial_revision=revision,
            initial_head=GuidelineHead(
                guideline_id=revision.guideline_id,
                revision_id=revision.revision_id,
                revision_number=1,
                semantic_version=revision.semantic_version,
                head_revision=1,
                updated_at=NOW,
            ),
            idempotency_key="create-b10-transition",
            request_digest="1" * 64,
        )
        await bare.append_binding_cas(
            binding=binding,
            expected_binding_revision=None,
            idempotency_key="bind-b10-transition",
            request_digest="2" * 64,
        )
        resolver = CommunitySqlAlchemyPolicySubjectSnapshotResolver(
            session,
            resource_gate_ready_resolver=lambda *_args: True,
        )
        subject = await resolver.resolve_subject_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
        )
        current = await resolver.resolve_current_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
        )
        assert subject is not None and current is not None
        evaluation_input = build_policy_evaluation_input_v1(
            evaluation_id="evaluation-b10-transition",
            subject_snapshot=subject,
            bindings=(binding,),
            revisions=(revision,),
            requested_by="agent-b10",
            requested_at=NOW,
            idempotency_key="evaluate-b10-transition",
        )
        output = evaluate_policy(
            evaluation_input,
            revisions=(revision,),
            evaluated_at=NOW,
            evaluated_by="agent-b10",
        )
        result = output.result
        policy = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=resolver,
        )
        await policy.save_evaluation_result(
            result=result,
            current_snapshot=current,
            idempotency_key="persist-b10-transition",
            request_digest="3" * 64,
        )
        transition = await policy.resolve_transition_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
            expected_from_status="draft",
        )
        assert transition.applicable_rule_count == 1
        assert transition.applicable_blocking_rule_count == 1
        assert transition.receipt == result.receipt
        assert transition.current_snapshot == current

        async def evaluation_unavailable(**_scope):
            raise RuntimeError("evaluator unavailable")

        monkeypatch.setattr(
            resolver,
            "_latest_receipt",
            evaluation_unavailable,
        )
        unavailable = await policy.resolve_transition_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
            expected_from_status="draft",
        )
        assert unavailable.evaluation_available is False
        assert unavailable.evaluation_error_code == "policy_evaluation_unavailable"
        assert unavailable.applicable_blocking_rule_count == 1
        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="policy_transition_subject_status_conflict",
        ):
            await policy.resolve_transition_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.SPEC,
                subject_id=SPEC_ID,
                expected_from_status="approved",
            )

        composed = CommunityRelationalApplicationAdapter().guideline_policy(session)
        assert isinstance(
            composed._current_snapshot_resolver,  # noqa: SLF001
            CommunitySqlAlchemyPolicySubjectSnapshotResolver,
        )


@pytest.mark.asyncio
async def test_transition_receipt_requires_its_historical_waiver_to_remain_effective(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b10-waiver-currentness.sqlite3")
    await _seed_subjects()
    clock = datetime.now(timezone.utc)
    rule = GuidelineRule(
        rule_id="rule-b10-waiver",
        code="policy.b10.waiver",
        title="Approved spec",
        description="The spec must already be approved.",
        target_entity_types=(PolicyEntityType.SPEC,),
        predicates=(
            GuidelinePredicate(
                predicate_code="eq",
                parameters=(("fact", "status"), ("value", "approved")),
            ),
        ),
        enforcement=GuidelineEnforcement.BLOCKING,
        waivable=True,
    )
    now = [clock + timedelta(minutes=3)]
    async with get_session_factory()() as session:
        revision, binding = await _install_policy(
            session,
            suffix="b10-waiver",
            rule=rule,
            created_at=clock - timedelta(minutes=1),
        )
        resolver = CommunitySqlAlchemyPolicySubjectSnapshotResolver(
            session,
            resource_gate_ready_resolver=lambda *_args: True,
            utc_now=lambda: now[0],
        )
        policy = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=resolver,
        )
        subject = await resolver.resolve_subject_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
        )
        current = await resolver.resolve_current_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
        )
        assert subject is not None and current is not None
        initial_input = build_policy_evaluation_input_v1(
            evaluation_id="evaluation-b10-waiver-source",
            subject_snapshot=subject,
            bindings=(binding,),
            revisions=(revision,),
            requested_by="agent-b10",
            requested_at=clock,
            idempotency_key="evaluate-b10-waiver-source",
        )
        source_result = evaluate_policy(
            initial_input,
            revisions=(revision,),
            evaluated_at=clock,
            evaluated_by="agent-b10",
        ).result
        await policy.save_evaluation_result(
            result=source_result,
            current_snapshot=current,
            idempotency_key="persist-b10-waiver-source",
            request_digest="3" * 64,
        )
        source = PolicyWaiverSource(
            finding=source_result.receipt.findings[0],
            revision=revision,
            currentness=PolicyCurrentnessAssessment(
                currentness=PolicyCurrentness.CURRENT,
                reasons=(),
            ),
        )
        request = request_policy_waiver(
            event_id="waiver-event-b10-request",
            waiver_id="waiver-b10-currentness",
            source=source,
            requester_id="requester-b10",
            reason="Bounded exception.",
            evidence_refs=("ticket://b10",),
            expires_at=clock + timedelta(hours=1),
            occurred_at=clock + timedelta(minutes=1),
        )
        await policy.create_waiver(
            mutation=request,
            idempotency_key="request-b10-waiver",
            request_digest="4" * 64,
        )
        approval = transition_policy_waiver(
            waiver=request.waiver,
            event_id="waiver-event-b10-approve",
            event_type=PolicyWaiverEventType.APPROVE,
            actor_id="reviewer-b10",
            reason="Independent review.",
            evidence_refs=("review://b10",),
            occurred_at=clock + timedelta(minutes=2),
            expected_waiver_revision=1,
            source=source,
        )
        await policy.transition_waiver_cas(
            mutation=approval,
            expected_waiver_revision=1,
            idempotency_key="approve-b10-waiver",
            request_digest="5" * 64,
        )
        authorization = await policy.resolve_effective_waiver(
            board_id=BOARD_ID,
            guideline_id=revision.guideline_id,
            revision_id=revision.revision_id,
            rule_id=rule.rule_id,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
            subject_version=subject.subject.subject_version,
            evaluated_at=now[0],
        )
        assert authorization is not None
        waived_input = build_policy_evaluation_input_v1(
            evaluation_id="evaluation-b10-waived",
            subject_snapshot=subject,
            bindings=(binding,),
            revisions=(revision,),
            requested_by="agent-b10",
            requested_at=now[0],
            idempotency_key="evaluate-b10-waived",
        )
        waived_result = evaluate_policy(
            waived_input,
            revisions=(revision,),
            waivers=(authorization,),
            evaluated_at=now[0],
            evaluated_by="agent-b10",
        ).result
        await policy.save_evaluation_result(
            result=waived_result,
            current_snapshot=current,
            idempotency_key="persist-b10-waived",
            request_digest="6" * 64,
        )

        effective = await policy.resolve_transition_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
            expected_from_status="draft",
        )
        assert effective.receipt == waived_result.receipt

        now[0] = clock + timedelta(hours=2)
        expired = await policy.resolve_transition_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
            expected_from_status="draft",
        )
        assert expired.receipt is None

        now[0] = clock + timedelta(minutes=5)
        revocation = transition_policy_waiver(
            waiver=approval.waiver,
            event_id="waiver-event-b10-revoke",
            event_type=PolicyWaiverEventType.REVOKE,
            actor_id="reviewer-b10",
            reason="Exception withdrawn.",
            evidence_refs=("review://b10/revoke",),
            occurred_at=clock + timedelta(minutes=4),
            expected_waiver_revision=2,
        )
        await policy.transition_waiver_cas(
            mutation=revocation,
            expected_waiver_revision=2,
            idempotency_key="revoke-b10-waiver",
            request_digest="7" * 64,
        )
        revoked = await policy.resolve_transition_snapshot(
            board_id=BOARD_ID,
            entity_type=PolicyEntityType.SPEC,
            subject_id=SPEC_ID,
            expected_from_status="draft",
        )
        assert revoked.receipt is None


@pytest.mark.asyncio
async def test_locked_path_takes_board_before_subject_and_read_path_does_not(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b10-lock-order.sqlite3")
    await _seed_subjects()
    statements: list[str] = []
    engine = get_engine().sync_engine

    def capture(_connection, _cursor, statement, *_args):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        async with get_session_factory()() as session:
            resolver = CommunitySqlAlchemyPolicySubjectSnapshotResolver(
                session,
                resource_gate_ready_resolver=lambda *_args: True,
            )
            await resolver.resolve_locked_current_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.SPEC,
                subject_id=SPEC_ID,
            )
        board_lock = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith("update boards ")
        )
        subject_read = next(
            index
            for index, statement in enumerate(statements)
            if " from specs " in f" {statement} "
        )
        assert board_lock < subject_read

        statements.clear()
        async with get_session_factory()() as session:
            resolver = CommunitySqlAlchemyPolicySubjectSnapshotResolver(
                session,
                resource_gate_ready_resolver=lambda *_args: True,
            )
            await resolver.resolve_readonly_current_snapshot(
                board_id=BOARD_ID,
                entity_type=PolicyEntityType.SPEC,
                subject_id=SPEC_ID,
            )
        assert not any(
            statement.startswith("update boards ") for statement in statements
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
