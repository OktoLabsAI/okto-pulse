"""AC-ARQ-15: real transition admission on disposable relational state."""

import copy
import httpx
import pytest
from types import SimpleNamespace
from sqlalchemy import func, select, update

from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, DomainEventRow, Spec, SpecHistory
from okto_pulse.community.adapters.relational_effects import register_community_relational_effects
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.code_traceability import DeliveryContext, DirectSpecDeliveryContextProvenance
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.domain.code_traceability import build_direct_spec_source_context_manifest

import test_architecture_classification_transports as transport
import test_architecture_classification_use_case as classification
from test_architecture_candidates_integration import design, read

adopted_context = classification.adopted_context
classified_context = classification.classified_context


async def complete_start_fixture(db, tmp_path, candidate_count=3):
    register_community_relational_effects(settings=SimpleNamespace(data_dir=str(tmp_path), port=1, environment="test"))
    provenance = DirectSpecDeliveryContextProvenance(
        value=DeliveryContext.GREENFIELD, source_spec_id="spec", source_spec_version=1)
    manifest, digest = build_direct_spec_source_context_manifest(spec_id="spec",
        delivery_context=DeliveryContext.GREENFIELD, provenance=provenance)
    fields = {field: [] for _, field in COLLECTIONS}
    fields.update(
        functional_requirements=[{"id": "fr", "text": "Show the operating procedure", "linked_task_ids": ["task"],
            "verification": {"mode": "explicit", "required_profiles": ["functional"]},
            "implementation_plan": {"contributions": [{"card_id": "task", "scope": "whole_requirement"}]}}],
        acceptance_criteria=[{"id": "ac-procedure", "text": "The procedure view displays all required steps",
            "linked_task_ids": ["task"], "verification_profile": "functional",
            "requirement_links": [{"requirement_type": "functional_requirement", "requirement_id": "fr"},
                {"requirement_type": "business_rule", "requirement_id": "br"}]}],
        business_rules=[{"id": "br", "title": "Complete procedure", "rule": "Every required step must be displayed",
            "when": "The procedure view opens", "then": "All required steps are visible",
            "linked_requirements": ["fr"], "linked_task_ids": ["task"],
            "verification": {"mode": "explicit", "required_profiles": ["functional"]},
            "implementation_plan": {"contributions": [{"card_id": "task", "scope": "whole_requirement"}]}}],
        decisions=[{"id": "decision", "title": "Document operating procedure",
            "rationale": "This Spec delivers the procedure", "status": "active", "linked_task_ids": ["task"]}],
        refinement_id=None, status="draft", test_scenarios=[{"id": "scenario", "title": "Procedure display", "scenario_type": "integration", "linked_task_ids": ["test"],
            "status": "ready", "given": "A stored procedure", "when": "The procedure view opens",
            "then": "All steps are displayed", "verification_method": "automated_test", "linked_criteria": ["ac-procedure"]}],
        evaluations=[{"spec_edition": 2, "evaluator_id": "reviewer", "recommendation": "approve", "overall_score": 95}],
        delivery_context="greenfield", delivery_context_provenance={"value": "greenfield",
            "source_spec_id": "spec", "source_spec_version": 1},
        source_context_manifest=manifest, source_context_sha256=digest,
        architecture_adoption=ArchitectureAdoptionScope(board_id="board", spec_id="spec",
            adopted_in_edition=2, actor_id="author", inherited_resource_ids=()).model_dump(mode="json"),
        execution_contract=new_execution_contract(board_id="board", spec_id="spec", edition=2,
            actor_id="author", origin="new_spec"))
    await db.execute(update(Spec).where(Spec.id == "spec").values(**fields))
    db.add(Card(id="task", board_id="board", spec_id="spec", title="Document procedure",
        card_type="normal", status="not_started", created_by="author"))
    db.add(Card(id="test", board_id="board", spec_id="spec", title="Verify procedure",
        card_type="test", status="not_started", created_by="author", test_scenario_ids=["scenario"]))
    db.add_all(design(f"extra-{index}") for index in range(candidate_count - 3))
    await db.commit()
    app, factory = transport.application(db)
    register_knowledge_propagation_port(CommunitySqlAlchemyKnowledgePropagationStore(factory))
    from okto_pulse.core.domain.execution_plan import resolve_spec_execution_plan
    from okto_pulse.core.ports.test_evidence import supported_test_verification_methods
    planned = await db.get(Spec, "spec", populate_existing=True)
    plan = resolve_spec_execution_plan(spec=planned, cards=[{
        "id": "task", "board_id": "board", "spec_id": "spec", "card_type": "normal",
        "status": "not_started", "archived": False, "test_scenario_ids": [],
    }, {"id": "test", "board_id": "board", "spec_id": "spec", "card_type": "test",
        "status": "not_started", "archived": False, "test_scenario_ids": ["scenario"]}],
        admitted_methods=supported_test_verification_methods())
    assert plan.complete, (plan.qualification, plan.inventory)
    return app, planned, fields


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [3, 31])
async def test_pending_architecture_blocks_actual_start_without_workflow_mutation(classified_context, tmp_path, candidate_count):
    db = classified_context
    app, planned, fields = await complete_start_fixture(db, tmp_path, candidate_count)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        population = await read(db)
        assert len(population.candidates) == candidate_count
        pending_id = population.candidates[-1].id
        initial_batch = {"expected_spec_version": planned.version, "expected_spec_edition": planned.edition,
            "idempotency_key": "review-initial-page", "decisions": [{
                "candidate_ref": candidate.id, "expected_source_digest": candidate.source_digest,
                "disposition": "context_only", "reason": "External contract outside this procedure view",
            } for candidate in population.candidates[:-1]]}
        initial = await client.post("/api/v1/boards/board/specs/spec/architecture-classifications", json=initial_batch)
        assert initial.status_code == 200, initial.text
        # The accepted assessment is fixture input, not approval inferred from classifications.
        await db.execute(update(Spec).where(Spec.id == "spec").values(status="validated"))
        await db.commit()
        summary = await client.get("/api/v1/boards/board/specs/spec/architecture-classifications?limit=1")
        assert summary.status_code == 200, summary.text
        assert summary.json()["items"][0]["state"] == "current"
        assert summary.json()["state_counts"]["pending"] == 1
        result = await client.post("/api/v1/specs/spec/move", json={"status": "in_progress"})
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "spec_architecture_classification_incomplete", result.text
    assert result.json()["detail"]["details"]["blocking_candidate_count"] == 1
    assert result.json()["detail"]["details"]["blocking_candidate_ids"] == [pending_id]
    remediation = result.json()["detail"]["details"]
    assert remediation["required_tool"] == "okto_pulse_list_architecture_classifications"
    assert remediation["follow_up_tool"] == "okto_pulse_classify_architecture_candidates"
    assert "authorized Draft revision" in remediation["operator_action"]
    row = await db.get(Spec, "spec", populate_existing=True)
    assert row.status == "validated"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        reopened = await client.post("/api/v1/specs/spec/move", json={"status": "draft"})
        assert reopened.status_code == 200, reopened.text
        await db.refresh(row)
        population = await read(db)
        batch = {"expected_spec_version": row.version, "expected_spec_edition": row.edition,
            "idempotency_key": "review-all", "decisions": [{
                "candidate_ref": candidate.id, "expected_source_digest": candidate.source_digest,
                "disposition": "context_only", "reason": "External contract outside this procedure view",
            } for candidate in population.candidates]}
        classified = await client.post("/api/v1/boards/board/specs/spec/architecture-classifications", json=batch)
        assert classified.status_code == 200, classified.text
        await db.refresh(row)
        assert row.status == "draft" and row.functional_requirements == fields["functional_requirements"]
        # Assessment submission has separate integration tests. Seed its result
        # to test transition re-evaluation without replacing any admission gate.
        await db.execute(update(Spec).where(Spec.id == "spec").values(status="validated",
            evaluations=[{"spec_edition": row.edition, "evaluator_id": "reviewer", "recommendation": "reject", "overall_score": 95}]))
        await db.commit()
        rejected = await client.post("/api/v1/specs/spec/move", json={"status": "in_progress"})
        assert rejected.status_code == 400 and "reject" in rejected.text, rejected.text
        await db.refresh(row)
        assert row.status == "validated"
        await db.execute(update(Spec).where(Spec.id == "spec").values(evaluations=[{**entry, "spec_edition": row.edition} for entry in fields["evaluations"]]))
        await db.commit()
        started = await client.post("/api/v1/specs/spec/move", json={"status": "in_progress"})
        assert started.status_code == 200, started.text
        await db.refresh(row)
        assert row.status == "in_progress"
        assert row.integration_requirements == []
        assert row.test_scenarios[0]["status"] == "ready"
        assert "evidence" not in row.test_scenarios[0]
        assert (await db.get(Card, "task", populate_existing=True)).status == "not_started"
        assert (await db.get(Card, "test", populate_existing=True)).status == "not_started"


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", ["contribution", "test_owner"])
async def test_approved_spec_cannot_start_with_incomplete_decomposition(classified_context, tmp_path, gap):
    """BASE T06: content approval cannot substitute for a complete execution plan."""
    db = classified_context
    app, planned, fields = await complete_start_fixture(db, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        population = await read(db)
        classified = await client.post("/api/v1/boards/board/specs/spec/architecture-classifications", json={
            "expected_spec_version": planned.version, "expected_spec_edition": planned.edition,
            "idempotency_key": "classify-for-planning", "decisions": [{
                "candidate_ref": candidate.id, "expected_source_digest": candidate.source_digest,
                "disposition": "context_only", "reason": "External contract outside this procedure view",
            } for candidate in population.candidates],
        })
        assert classified.status_code == 200, classified.text
        # The accepted evaluation is seeded; no planning gate is substituted.
        await db.execute(update(Spec).where(Spec.id == "spec").values(status="validated"))
        if gap == "contribution":
            incomplete = copy.deepcopy(fields["functional_requirements"])
            incomplete[0]["implementation_plan"] = {"contributions": []}
            await db.execute(update(Spec).where(Spec.id == "spec").values(functional_requirements=incomplete))
        else:
            await db.execute(update(Card).where(Card.id == "test").values(test_scenario_ids=[]))
        await db.commit()
        row = await db.get(Spec, "spec", populate_existing=True)
        preserved = copy.deepcopy((row.evaluations, row.validations, row.edition, row.current_validation_id))
        blocked = await client.post("/api/v1/specs/spec/move", json={"status": "in_progress"})
        assert blocked.status_code == 400, blocked.text
        assert "spec_execution_plan_incomplete" in blocked.text, blocked.text
        await db.refresh(row)
        assert row.status == "validated"
        assert (row.evaluations, row.validations, row.edition, row.current_validation_id) == preserved
        assert (await db.get(Card, "task", populate_existing=True)).status == "not_started"
        # Repair only the missing plan fact; the very same approval now permits start.
        if gap == "contribution":
            await db.execute(update(Spec).where(Spec.id == "spec").values(functional_requirements=fields["functional_requirements"]))
        else:
            await db.execute(update(Card).where(Card.id == "test").values(test_scenario_ids=["scenario"]))
        await db.commit()
        started = await client.post("/api/v1/specs/spec/move", json={"status": "in_progress"})
        assert started.status_code == 200, started.text
        await db.refresh(row)
        assert row.status == "in_progress"
        assert (row.evaluations, row.validations, row.edition, row.current_validation_id) == preserved


@pytest.mark.asyncio
async def test_removing_only_test_card_invalidates_previously_complete_approved_plan(adopted_context, tmp_path):
    """ADV-14: an approved plan is re-evaluated after an authorized Card deletion."""
    from okto_pulse.community.api.cards import router as cards_router
    from okto_pulse.core.domain.realm import RealmScope
    from okto_pulse.core.services.main import SpecService

    db = adopted_context
    db.info['realm_scope'] = RealmScope.local()
    app, _, _ = await complete_start_fixture(db, tmp_path)
    app.include_router(cards_router, prefix='/api/v1/cards')
    await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated'))
    await db.commit()
    spec = await db.get(Spec, 'spec', populate_existing=True)
    await SpecService(db).require_execution_contract_ready(spec)
    preserved = copy.deepcopy((spec.evaluations, spec.validations, spec.edition,
                               spec.current_validation_id, spec.functional_requirements, spec.acceptance_criteria))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        removed = await client.delete('/api/v1/cards/test')
        assert removed.status_code == 204, removed.text
        db.expire_all()
        assert await db.get(Card, 'test') is None
        spec = await db.get(Spec, 'spec')
        assert spec.status == 'validated'
        assert (spec.evaluations, spec.validations, spec.edition, spec.current_validation_id,
                spec.functional_requirements, spec.acceptance_criteria) == preserved
        assert spec.test_scenarios[0]['linked_task_ids'] == []
        assert spec.test_scenarios[0]['status'] == 'ready'
        assert 'evidence' not in spec.test_scenarios[0]
        with pytest.raises(ValueError, match='spec_execution_plan_incomplete'):
            await SpecService(db).require_execution_contract_ready(spec)
        before = await classification.snapshot(db)
        blocked = await client.post('/api/v1/specs/spec/move', json={'status': 'in_progress'})
        assert blocked.status_code == 400 and 'have no linked test cards' in blocked.text, blocked.text
        assert await classification.snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("verification", ["absent", "unexecuted"])
async def test_task_approvals_do_not_replace_spec_integrated_verification(adopted_context, tmp_path, verification):
    """BASE T07: completed/reviewed tasks cannot mint Spec-level test credit."""
    db = adopted_context
    app, _, fields = await complete_start_fixture(db, tmp_path)
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from okto_pulse.community.adapters.composition import configure_community_kg_registry
    from okto_pulse.community.config import CommunitySettings
    configure_community_kg_registry(async_sessionmaker(db.bind), settings=CommunitySettings(
        data_dir=str(tmp_path / "runtime"), kg_base_dir=str(tmp_path / "kg"),
        kg_embedding_mode="stub", kg_embedding_dim=8,
    ))
    # Approved task results are fixture inputs. T04 exercises actual submission.
    approved = [{"reviewer_id": "independent", "recommendation": "approve", "confidence": 95}]
    await db.execute(update(Card).where(Card.id == "task").values(status="done", validations=approved))
    await db.execute(update(Card).where(Card.id == "test").values(status="done"))
    await db.execute(update(Spec).where(Spec.id == "spec").values(
        status="in_progress", test_scenarios=[] if verification == "absent" else fields["test_scenarios"]))
    board = await db.get(Board, "board")
    board.settings = {**(board.settings or {}), "skip_cognitive_consolidation": True,
                      "delivery_evidence_gate": "blocking"}
    await db.commit()
    before_events = await db.scalar(select(func.count()).select_from(DomainEventRow))
    before_history = await db.scalar(select(func.count()).select_from(SpecHistory))
    row = await db.get(Spec, "spec", populate_existing=True)
    preserved = copy.deepcopy((row.evaluations, row.edition, row.version, row.test_scenarios))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        blocked = await client.post("/api/v1/specs/spec/move", json={"status": "done"})
    assert blocked.status_code == 400, blocked.text
    expected = "lack test scenarios" if verification == "absent" else "delivery_evidence_incomplete"
    assert expected in blocked.text, blocked.text
    await db.refresh(row)
    assert row.status == "in_progress"
    assert (row.evaluations, row.edition, row.version, row.test_scenarios) == preserved
    assert (await db.get(Card, "task", populate_existing=True)).validations == approved
    assert await db.scalar(select(func.count()).select_from(DomainEventRow)) == before_events
    assert await db.scalar(select(func.count()).select_from(SpecHistory)) == before_history
