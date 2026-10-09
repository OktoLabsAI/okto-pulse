"""Five-metric Spec persistence with native semantic authority and policy gates.

Requirement Lint acceptance is a fixture input; its admission has separate suites.
The policy transition, semantic projection and commits below use real adapters.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    CardStatus,
    CardType,
    Ideation,
    IdeationStatus,
    Refinement,
    RefinementSnapshot,
    RefinementStatus,
    Spec,
    SpecStatus,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.code_traceability import (
    DeliveryContext,
    RefinementDeliveryContextProvenance,
    RefinementSourceContextManifestV2,
    SpecDeliveryContextProvenance,
    build_source_context_summary_v2,
)
from okto_pulse.core.services.main import SpecService
from test_bug_cognitive_context_adapter import _runtime
from test_delivery_reused_impact import register_report_adapters

BOARD_ID = "validation-board-001"
SPEC_ID = "validation-spec-001"
USER_ID = "user-test-001"
ACTOR = ActorContext(
    USER_ID, "rest", actor_kind="human", realm_scope=RealmScope.local()
)


@pytest.fixture
async def db_factory(tmp_path, monkeypatch):
    engine, _ = await _runtime(tmp_path / "spec-validation.db")
    register_report_adapters()
    from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
        CommunitySqlAlchemyResourceGateAdapter,
    )
    from okto_pulse.core.ports.relational_services import (
        register_resource_gate_adapter_factory,
    )

    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)

    async def accepted_lint(_service, _spec):
        return None

    monkeypatch.setattr(
        SpecService, "_enforce_spec_requirement_lint_gate", accepted_lint
    )
    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        sync_session_class=CommunitySemanticSession,
        info={"realm_scope": RealmScope.local()},
    )
    from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
        CommunitySqlAlchemyKnowledgePropagationStore,
    )
    from okto_pulse.core.ports.knowledge_propagation import (
        register_knowledge_propagation_port,
    )

    register_knowledge_propagation_port(
        CommunitySqlAlchemyKnowledgePropagationStore(factory)
    )
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_board(
    db_factory, board_id=None, spec_id=None, *, execution_ready=False
) -> None:
    """Create a board with ideation → refinement → spec chain.

    Idempotent — skips if board already seeded.
    Uses global BOARD_ID/SPEC_ID if not provided.
    """
    if board_id is None:
        board_id = BOARD_ID
    if spec_id is None:
        spec_id = SPEC_ID
    return await _seed_board_with_ids(
        db_factory, board_id, spec_id, execution_ready=execution_ready
    )


async def _seed_native_checklist_board(db, board_id):
    """Model the explicit Advisory binding installed by native Board creation."""
    from okto_pulse.community.adapters.sqlalchemy_checklist import CommunitySqlAlchemyChecklist
    from sqlalchemy import select
    from okto_pulse.core.domain.checklist import ChecklistMode, ChecklistPhase, ChecklistTargetType
    from okto_pulse.core.services.checklist import ChecklistService

    adapter = CommunitySqlAlchemyChecklist(db)
    service = ChecklistService()
    binding = service.prepare_binding(
        board_id=board_id, mode=ChecklistMode.ADVISORY, current_binding=None,
    )
    await service.apply_binding(binding, previous_binding=None, persistence=adapter)
    # This suite seeds admitted Specs directly. Their native lifecycle pin is
    # fixture setup, not a reader-side backfill in the product.
    specs = (await db.execute(select(Spec).where(Spec.board_id == board_id))).scalars().all()
    for spec in specs:
        if spec.status in {SpecStatus.APPROVED, SpecStatus.VALIDATED, SpecStatus.IN_PROGRESS, SpecStatus.DONE}:
            await adapter.freeze_validation_binding(
                board_id=board_id, spec_id=spec.id, spec_edition=spec.edition,
                target_type=ChecklistTargetType.SPEC, phase=ChecklistPhase.SPEC_VALIDATION,
            )


async def _seed_board_with_ids(
    db_factory, board_id, spec_id, *, execution_ready=False
) -> None:
    """Create a board with ideation → refinement → spec chain.

    Idempotent — skips if board already seeded.
    """
    async with db_factory() as db:
        existing = await db.get(Board, board_id)
        if existing is not None:
            return

    ideation_id = str(uuid.uuid4())
    ref_id = str(uuid.uuid4())
    refinement_snapshot_id = str(uuid.uuid4())
    card_impl_id = str(uuid.uuid4())
    card_test_id = str(uuid.uuid4())
    refinement_context_provenance = RefinementDeliveryContextProvenance(
        value=DeliveryContext.BROWNFIELD,
        source_refinement_id=ref_id,
        source_refinement_version=1,
    )
    source_context = RefinementSourceContextManifestV2(
        refinement_id=ref_id,
        refinement_version=1,
        summary=build_source_context_summary_v2(
            delivery_context=DeliveryContext.BROWNFIELD,
            delivery_context_provenance=refinement_context_provenance,
            current_investigation_outcomes=(),
            evidence=(),
        ),
        current_receipts=(),
    )
    spec_context_provenance = SpecDeliveryContextProvenance(
        value=DeliveryContext.BROWNFIELD,
        inherited_value=DeliveryContext.BROWNFIELD,
        source_refinement_id=ref_id,
        source_refinement_version=1,
    )
    async with db_factory() as db:
        from okto_pulse.core.domain.execution_contract import new_execution_contract

        db.add(
            Board(
                id=board_id,
                name="Validation Gate Board",
                owner_id=USER_ID,
                realm_id="local",
                settings={
                    "require_spec_validation": True,
                    "min_spec_confidence": 70,
                    "min_spec_assertiveness": 80,
                    "max_spec_ambiguity": 30,
                    "delivery_evidence_gate": "advisory"
                    if execution_ready
                    else "blocking",
                },
            )
        )
        db.add(
            Ideation(
                id=ideation_id,
                board_id=board_id,
                title="Validation Gate Ideation",
                status=IdeationStatus.DONE,
                archived=False,
                created_by=USER_ID,
            )
        )
        db.add(
            Refinement(
                id=ref_id,
                ideation_id=ideation_id,
                board_id=board_id,
                title="Validation Gate Refinement",
                status=RefinementStatus.DONE,
                archived=False,
                delivery_context=DeliveryContext.BROWNFIELD.value,
                created_by=USER_ID,
            )
        )
        db.add(
            RefinementSnapshot(
                id=refinement_snapshot_id,
                refinement_id=ref_id,
                version=1,
                title="Validation Gate Refinement",
                delivery_context=DeliveryContext.BROWNFIELD.value,
                qa_snapshot=[],
                code_evidence_manifest=[],
                source_context_manifest=source_context.as_dict(),
                source_context_sha256=source_context.payload_sha256,
                created_by=USER_ID,
            )
        )
        db.add(
            Spec(
                id=spec_id,
                board_id=board_id,
                ideation_id=ideation_id,
                refinement_id=ref_id,
                title="Validation Gate Spec",
                execution_contract=new_execution_contract(
                    board_id=board_id,
                    spec_id=spec_id,
                    edition=1,
                    actor_id=USER_ID,
                    origin="new_spec",
                ),
                evaluations=(
                    [
                        {
                            "spec_edition": 1,
                            "evaluator_id": "reviewer",
                            "recommendation": "approve",
                            "overall_score": 100,
                        }
                    ]
                    if execution_ready
                    else []
                ),
                skip_qualitative_validation=execution_ready,
                architecture_adoption={
                    "contract_version": "architecture-adoption/v1",
                    "board_id": board_id,
                    "spec_id": spec_id,
                    "adopted_in_edition": 1,
                    "actor_id": USER_ID,
                    "inherited_resource_ids": [],
                },
                status=SpecStatus.APPROVED,
                archived=False,
                delivery_context=DeliveryContext.BROWNFIELD.value,
                delivery_context_provenance={
                    "value": spec_context_provenance.value.value,
                    "inherited_value": (spec_context_provenance.inherited_value.value),
                    "source_refinement_id": (
                        spec_context_provenance.source_refinement_id
                    ),
                    "source_refinement_version": (
                        spec_context_provenance.source_refinement_version
                    ),
                    "override_reason": None,
                },
                source_refinement_snapshot_id=refinement_snapshot_id,
                source_refinement_version=1,
                source_context_manifest=source_context.as_dict(),
                source_context_sha256=source_context.payload_sha256,
                skip_test_coverage=True,
                acceptance_criteria=[
                    {"id": "ac_0", "text": "AC1: System returns 200 on health check"},
                    {"id": "ac_1", "text": "AC2: System returns 401 on invalid token"},
                    {
                        "id": "ac_2",
                        "text": "AC3: System returns 404 on unknown resource",
                    },
                ],
                functional_requirements=[
                    {"id": "fr_0", "text": "FR1: Health endpoint exists"},
                    {"id": "fr_1", "text": "FR2: Authentication required"},
                    {"id": "fr_2", "text": "FR3: Resource not found handling"},
                ],
                test_scenarios=[
                    {
                        "id": "ts_health",
                        "title": "Health check returns 200",
                        "given": "Server is running",
                        "when": "GET /health",
                        "then": "Returns 200 OK",
                        "scenario_type": "integration",
                        "linked_criteria": ["ac_0"],
                        "linked_task_ids": [card_impl_id],
                    },
                    {
                        "id": "ts_auth",
                        "title": "Invalid token returns 401",
                        "given": "Client sends invalid token",
                        "when": "GET /resource",
                        "then": "Returns 401 Unauthorized",
                        "scenario_type": "integration",
                        "linked_criteria": ["ac_1"],
                        "linked_task_ids": [card_impl_id],
                    },
                    {
                        "id": "ts_notfound",
                        "title": "Unknown resource returns 404",
                        "given": "Client requests unknown path",
                        "when": "GET /unknown",
                        "then": "Returns 404 Not Found",
                        "scenario_type": "integration",
                        "linked_criteria": ["ac_2"],
                        "linked_task_ids": [card_impl_id],
                    },
                ],
                business_rules=[
                    {
                        "id": "br_health",
                        "title": "Health endpoint exists",
                        "rule": "Health endpoint must return 200",
                        "when": "GET /health is called",
                        "then": "Return 200 OK",
                        "linked_requirements": ["fr_0"],
                        "linked_task_ids": [card_impl_id],
                    },
                    {
                        "id": "br_auth",
                        "title": "Authentication required",
                        "rule": "All endpoints require valid token",
                        "when": "Request is made without token",
                        "then": "Return 401",
                        "linked_requirements": ["fr_1"],
                        "linked_task_ids": [card_impl_id],
                    },
                    {
                        "id": "br_notfound",
                        "title": "Resource not found handling",
                        "rule": "Unknown resources return 404",
                        "when": "Resource does not exist",
                        "then": "Return 404 with message",
                        "linked_requirements": ["fr_2"],
                        "linked_task_ids": [card_impl_id],
                    },
                ],
                technical_requirements=[
                    {
                        "id": "tr_1",
                        "text": "Must use JWT auth",
                        "linked_task_ids": [card_impl_id],
                    },
                    {
                        "id": "tr_2",
                        "text": "Response time < 200ms",
                        "linked_task_ids": [card_impl_id],
                    },
                ],
                api_contracts=[
                    {
                        "id": "api_1",
                        "method": "GET",
                        "path": "/health",
                        "description": "Health check endpoint",
                        "request_body": None,
                        "response_success": {"status": 200, "message": "ok"},
                        "response_errors": [
                            {"status": 500, "detail": "internal error"}
                        ],
                        "linked_requirements": ["fr_0"],
                        "linked_rules": [],
                        "linked_task_ids": [card_impl_id],
                    },
                ],
                decisions=[
                    {
                        "id": "dec_1",
                        "title": "Use JWT",
                        "status": "active",
                        "linked_task_ids": [card_impl_id],
                    },
                ],
                created_by=USER_ID,
            )
        )
        yesterday = datetime.now(timezone.utc)
        db.add(
            Card(
                id=card_impl_id,
                board_id=board_id,
                spec_id=spec_id,
                title="Implementation card",
                status=CardStatus.DONE,
                card_type=CardType.NORMAL,
                archived=False,
                created_by=USER_ID,
                created_at=yesterday,
                updated_at=yesterday,
            )
        )
        # Test card — required by check_test_coverage for scenarios with linked_task_ids
        db.add(
            Card(
                id=card_test_id,
                board_id=board_id,
                spec_id=spec_id,
                title="Test card",
                status=CardStatus.DONE,
                card_type=CardType.TEST,
                archived=False,
                created_by=USER_ID,
                created_at=yesterday,
                updated_at=yesterday,
            )
        )
        async with CommunityUnitOfWork(
            db, actor=ACTOR, realm_scope=RealmScope.local()
        ) as uow:
            await db.flush()
            await _seed_native_checklist_board(db, board_id)
            await uow.commit()


def _canonical_submit_data(
    *,
    confidence: int = 90,
    clarity: int = 90,
    assertiveness: int = 90,
    decidability: int = 90,
    ambiguity: int = 10,
    recommendation: str = "approve",
) -> dict:
    from okto_pulse.core.domain.spec_validation import SpecValidationPinpoint
    from okto_pulse.core.domain.guideline_semantic_v2 import (
        AnchorSnapshot,
        SemanticAnchorAvailability,
    )

    # The service receives snapshots sealed by its public use case. Admission
    # of anchors is covered by transport/use-case suites; this is fixture input.
    pinpoint = (
        SpecValidationPinpoint.from_dict(
            {
                "metrics": ["decidability"], "kind": "problem", "severity": "medium",
                "excerpt": "Required scaling bounds", "recommendation": "Specify the expected measurable bounds.",
                "anchor_type": "field",
                "anchor_ref": "technical_requirements.tr_availability",
                "detail": "State the required scaling bounds.",
            }
        )
        .seal(
            AnchorSnapshot(
                label="Availability",
                excerpt="Required scaling bounds",
                source_version="1",
                availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
            )
        )
        .to_dict()
    )
    return {
        "confidence": confidence,
        "confidence_justification": "The evaluator inspected the complete Spec.",
        "clarity": clarity,
        "clarity_justification": "Problem, solution and requirements are explicit.",
        "assertiveness": assertiveness,
        "assertiveness_justification": "Requirements use measurable and testable language.",
        "decidability": decidability,
        "decidability_justification": "Constraints lead to concrete implementation choices.",
        "ambiguity": ambiguity,
        "ambiguity_justification": "Defined terms have one interpretation in context.",
        "recommendation": recommendation,
        "pinpoints": [pinpoint],
    }


async def _submit_spec_validation(service, db, *args, **kwargs):
    """Model the caller-owned transaction used by the application UoW."""
    data = dict(kwargs.get("data") or {})
    spec_id = kwargs.get("spec_id") or (args[0] if args else None)
    spec = await service.get_spec(spec_id)
    if spec is not None:
        data.setdefault("expected_validation_edition", spec.edition)
        data.setdefault("expected_spec_version", spec.version)
        data.setdefault(
            "expected_head_revision",
            max(
                (
                    int(item.get("head_revision", 0))
                    for item in (spec.validations or [])
                    if item.get("edition") == spec.edition
                ),
                default=0,
            ),
        )
    kwargs["data"] = data
    async with CommunityUnitOfWork(
        db, actor=ACTOR, realm_scope=RealmScope.local()
    ) as uow:
        result = await service.submit_spec_validation(*args, **kwargs)
        await uow.commit()
    return result


@pytest.mark.asyncio
class TestCanonicalFiveMetricGate:
    async def test_canonical_scores_justifications_and_pinpoints_are_persisted(
        self,
        db_factory,
    ):
        await _seed_board(db_factory)
        async with db_factory() as db:
            result = await _submit_spec_validation(
                SpecService(db),
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Evaluator Agent",
                data=_canonical_submit_data(),
            )

        assert result["outcome"] == "success"
        assert result["spec_status"] == "validated"
        assert result["confidence"] == 90
        assert result["clarity"] == 90
        assert result["assertiveness"] == 90
        assert result["decidability"] == 90
        assert result["ambiguity"] == 10
        assert result["pinpoints"] == _canonical_submit_data()["pinpoints"]
        assert result["resolved_thresholds"] == {
            "min_spec_confidence": 70,
            "min_spec_clarity": 80,
            "min_spec_assertiveness": 80,
            "min_spec_decidability": 80,
            "max_spec_ambiguity": 30,
        }
        assert "min_spec_completeness" not in result["resolved_thresholds"]
        async with db_factory() as reader:
            projected = await SpecService(reader).list_spec_validations(SPEC_ID)
            assert projected["current_validation"]["id"] == result["id"]
            stored = await reader.get(Spec, SPEC_ID)
            assert stored.status == SpecStatus.VALIDATED
            assert stored.current_validation_id == result["id"]
            assert stored.validations[-1]["pinpoints"] == result["pinpoints"]
            for metric in (
                "confidence",
                "clarity",
                "assertiveness",
                "decidability",
                "ambiguity",
            ):
                assert stored.validations[-1][metric] == result[metric]
                assert (
                    stored.validations[-1][metric + "_justification"]
                    == result[metric + "_justification"]
                )

    async def test_every_canonical_threshold_participates_in_gate_outcome(
        self,
        db_factory,
    ):
        await _seed_board(db_factory)
        async with db_factory() as db:
            result = await _submit_spec_validation(
                SpecService(db),
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Evaluator Agent",
                data=_canonical_submit_data(
                    confidence=69,
                    clarity=79,
                    assertiveness=79,
                    decidability=79,
                    ambiguity=31,
                ),
            )

        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"
        assert result["threshold_violations"] == [
            "confidence 69 < min 70",
            "clarity 79 < min 80",
            "assertiveness 79 < min 80",
            "decidability 79 < min 80",
            "ambiguity 31 > max 30",
        ]
        async with db_factory() as reader:
            stored = await reader.get(Spec, SPEC_ID)
            assert stored.status == SpecStatus.APPROVED
            assert stored.validations[-1]["outcome"] == "failed"
            assert (
                stored.validations[-1]["threshold_violations"]
                == result["threshold_violations"]
            )
