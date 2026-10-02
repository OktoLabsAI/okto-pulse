"""Spec Validation lifecycle cases on native Community persistence.

Transferred from the former Core SQL fixture suite. Requirement Lint acceptance
is fixture input; actual policy, semantic authority and persistence remain active.
"""

from __future__ import annotations

import uuid
from datetime import datetime
import pytest
from sqlalchemy import func, select
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    DomainEventRow,
    Spec,
    SpecHistory,
    SpecStatus,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.human_validation_cycle import SubjectEditRequiresDraftError
from okto_pulse.core.domain.spec_validation import (
    RequirementLintRequired,
    SpecValidationGateNotReady,
)
from okto_pulse.core.models.schemas import SpecMove, SpecUpdate
from okto_pulse.core.services.main import (
    CardService,
    SpecService,
    spec_is_content_locked,
)
from okto_pulse.core.services import main as main_service
from test_spec_validation_native_gate import (
    db_factory as _native_db_factory,
    _seed_board,
    _seed_board_with_ids,
    _canonical_submit_data,
    _submit_spec_validation,
    ACTOR,
    BOARD_ID,
    SPEC_ID,
    USER_ID,
)

db_factory = _native_db_factory


def _new_spec(**fields):
    """Create a current direct-Spec fixture with explicit empty architecture scope."""
    from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
    from okto_pulse.core.domain.execution_contract import new_execution_contract
    from okto_pulse.core.domain.code_traceability import (
        DeliveryContext,
        DirectSpecDeliveryContextProvenance,
        build_direct_spec_source_context_manifest,
    )

    board_id, spec_id = fields["board_id"], fields["id"]
    provenance = DirectSpecDeliveryContextProvenance(
        value=DeliveryContext.GREENFIELD, source_spec_id=spec_id, source_spec_version=1
    )
    manifest, digest = build_direct_spec_source_context_manifest(
        spec_id=spec_id,
        delivery_context=DeliveryContext.GREENFIELD,
        provenance=provenance,
    )
    return Spec(
        **fields,
        architecture_adoption=ArchitectureAdoptionScope(
            board_id=board_id,
            spec_id=spec_id,
            adopted_in_edition=1,
            actor_id=USER_ID,
            inherited_resource_ids=(),
        ).model_dump(mode="json"),
        execution_contract=new_execution_contract(
            board_id=board_id,
            spec_id=spec_id,
            edition=1,
            actor_id=USER_ID,
            origin="new_spec",
        ),
        delivery_context="greenfield",
        delivery_context_provenance={
            "value": "greenfield",
            "source_spec_id": spec_id,
            "source_spec_version": 1,
        },
        source_context_manifest=manifest,
        source_context_sha256=digest,
    )


def _valid_submit_data(
    confidence=90, assertiveness=85, ambiguity=15, recommendation="approve"
):
    return _canonical_submit_data(
        confidence=confidence,
        assertiveness=assertiveness,
        ambiguity=ambiguity,
        recommendation=recommendation,
    )


async def _commit(db):
    async with CommunityUnitOfWork(
        db, actor=ACTOR, realm_scope=RealmScope.local()
    ) as uow:
        await uow.commit()


async def _move_spec(service, db, *args, **kwargs):
    async with CommunityUnitOfWork(
        db, actor=ACTOR, realm_scope=RealmScope.local()
    ) as uow:
        result = await service.move_spec(*args, **kwargs)
        await uow.commit()
    return result


async def _update_spec(service, db, *args, **kwargs):
    async with CommunityUnitOfWork(
        db, actor=ACTOR, realm_scope=RealmScope.local()
    ) as uow:
        result = await service.update_spec(*args, **kwargs)
        await uow.commit()
    return result


@pytest.mark.asyncio
class TestStateGuard:
    """Spec must be in 'approved' status to submit validation."""

    async def test_approved_status_allows_submit(self, db_factory):
        """Spec in 'approved' status should accept validation submission."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            history = (
                await db.execute(
                    select(SpecHistory).where(SpecHistory.spec_id == SPEC_ID)
                )
            ).scalar_one()
        assert result["outcome"] == "success"
        assert result["spec_status"] == "validated"
        assert history.action == "validation_submitted"
        assert history.changes == [
            {
                "field": "current_validation_id",
                "old": None,
                "new": result["id"],
            },
            {"field": "status", "old": "approved", "new": "validated"},
        ]

    async def test_code_evidence_coverage_blocks_submit_before_validation_write(
        self,
        db_factory,
        monkeypatch,
    ):
        from okto_pulse.core.domain.code_traceability import (
            CodeTraceabilityContractError,
        )

        await _seed_board(db_factory)

        async def blocked(_service, _spec, _board):
            raise CodeTraceabilityContractError(
                "code_evidence_disposition_required",
                "Every active inherited Evidence needs a link or final disposition.",
                details={
                    "reason": "matrix_pending",
                    "evidence_pending_ids": ["evidence-1", "evidence-2"],
                    "evidence_disposition_coverage_pct": 0.0,
                },
            )

        monkeypatch.setattr(
            CardService,
            "check_code_evidence_coverage",
            blocked,
        )
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(SpecValidationGateNotReady) as raised:
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )
            spec = await service.get_spec(SPEC_ID)

        assert raised.value.details["reason"] == "code_evidence_disposition_required"
        assert raised.value.details["technical_reason"] == "matrix_pending"
        assert raised.value.details["evidence_pending_ids"] == [
            "evidence-1",
            "evidence-2",
        ]
        assert raised.value.details["evidence_disposition_coverage_pct"] == 0.0
        assert spec.status == SpecStatus.APPROVED
        assert spec.current_validation_id is None
        assert spec.validations in (None, [])

    async def test_lost_lifecycle_fence_appends_nothing(
        self,
        db_factory,
        monkeypatch,
    ):
        """A concurrent validation/reopen loses cleanly before any side effect."""

        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            history_before = await db.scalar(
                select(func.count()).select_from(SpecHistory)
            )
            events_before = await db.scalar(
                select(func.count()).select_from(DomainEventRow)
            )

            async def lost_fence(*_args, **_kwargs):
                return False

            recorded_allow_decisions: list[object] = []

            async def record_allow(*_args, **kwargs):
                recorded_allow_decisions.append(kwargs["decision"])

            monkeypatch.setattr(main_service, "_application_fence", lost_fence)
            monkeypatch.setattr(
                main_service,
                "_record_critical_context_decision",
                record_allow,
            )
            with pytest.raises(SpecValidationGateNotReady) as raised:
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )

            spec = await service.get_spec(SPEC_ID)
            assert raised.value.details == {"reason": "lifecycle_fence_conflict"}
            assert spec.status == SpecStatus.APPROVED
            assert spec.validations in (None, [])
            assert spec.current_validation_id is None
            assert recorded_allow_decisions == []
            assert (
                await db.scalar(select(func.count()).select_from(SpecHistory))
                == history_before
            )
            assert (
                await db.scalar(select(func.count()).select_from(DomainEventRow))
                == events_before
            )

    async def test_lint_head_replaced_after_preflight_blocks_promotion(
        self,
        db_factory,
        monkeypatch,
    ):
        """The post-fence head read is authoritative for this edition."""

        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            lint_reads = 0
            recorded_allow_decisions: list[object] = []

            async def replaced_lint(_spec):
                nonlocal lint_reads
                lint_reads += 1
                if lint_reads == 2:
                    raise RequirementLintRequired(
                        "Requirement Lint was replaced before promotion."
                    )

            async def record_allow(*_args, **kwargs):
                recorded_allow_decisions.append(kwargs["decision"])

            service._enforce_spec_requirement_lint_gate = replaced_lint  # type: ignore[method-assign]
            monkeypatch.setattr(
                main_service,
                "_record_critical_context_decision",
                record_allow,
            )

            with pytest.raises(RequirementLintRequired):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )

            spec = await service.get_spec(SPEC_ID)
            assert lint_reads == 2
            assert recorded_allow_decisions == []
            assert spec.status == SpecStatus.APPROVED
            assert spec.validations in (None, [])
            assert spec.current_validation_id is None

    async def test_successful_submit_emits_status_consolidation_event(self, db_factory):
        """Spec validation promotion must re-enqueue KG consolidation."""
        board_id = str(uuid.uuid4())
        spec_id = str(uuid.uuid4())
        await _seed_board_with_ids(db_factory, board_id, spec_id)
        async with db_factory() as db:
            await db.execute(
                DomainEventRow.__table__.delete().where(
                    DomainEventRow.board_id == board_id
                )
            )
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            events = (
                (
                    await db.execute(
                        select(DomainEventRow).where(
                            DomainEventRow.board_id == board_id
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert result["spec_status"] == "validated"
        by_type = {event.event_type: event.payload_json for event in events}
        assert by_type["spec.moved"] == {
            "spec_id": spec_id,
            "from_status": "approved",
            "to_status": "validated",
        }
        assert by_type["spec.semantic_changed"] == {
            "spec_id": spec_id,
            "changed_fields": ["status"],
            "projection_card_ids": [],
        }

    async def test_draft_status_rejects_submit(self, db_factory):
        """Spec in 'draft' status must raise ValueError."""
        spec_id = str(uuid.uuid4())
        board_id = str(uuid.uuid4())
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Validation Gate Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 80,
                        "min_spec_assertiveness": 80,
                        "max_spec_ambiguity": 30,
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="Draft Spec",
                    status=SpecStatus.DRAFT,
                    archived=False,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            with pytest.raises(ValueError, match="'draft'"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )

    async def test_in_progress_status_rejects_submit(self, db_factory):
        """Spec in 'in_progress' status must raise ValueError."""
        spec_id = str(uuid.uuid4())
        board_id = str(uuid.uuid4())
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Validation Gate Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 80,
                        "min_spec_assertiveness": 80,
                        "max_spec_ambiguity": 30,
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="In Progress Spec",
                    status=SpecStatus.IN_PROGRESS,
                    archived=False,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            with pytest.raises(ValueError, match="'in_progress'"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )

    async def test_done_status_rejects_submit(self, db_factory):
        """Spec in 'done' status must raise ValueError."""
        spec_id = str(uuid.uuid4())
        board_id = str(uuid.uuid4())
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Validation Gate Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 80,
                        "min_spec_assertiveness": 80,
                        "max_spec_ambiguity": 30,
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="Done Spec",
                    status=SpecStatus.DONE,
                    archived=False,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            with pytest.raises(ValueError, match="'done'"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )

    async def test_validated_status_rejects_submit(self, db_factory):
        """Spec already in 'validated' status must raise ValueError."""
        spec_id = str(uuid.uuid4())
        board_id = str(uuid.uuid4())
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Validation Gate Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 80,
                        "min_spec_assertiveness": 80,
                        "max_spec_ambiguity": 30,
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="Validated Spec",
                    status=SpecStatus.VALIDATED,
                    archived=False,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            with pytest.raises(ValueError, match="'validated'"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )

    async def test_review_status_rejects_submit(self, db_factory):
        """Spec in 'review' status must raise ValueError."""
        spec_id = str(uuid.uuid4())
        board_id = str(uuid.uuid4())
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Validation Gate Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 80,
                        "min_spec_assertiveness": 80,
                        "max_spec_ambiguity": 30,
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="Review Spec",
                    status=SpecStatus.REVIEW,
                    archived=False,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            with pytest.raises(ValueError, match="'review'"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )

    async def test_nonexistent_spec_rejects_submit(self, db_factory):
        """Submitting validation for a nonexistent spec must raise ValueError."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(ValueError, match="not found"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id="nonexistent-spec",
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )


# ===========================================================================
# 2. Threshold pass — all scores meet thresholds + approve → success
# ===========================================================================


@pytest.mark.asyncio
class TestThresholdPass:
    """All scores meet thresholds + recommendation=approve → spec becomes validated."""

    async def test_all_thresholds_pass(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(
                    confidence=90,
                    assertiveness=85,
                    ambiguity=15,
                ),
            )
        assert result["outcome"] == "success"
        assert result["spec_status"] == "validated"
        assert result["threshold_violations"] == []
        assert result["recommendation"] == "approve"

    async def test_boundary_scores_pass(self, db_factory):
        """Scores exactly at threshold boundaries should pass."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 70,  # exactly at min
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 80,  # exactly at min
                    "assertiveness_justification": "FRs are measurable with no weasel words",
                    "ambiguity": 30,  # exactly at max
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Spec is ready for execution with high confidence",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "success"
        assert result["spec_status"] == "validated"

    async def test_max_scores_pass(self, db_factory):
        """Maximum scores (100/100/0) should pass."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 100,
                    "confidence_justification": "Perfect confidence across all areas",
                    "assertiveness": 100,
                    "assertiveness_justification": "Every requirement is measurable",
                    "ambiguity": 0,
                    "ambiguity_justification": "Zero ambiguity — all terms defined",
                    "clarity_justification": "Spec is ready for execution with high confidence",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "success"
        assert result["spec_status"] == "validated"

    async def test_spec_persists_validated_status(self, db_factory):
        """After successful validation, spec status is persisted as 'validated'."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            # Re-fetch spec to verify persistence
            spec = await service.get_spec(SPEC_ID)
        assert spec.status == SpecStatus.VALIDATED
        assert spec.current_validation_id is not None


# ===========================================================================
# 3. Threshold fail — confidence < 70
# ===========================================================================


@pytest.mark.asyncio
class TestThresholdFailConfidence:
    """Confidence below threshold → validation fails, spec stays approved."""

    async def test_confidence_below_threshold(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 62,
                    "confidence_justification": "Edge case scenarios are still missing",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 15,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Scores are low in confidence but I approve overall",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"
        assert len(result["threshold_violations"]) == 1
        assert "confidence" in result["threshold_violations"][0]
        assert "62" in result["threshold_violations"][0]

    async def test_confidence_zero_fails(self, db_factory):
        """Confidence of 0 should fail."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 0,
                    "confidence_justification": "No coverage at all",
                    "assertiveness": 100,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 0,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Zero confidence but I approve overall",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"

    async def test_spec_status_unchanged_on_failure(self, db_factory):
        """Spec status remains 'approved' after a failed validation."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 50,
                    "confidence_justification": "Half the ACs are covered",
                    "assertiveness": 100,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 0,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Half coverage but I approve overall",
                    "recommendation": "approve",
                },
            )
            spec = await service.get_spec(SPEC_ID)
        assert spec.status == SpecStatus.APPROVED


# ===========================================================================
# 4. Threshold fail — assertiveness < 80
# ===========================================================================


@pytest.mark.asyncio
class TestThresholdFailAssertiveness:
    """Assertiveness below threshold → validation fails, spec stays approved."""

    async def test_assertiveness_below_threshold(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 90,
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 70,
                    "assertiveness_justification": "Some FRs use vague language",
                    "ambiguity": 15,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Good confidence but assertiveness is low",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"
        assert len(result["threshold_violations"]) == 1
        assert "assertiveness" in result["threshold_violations"][0]

    async def test_assertiveness_zero_fails(self, db_factory):
        """Assertiveness of 0 should fail."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 100,
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 0,
                    "assertiveness_justification": "No measurable criteria",
                    "ambiguity": 0,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Perfect confidence but no assertiveness",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"


# ===========================================================================
# 5. Threshold fail — ambiguity > 30
# ===========================================================================


@pytest.mark.asyncio
class TestThresholdFailAmbiguity:
    """Ambiguity above threshold → validation fails, spec stays approved."""

    async def test_ambiguity_above_threshold(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 90,
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 45,
                    "ambiguity_justification": "Many terms are undefined",
                    "clarity_justification": "Good scores but ambiguity is too high",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"
        assert len(result["threshold_violations"]) == 1
        assert "ambiguity" in result["threshold_violations"][0]
        assert "45" in result["threshold_violations"][0]

    async def test_ambiguity_at_boundary_passes(self, db_factory):
        """Ambiguity of exactly 30 should pass (it's the max)."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 90,
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 30,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Spec is ready for execution with high confidence",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "success"
        assert result["spec_status"] == "validated"

    async def test_ambiguity_one_above_boundary_fails(self, db_factory):
        """Ambiguity of 31 should fail (one above max)."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 90,
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 31,
                    "ambiguity_justification": "Some terms are still vague",
                    "clarity_justification": "Almost perfect but ambiguity is slightly too high",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"


# ===========================================================================
# 6. Recommendation reject
# ===========================================================================


@pytest.mark.asyncio
class TestRecommendationReject:
    """Even with passing thresholds, recommendation=reject → fails."""

    async def test_reject_override_with_passing_thresholds(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 95,
                    "confidence_justification": "Excellent confidence across all areas",
                    "assertiveness": 95,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 5,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Scores are excellent but I still reject this spec",
                    "recommendation": "reject",
                },
            )
        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"
        assert result["threshold_violations"] == []  # no threshold violations
        assert result["recommendation"] == "reject"

    async def test_reject_preserves_spec_status(self, db_factory):
        """Spec remains in 'approved' after a reject validation."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 95,
                    "confidence_justification": "Excellent confidence across all areas",
                    "assertiveness": 95,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 5,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Scores are excellent but I still reject this spec",
                    "recommendation": "reject",
                },
            )
            spec = await service.get_spec(SPEC_ID)
        assert spec.status == SpecStatus.APPROVED
        assert spec.current_validation_id is not None  # pointer still set


# ===========================================================================
# 7. Append-only history
# ===========================================================================


@pytest.mark.asyncio
class TestAppendOnlyHistory:
    """Multiple submissions create multiple validation records."""

    async def test_multiple_submissions_append(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            # First submission: fails (low confidence)
            result1 = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 60,
                    "confidence_justification": "Half the ACs are covered",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 10,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Low confidence but I approve overall",
                    "recommendation": "approve",
                },
            )
            assert result1["outcome"] == "failed"

            # Second submission: passes
            result2 = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            assert result2["outcome"] == "success"

            # Open a new Draft edition, then return to Approved for submission.
            await _move_spec(
                service,
                db,
                SPEC_ID,
                USER_ID,
                SpecMove(status=SpecStatus.DRAFT),
            )
            await _move_spec(
                service,
                db,
                SPEC_ID,
                USER_ID,
                SpecMove(status=SpecStatus.REVIEW),
            )
            await _move_spec(
                service,
                db,
                SPEC_ID,
                USER_ID,
                SpecMove(status=SpecStatus.APPROVED),
            )

            # Third submission: fails again (reject)
            result3 = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 95,
                    "confidence_justification": "Excellent confidence across all areas",
                    "assertiveness": 95,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 5,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Scores are excellent but I still reject this spec",
                    "recommendation": "reject",
                },
            )
            assert result3["outcome"] == "failed"

            # Verify all three records exist
            spec = await service.get_spec(SPEC_ID)
        assert len(spec.validations) == 3

    async def test_validation_ids_are_unique(self, db_factory):
        """Each validation record gets a unique ID."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            results = []
            for i in range(3):
                result = await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": 90,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        "assertiveness": 90,
                        "assertiveness_justification": "FRs are measurable and testable",
                        "ambiguity": 10,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "reject",  # always reject to stay in approved
                    },
                )
                results.append(result)
        ids = [r["id"] for r in results]
        assert len(set(ids)) == 3  # all unique

    async def test_current_validation_id_points_to_latest(self, db_factory):
        """After multiple submissions, current_validation_id points to the last one."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 60,
                    "confidence_justification": "Half the ACs are covered",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 10,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Low confidence but I approve overall",
                    "recommendation": "approve",
                },
            )
            result2 = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            spec = await service.get_spec(SPEC_ID)
        assert spec.current_validation_id == result2["id"]

    async def test_validation_records_preserved_on_status_change(self, db_factory):
        """Moving spec back to draft preserves all validation records."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            # Submit a successful validation → spec becomes validated
            await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            spec = await service.get_spec(SPEC_ID)
            validation_count = len(spec.validations)

            # Move back to draft
            await _move_spec(
                service,
                db,
                SPEC_ID,
                USER_ID,
                SpecMove(status=SpecStatus.DRAFT),
            )

            # Move back through review to approved so we can submit again
            await _move_spec(
                service,
                db,
                SPEC_ID,
                USER_ID,
                SpecMove(status=SpecStatus.REVIEW),
            )
            await _move_spec(
                service,
                db,
                SPEC_ID,
                USER_ID,
                SpecMove(status=SpecStatus.APPROVED),
            )

            # Submit another validation
            await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 90,
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 10,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Spec is ready for execution with high confidence",
                    "recommendation": "reject",
                },
            )

            spec = await service.get_spec(SPEC_ID)
        assert len(spec.validations) == validation_count + 1


# ===========================================================================
# 8. Content lock
# ===========================================================================


@pytest.mark.asyncio
class TestContentLock:
    """After successful validation, edits require reopening Draft."""

    async def test_update_spec_blocked_after_success(self, db_factory):
        content_lock_board_id = str(uuid.uuid4())
        content_lock_spec_id = str(uuid.uuid4())
        await _seed_board(
            db_factory, board_id=content_lock_board_id, spec_id=content_lock_spec_id
        )
        async with db_factory() as db:
            service = SpecService(db)
            # First, pass validation
            await _submit_spec_validation(
                service,
                db,
                spec_id=content_lock_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            # Now try to update — should be blocked
            with pytest.raises(SubjectEditRequiresDraftError) as exc_info:
                await _update_spec(
                    service,
                    db,
                    content_lock_spec_id,
                    USER_ID,
                    SpecUpdate(description="New description after validation"),
                )
        assert exc_info.value.code == "subject_edit_requires_draft"
        assert "draft" in str(exc_info.value).lower()

    async def test_update_title_blocked_after_success(self, db_factory):
        """Updating the title should also be blocked."""
        content_lock_board_id = str(uuid.uuid4())
        content_lock_spec_id = str(uuid.uuid4())
        await _seed_board(
            db_factory, board_id=content_lock_board_id, spec_id=content_lock_spec_id
        )
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=content_lock_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            with pytest.raises(SubjectEditRequiresDraftError):
                await _update_spec(
                    service,
                    db,
                    content_lock_spec_id,
                    USER_ID,
                    SpecUpdate(title="New title"),
                )

    async def test_update_functional_requirements_blocked(self, db_factory):
        """Updating functional requirements should be blocked."""
        cl_board_id = str(uuid.uuid4())
        cl_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=cl_board_id, spec_id=cl_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=cl_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            with pytest.raises(SubjectEditRequiresDraftError):
                await _update_spec(
                    service,
                    db,
                    cl_spec_id,
                    USER_ID,
                    SpecUpdate(
                        functional_requirements=[{"id": "fr_new", "text": "New FR"}]
                    ),
                )

    async def test_get_spec_allowed_after_success(self, db_factory):
        """Reading the spec should still be allowed after validation."""
        cl_board_id = str(uuid.uuid4())
        cl_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=cl_board_id, spec_id=cl_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=cl_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            spec = await service.get_spec(cl_spec_id)
        assert spec is not None
        assert spec.status == SpecStatus.VALIDATED
        assert spec.validations is not None
        assert len(spec.validations) == 1

    async def test_spec_locked_error_has_correct_message(self, db_factory):
        """SpecLockedError should have a meaningful message."""
        cl_board_id = str(uuid.uuid4())
        cl_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=cl_board_id, spec_id=cl_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=cl_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            try:
                await _update_spec(
                    service,
                    db,
                    cl_spec_id,
                    USER_ID,
                    SpecUpdate(description="attempted edit"),
                )
            except SubjectEditRequiresDraftError as exc:
                assert exc.code == "subject_edit_requires_draft"
                assert "only be edited while in draft" in str(exc).lower()


# ===========================================================================
# 9. Lock release
# ===========================================================================


@pytest.mark.asyncio
class TestLockRelease:
    """Only a new Draft edition clears the current validation lock."""

    async def test_backward_move_clears_lock(self, db_factory):
        """Moving from validated → draft clears current_validation_id."""
        lr_board_id = str(uuid.uuid4())
        lr_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lr_board_id, spec_id=lr_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            # Pass validation
            await _submit_spec_validation(
                service,
                db,
                spec_id=lr_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            spec = await service.get_spec(lr_spec_id)
            assert spec.current_validation_id is not None

            # Move back to draft
            await _move_spec(
                service,
                db,
                lr_spec_id,
                USER_ID,
                SpecMove(status=SpecStatus.DRAFT),
            )
            spec = await service.get_spec(lr_spec_id)
        assert spec.current_validation_id is None
        assert spec.status == SpecStatus.DRAFT
        assert len(spec.validations) == 1  # history preserved

    async def test_lock_released_allows_edit(self, db_factory):
        """After lock is released, spec edits should work."""
        lr_board_id = str(uuid.uuid4())
        lr_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lr_board_id, spec_id=lr_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            # Pass validation
            await _submit_spec_validation(
                service,
                db,
                spec_id=lr_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            # Move back to draft (releases lock)
            await _move_spec(
                service,
                db,
                lr_spec_id,
                USER_ID,
                SpecMove(status=SpecStatus.DRAFT),
            )
            # Draft is the only editable state in the new lifecycle edition.
            spec = await _update_spec(
                service,
                db,
                lr_spec_id,
                USER_ID,
                SpecUpdate(description="Updated after lock release"),
            )
        assert spec.description == "Updated after lock release"

    async def test_validations_history_preserved_after_release(self, db_factory):
        """Validation history is preserved after lock release."""
        lr_board_id = str(uuid.uuid4())
        lr_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lr_board_id, spec_id=lr_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            # Submit multiple validations
            await _submit_spec_validation(
                service,
                db,
                spec_id=lr_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 60,
                    "confidence_justification": "Half the ACs are covered",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 10,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Low confidence but I approve overall",
                    "recommendation": "approve",
                },
            )
            await _submit_spec_validation(
                service,
                db,
                spec_id=lr_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            spec = await service.get_spec(lr_spec_id)
            assert len(spec.validations) == 2

            # Move back to draft
            await _move_spec(
                service,
                db,
                lr_spec_id,
                USER_ID,
                SpecMove(status=SpecStatus.DRAFT),
            )
            spec = await service.get_spec(lr_spec_id)
        assert len(spec.validations) == 2  # history preserved
        assert spec.current_validation_id is None

    async def test_move_to_approved_preserves_current_validation(self, db_factory):
        """A same-edition move does not clear the current validation."""
        lr_board_id = str(uuid.uuid4())
        lr_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lr_board_id, spec_id=lr_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=lr_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            spec = await service.get_spec(lr_spec_id)
            current_id = spec.current_validation_id
            current_edition = spec.edition
            assert current_id is not None
            assert spec_is_content_locked(spec) is True

            # Move back to approved (not draft)
            await _move_spec(
                service,
                db,
                lr_spec_id,
                USER_ID,
                SpecMove(status=SpecStatus.APPROVED),
            )
            spec = await service.get_spec(lr_spec_id)
        assert spec.current_validation_id == current_id
        assert spec.edition == current_edition
        assert spec_is_content_locked(spec) is True
        assert spec.status == SpecStatus.APPROVED

    async def test_forward_execution_lifecycle_preserves_current_validation(
        self,
        db_factory,
        tmp_path,
        monkeypatch,
    ):
        """validated -> in_progress -> done remains in the validated edition."""

        from okto_pulse.community.adapters.rebuild_audit_storage import (
            CommunityFileSystemRebuildAuditArtifactStore,
        )
        from okto_pulse.core.kg.cognitive_closeout_gate import CognitiveCloseoutGate
        from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItemStore

        gate = CognitiveCloseoutGate(
            store=CognitiveConsolidationItemStore(
                artifact_store=CommunityFileSystemRebuildAuditArtifactStore(
                    tmp_path / "cognitive"
                )
            )
        )
        monkeypatch.setattr(
            main_service, "_build_default_cognitive_closeout_gate", lambda: gate
        )

        board_id = str(uuid.uuid4())
        spec_id = str(uuid.uuid4())
        await _seed_board(
            db_factory, board_id=board_id, spec_id=spec_id, execution_ready=True
        )
        async with db_factory() as db:
            service = SpecService(db)
            validation = await _submit_spec_validation(
                service,
                db,
                spec_id=spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            current_id = validation["id"]
            validated = await service.get_spec(spec_id)
            initial_edition = validated.edition
            # Isolate validation-pointer ownership with accepted planning input.
            # Delivery uses real advisory policy; cognitive closeout uses real storage.
            # This case does not qualify execution-plan completeness.
            from unittest.mock import AsyncMock

            accepted_planning = AsyncMock(return_value=None)
            service.require_execution_contract_ready = accepted_planning
            in_progress = await _move_spec(
                service,
                db,
                spec_id,
                USER_ID,
                SpecMove(status=SpecStatus.IN_PROGRESS),
            )
            assert in_progress.current_validation_id == current_id
            assert in_progress.edition == initial_edition
            assert spec_is_content_locked(in_progress) is True
            accepted_planning.assert_awaited_once()  # Planning runs under the board fence.

            done = await _move_spec(
                service,
                db,
                spec_id,
                USER_ID,
                SpecMove(status=SpecStatus.DONE),
            )

        assert done.status == SpecStatus.DONE
        assert done.current_validation_id == current_id
        assert done.edition == initial_edition
        assert spec_is_content_locked(done) is True


# ===========================================================================
# 10. list_validations
# ===========================================================================


@pytest.mark.asyncio
class TestListValidations:
    """list_validations returns all validations with current one marked active."""

    async def test_list_returns_validations_in_reverse_chronological_order(
        self, db_factory
    ):
        lv_board_id = str(uuid.uuid4())
        lv_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lv_board_id, spec_id=lv_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            # Submit 3 validations
            for i in range(3):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=lv_spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": 90,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        "assertiveness": 90,
                        "assertiveness_justification": "FRs are measurable and testable",
                        "ambiguity": 10,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "reject",
                    },
                )
            result = await service.list_spec_validations(lv_spec_id)
        assert len(result["validations"]) == 3
        # First item in result should be the latest (reversed)
        assert result["validations"][0]["id"] == result["current_validation_id"]

    async def test_list_marks_active_validation(self, db_factory):
        """The validation pointed to by current_validation_id should have active=True."""
        lv_board_id = str(uuid.uuid4())
        lv_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lv_board_id, spec_id=lv_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            # Submit 2 validations
            result1 = await _submit_spec_validation(
                service,
                db,
                spec_id=lv_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 60,
                    "confidence_justification": "Half the ACs are covered",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 10,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Low confidence but I approve overall",
                    "recommendation": "approve",
                },
            )
            result2 = await _submit_spec_validation(
                service,
                db,
                spec_id=lv_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            list_result = await service.list_spec_validations(lv_spec_id)
        # Latest should be active
        latest = list_result["validations"][0]
        assert latest["id"] == result2["id"]
        assert latest["active"] is True
        assert latest["is_current"] is True
        # Previous should be inactive
        prev = list_result["validations"][1]
        assert prev["id"] == result1["id"]
        assert prev["active"] is False
        assert prev["is_current"] is False

    async def test_list_after_lock_release(self, db_factory):
        """After lock release, current_validation_id is None and no active flag."""
        lv_board_id = str(uuid.uuid4())
        lv_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lv_board_id, spec_id=lv_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=lv_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
            # Move back to draft
            await _move_spec(
                service,
                db,
                lv_spec_id,
                USER_ID,
                SpecMove(status=SpecStatus.DRAFT),
            )
            result = await service.list_spec_validations(lv_spec_id)
        assert result["current_validation_id"] is None
        for v in result["validations"]:
            assert v["active"] is False
            assert v["is_current"] is False

    async def test_list_returns_all_record_fields(self, db_factory):
        """Each validation record should include all expected fields."""
        lv_board_id = str(uuid.uuid4())
        lv_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lv_board_id, spec_id=lv_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=lv_spec_id,
                reviewer_id=USER_ID,
                reviewer_name="TestReviewer",
                data=_valid_submit_data(),
            )
            result = await service.list_spec_validations(lv_spec_id)
        v = result["validations"][0]
        for field in (
            "id",
            "spec_id",
            "board_id",
            "reviewer_id",
            "reviewer_name",
            "confidence",
            "confidence_justification",
            "assertiveness",
            "assertiveness_justification",
            "ambiguity",
            "ambiguity_justification",
            "clarity_justification",
            "recommendation",
            "outcome",
            "threshold_violations",
            "resolved_thresholds",
            "created_at",
        ):
            assert field in v, f"Missing field: {field}"

    async def test_list_empty_validations(self, db_factory):
        """Spec with no validations returns empty list."""
        lv_board_id = str(uuid.uuid4())
        lv_spec_id = str(uuid.uuid4())
        await _seed_board(db_factory, board_id=lv_board_id, spec_id=lv_spec_id)
        async with db_factory() as db:
            service = SpecService(db)
            result = await service.list_spec_validations(lv_spec_id)
        assert result["validations"] == []
        assert result["current_validation_id"] is None


# ===========================================================================
# 11. Input validation
# ===========================================================================


@pytest.mark.asyncio
class TestInputValidation:
    """Invalid score ranges and missing justification fields."""

    async def test_negative_confidence_raises(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(ValueError, match="confidence"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": -1,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        "assertiveness": 80,
                        "assertiveness_justification": "FRs are measurable and testable",
                        "ambiguity": 10,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "approve",
                    },
                )

    async def test_confidence_over_100_raises(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(ValueError, match="confidence"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": 101,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        "assertiveness": 80,
                        "assertiveness_justification": "FRs are measurable and testable",
                        "ambiguity": 10,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "approve",
                    },
                )

    async def test_negative_assertiveness_raises(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(ValueError, match="assertiveness"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": 90,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        "assertiveness": -5,
                        "assertiveness_justification": "FRs are measurable and testable",
                        "ambiguity": 10,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "approve",
                    },
                )

    async def test_negative_ambiguity_raises(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(ValueError, match="ambiguity"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": 90,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        "assertiveness": 90,
                        "assertiveness_justification": "FRs are measurable and testable",
                        "ambiguity": -10,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "approve",
                    },
                )

    async def test_ambiguity_over_100_raises(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(ValueError, match="ambiguity"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": 90,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        "assertiveness": 90,
                        "assertiveness_justification": "FRs are measurable and testable",
                        "ambiguity": 150,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "approve",
                    },
                )

    async def test_invalid_recommendation_raises(self, db_factory):
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(ValueError, match="recommendation"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": 90,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        "assertiveness": 90,
                        "assertiveness_justification": "FRs are measurable and testable",
                        "ambiguity": 10,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "maybe",
                    },
                )

    async def test_missing_justification_field_raises(self, db_factory):
        """Omitting a required justification field should raise."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises((KeyError, TypeError, ValueError)):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=SPEC_ID,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data={
                        "clarity": 90,
                        "decidability": 90,
                        "decidability_justification": "Explicit implementation choices.",
                        "confidence": 90,
                        "confidence_justification": "All ACs are covered with detailed test plans",
                        # Missing assertiveness_justification
                        "assertiveness": 90,
                        "ambiguity": 10,
                        "ambiguity_justification": "Glossary added and terms defined clearly",
                        "clarity_justification": "Spec is ready for execution with high confidence",
                        "recommendation": "approve",
                    },
                )


# ===========================================================================
# 12. Board-level config with custom thresholds
# ===========================================================================


@pytest.mark.asyncio
class TestBoardLevelConfig:
    """Test with custom threshold values at the board level."""

    async def test_custom_thresholds_applied(self, db_factory):
        """Board with custom thresholds should enforce them."""
        board_id = "custom-threshold-board"
        spec_id = "custom-threshold-spec"
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Custom Threshold Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 95,  # higher default
                        "min_spec_assertiveness": 90,  # higher default
                        "max_spec_ambiguity": 10,  # lower default (stricter)
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="Custom Threshold Spec",
                    status=SpecStatus.APPROVED,
                    archived=False,
                    skip_decisions_coverage=True,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[
                        {
                            "id": "dec_threshold",
                            "title": "Thresholds are board-configured",
                            "status": "active",
                        },
                    ],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            # With default thresholds (80/80/30) this would pass, but with
            # custom thresholds (95/90/10) it should fail on confidence.
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 90,  # below custom min 95
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 95,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 5,
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Spec is ready for execution with high confidence",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "failed"
        assert result["spec_status"] == "approved"
        assert any("confidence" in v for v in result["threshold_violations"])

    async def test_custom_thresholds_pass_with_higher_scores(self, db_factory):
        """With scores meeting custom thresholds, validation should pass."""
        board_id = "custom-pass-board"
        spec_id = "custom-pass-spec"
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Custom Pass Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 95,
                        "min_spec_assertiveness": 90,
                        "max_spec_ambiguity": 10,
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="Custom Pass Spec",
                    status=SpecStatus.APPROVED,
                    archived=False,
                    skip_decisions_coverage=True,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[
                        {
                            "id": "dec_threshold_pass",
                            "title": "Thresholds can pass",
                            "status": "active",
                        },
                    ],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 96,  # above custom min 95
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 95,  # above custom min 90
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 5,  # below custom max 10
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Spec is ready for execution with high confidence",
                    "recommendation": "approve",
                },
            )
        assert result["outcome"] == "success"
        assert result["spec_status"] == "validated"

    async def test_default_validation_gate_and_thresholds_when_not_configured(
        self, db_factory
    ):
        """Board without explicit gate settings should require validation with default thresholds."""
        board_id = "default-threshold-board"
        spec_id = "default-threshold-spec"
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Default Threshold Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={},  # no threshold settings
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="Default Threshold Spec",
                    status=SpecStatus.APPROVED,
                    archived=False,
                    skip_decisions_coverage=True,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[
                        {
                            "id": "dec_default_threshold",
                            "title": "Default thresholds apply",
                            "status": "active",
                        },
                    ],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )

        assert result["outcome"] == "success"
        assert result["spec_status"] == "validated"

    async def test_opt_in_required(self, db_factory):
        """Board without require_spec_validation should reject all submissions."""
        board_id = "opt-out-board"
        spec_id = "opt-out-spec"
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="Opt Out Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": False,  # explicitly disabled
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="Opt Out Spec",
                    status=SpecStatus.APPROVED,
                    archived=False,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            with pytest.raises(ValueError, match="does not require"):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )

    async def test_multiple_threshold_violations_reported(self, db_factory):
        """When multiple thresholds are violated, all should be reported."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 50,  # below 80
                    "confidence_justification": "Half the ACs are covered",
                    "assertiveness": 50,  # below 80
                    "assertiveness_justification": "FRs are vague",
                    "ambiguity": 60,  # above 30
                    "ambiguity_justification": "Many undefined terms",
                    "clarity_justification": "Very poor spec quality",
                    "recommendation": "reject",
                },
            )
        assert result["outcome"] == "failed"
        assert len(result["threshold_violations"]) == 3
        violation_types = {v.split()[0] for v in result["threshold_violations"]}
        assert violation_types == {"confidence", "assertiveness", "ambiguity"}


# ===========================================================================
# 13. Validation record structure
# ===========================================================================


@pytest.mark.asyncio
class TestValidationRecordStructure:
    """Verify the structure and content of validation records."""

    async def test_validation_record_has_all_fields(self, db_factory):
        """Each validation record should contain all expected fields."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="TestReviewer",
                data=_valid_submit_data(),
            )
        expected_fields = {
            "id",
            "spec_id",
            "board_id",
            "reviewer_id",
            "reviewer_name",
            "confidence",
            "confidence_justification",
            "assertiveness",
            "assertiveness_justification",
            "ambiguity",
            "ambiguity_justification",
            "clarity_justification",
            "recommendation",
            "outcome",
            "threshold_violations",
            "resolved_thresholds",
            "created_at",
            "spec_status",
            "active",
        }
        assert set(result.keys()) >= expected_fields

    async def test_validation_record_resolved_thresholds(self, db_factory):
        """resolved_thresholds should contain the thresholds in effect."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
        rt = result["resolved_thresholds"]
        assert rt["min_spec_confidence"] == 70
        assert rt["min_spec_assertiveness"] == 80
        assert rt["max_spec_ambiguity"] == 30

    async def test_validation_record_preserves_reviewer_info(self, db_factory):
        """Validation record should preserve reviewer_id and reviewer_name."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id="reviewer-123",
                reviewer_name="JaneReviewer",
                data=_valid_submit_data(),
            )
            spec = await service.get_spec(SPEC_ID)
        v = spec.validations[0]
        assert v["reviewer_id"] == "reviewer-123"
        assert v["reviewer_name"] == "JaneReviewer"

    async def test_validation_record_has_timestamp(self, db_factory):
        """Validation record should have a created_at timestamp."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
        assert "created_at" in result
        assert result["created_at"] is not None
        # Should be a valid ISO format
        datetime.fromisoformat(result["created_at"])

    async def test_validation_id_format(self, db_factory):
        """Validation ID should follow the 'val_' prefix format."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
        assert result["id"].startswith("val_")
        assert len(result["id"]) == 12  # "val_" + 8 hex chars


# ===========================================================================
# 14. Edge cases
# ===========================================================================


@pytest.mark.asyncio
class TestEdgeCases:
    """Edge cases and boundary conditions."""

    async def test_justification_stripped(self, db_factory):
        """Justification fields should be stripped of leading/trailing whitespace."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 90,
                    "confidence_justification": "  All ACs are covered with detailed test plans  ",
                    "assertiveness": 90,
                    "assertiveness_justification": "  FRs are measurable and testable  ",
                    "ambiguity": 10,
                    "ambiguity_justification": "  Glossary added and terms defined clearly  ",
                    "clarity_justification": "  Spec is ready for execution with high confidence  ",
                    "recommendation": "approve",
                },
            )
            spec = await service.get_spec(SPEC_ID)
        v = spec.validations[0]
        assert (
            v["confidence_justification"]
            == "All ACs are covered with detailed test plans"
        )
        assert v["assertiveness_justification"] == "FRs are measurable and testable"
        assert (
            v["ambiguity_justification"] == "Glossary added and terms defined clearly"
        )
        assert (
            v["clarity_justification"]
            == "Spec is ready for execution with high confidence"
        )

    async def test_multiple_violations_with_reject(self, db_factory):
        """Both threshold violations AND reject recommendation are recorded."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 50,  # below 80
                    "confidence_justification": "Half the ACs are covered",
                    "assertiveness": 50,  # below 80
                    "assertiveness_justification": "FRs are vague",
                    "ambiguity": 60,  # above 30
                    "ambiguity_justification": "Many undefined terms",
                    "clarity_justification": "Very poor spec quality",
                    "recommendation": "reject",
                },
            )
        assert result["outcome"] == "failed"
        assert len(result["threshold_violations"]) == 3
        assert result["recommendation"] == "reject"

    async def test_ambiguity_is_max_not_min(self, db_factory):
        """Ambiguity is checked as a maximum (lower is better), not minimum."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            # Low ambiguity should pass
            result_pass = await _submit_spec_validation(
                service,
                db,
                spec_id=SPEC_ID,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data={
                    "clarity": 90,
                    "decidability": 90,
                    "decidability_justification": "Explicit implementation choices.",
                    "confidence": 90,
                    "confidence_justification": "All ACs are covered with detailed test plans",
                    "assertiveness": 90,
                    "assertiveness_justification": "FRs are measurable and testable",
                    "ambiguity": 0,  # perfect — no ambiguity
                    "ambiguity_justification": "Glossary added and terms defined clearly",
                    "clarity_justification": "Spec is ready for execution with high confidence",
                    "recommendation": "approve",
                },
            )
        assert result_pass["outcome"] == "success"

    async def test_spec_with_no_coverage_passes_threshold_test(self, db_factory):
        """Spec with empty coverage arrays should pass coverage pre-checks."""
        await _seed_board(db_factory)
        async with db_factory() as db:
            # Create a spec with no coverage items (empty arrays)
            spec_id = "no-coverage-spec"
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=BOARD_ID,
                    title="No Coverage Spec",
                    status=SpecStatus.APPROVED,
                    archived=False,
                    skip_decisions_coverage=True,
                    acceptance_criteria=[],
                    functional_requirements=[],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[
                        {
                            "id": "dec_no_coverage",
                            "title": "Coverage arrays are empty by design",
                            "status": "active",
                        },
                    ],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            result = await _submit_spec_validation(
                service,
                db,
                spec_id=spec_id,
                reviewer_id=USER_ID,
                reviewer_name="Tester",
                data=_valid_submit_data(),
            )
        # Empty coverage should pass the pre-checks (nothing to cover)
        assert result["outcome"] == "success"


@pytest.mark.asyncio
class TestAcScenarioPrecheck:
    """submit_spec_validation must reject uncovered ACs BEFORE locking the spec.

    Without this gate, validation could pass (locking the spec) and then
    move_spec→done would fail on the same condition — but by then the spec
    is locked and scenarios cannot be added without unlocking.
    """

    async def _seed_spec_with_uncovered_ac(self, db_factory) -> tuple[str, str]:
        board_id = str(uuid.uuid4())
        spec_id = str(uuid.uuid4())
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="AC Precheck Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 80,
                        "min_spec_assertiveness": 80,
                        "max_spec_ambiguity": 30,
                        "skip_test_coverage_global": False,
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="AC Precheck Spec",
                    status=SpecStatus.APPROVED,
                    archived=False,
                    skip_test_coverage=False,
                    acceptance_criteria=[
                        {"id": "ac_1", "text": "AC1: covered behavior"},
                        {"id": "ac_2", "text": "AC2: UNcovered behavior"},
                    ],
                    functional_requirements=[],
                    test_scenarios=[
                        {
                            "id": "ts_covered",
                            "title": "Covered scenario",
                            "given": "g",
                            "when": "w",
                            "then": "t",
                            "scenario_type": "integration",
                            "linked_criteria": ["ac_1"],
                            "linked_task_ids": [],
                            "status": "passed",
                        },
                    ],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[],
                    created_by=USER_ID,
                )
            )
            await _commit(db)
        return board_id, spec_id

    async def test_uncovered_ac_blocks_validation_before_lock(self, db_factory):
        """AC without a linked scenario must raise BEFORE the spec gets locked."""
        board_id, spec_id = await self._seed_spec_with_uncovered_ac(db_factory)
        async with db_factory() as db:
            service = SpecService(db)
            with pytest.raises(
                ValueError, match="acceptance criteria lack test scenarios"
            ):
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )
            # Spec must still be in 'approved' (not locked) so the caller can
            # add the missing scenario and retry without unlocking.
            spec = await service.get_spec(spec_id)
            assert spec.status == SpecStatus.APPROVED
            assert spec.current_validation_id is None


@pytest.mark.asyncio
class TestFrCoverageMessageFormat:
    """The uncovered-FR error message must not duplicate the 'FRN:' prefix.

    Bug: the formatter prepended 'FR{i}:' where i is the 0-based Python
    index, but the FR text already starts with a 1-based 'FRN:' label
    chosen by the author. The combination produced strings like
    '"FR1: FR2: ..."' which confuse the reader about which FR is meant.
    """

    async def test_message_uses_index_marker_not_duplicated_label(self, db_factory):
        spec_id = str(uuid.uuid4())
        board_id = str(uuid.uuid4())
        async with db_factory() as db:
            db.add(
                Board(
                    id=board_id,
                    name="FR Coverage Board",
                    owner_id=USER_ID,
                    realm_id="local",
                    settings={
                        "require_spec_validation": True,
                        "min_spec_confidence": 80,
                        "min_spec_assertiveness": 80,
                        "max_spec_ambiguity": 30,
                    },
                )
            )
            db.add(
                _new_spec(
                    id=spec_id,
                    board_id=board_id,
                    title="FR Coverage Spec",
                    status=SpecStatus.APPROVED,
                    archived=False,
                    skip_test_coverage=True,
                    acceptance_criteria=[],
                    functional_requirements=[
                        {"id": "fr_1", "text": "FR1: do thing A"},
                        {"id": "fr_2", "text": "FR2: do thing B"},
                    ],
                    test_scenarios=[],
                    business_rules=[],
                    technical_requirements=[],
                    api_contracts=[],
                    decisions=[],
                    created_by=USER_ID,
                )
            )
            await _commit(db)

            service = SpecService(db)
            with pytest.raises(ValueError) as exc_info:
                await _submit_spec_validation(
                    service,
                    db,
                    spec_id=spec_id,
                    reviewer_id=USER_ID,
                    reviewer_name="Tester",
                    data=_valid_submit_data(),
                )
        msg = str(exc_info.value)
        # Bug repro: must NOT contain a duplicated label like 'FR0: FR1:' or 'FR1: FR2:'
        assert "FR0: FR1:" not in msg
        assert "FR1: FR2:" not in msg
        # Fix verification: the message uses bracket index marker '[0]', '[1]'
        # to identify the uncovered FR without colliding with the author's label.
        assert "[0]" in msg and "[1]" in msg
