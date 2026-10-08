"""AC-INT-08 regression: source-owned classification links reach extraction.

The private worker hook is invoked only by this cross-repository test to observe
the production serializer/extractor; no adapter imports this Core implementation.
"""
from types import SimpleNamespace

import pytest
from okto_pulse.community.adapters.sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
from okto_pulse.core.application.processors.consolidation import _run_deterministic_worker
from okto_pulse.core.services.architecture_classification import ArchitectureClassificationService
import test_architecture_classification_use_case as writes

classified_context = writes.classified_context
adopted_context = writes.adopted_context


@pytest.mark.asyncio
async def test_current_architecture_association_is_present_in_worker_relationships(classified_context):
    db = classified_context
    await writes.execute(db, await writes.batch_for(db))
    review = await ArchitectureClassificationService(db).review(board_id="board", spec_id="spec")
    associated = next(item for item in review["items"] if item["root_design_id"] == "associate")
    assert associated["state"] == "current"
    persistence = CommunitySqlAlchemyConsolidationPersistence()
    artifact = await persistence.load_artifact(db, artifact_type="spec", artifact_id="spec")
    inputs = await persistence.load_projection_inputs(
        db, board_id="board", artifact_type="spec", artifact_id="spec", artifact=artifact,
    )
    result = _run_deterministic_worker(SimpleNamespace(artifact_type="spec"), artifact, inputs)
    from okto_pulse.core.application.processors.architecture_association_projection import prepare_architecture_association_projection
    result = await prepare_architecture_association_projection(
        db, board_id="board", spec_id="spec", result=result,
    )
    references = {node.candidate_id: node.source_artifact_ref for node in result.nodes}
    contract = "architecture_design:associate:interface:boundary"
    requirement = "spec:spec:integration_requirement:ir_existing"
    assert contract in references.values() and requirement in references.values()
    # Verify the required fact without prescribing a new physical edge type.
    assert any(
        {references.get(edge.from_candidate_id), references.get(edge.to_candidate_id)}
        == {contract, requirement}
        for edge in result.edges
    ), "Current relational candidate→IR association is absent from production extraction"


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_association_rebuild_and_reclassification_converge(tmp_path):
    """Real Grafx: SQL changes first; ordinary projection replaces only its edges."""
    from sqlalchemy import select
    from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec, ConsolidationQueue
    from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
    from okto_pulse.core.domain.architecture_classification import ArchitectureClassificationBatch
    from okto_pulse.core.ports.structured_spec import get_structured_spec_store
    from test_projection_materialized_parity import materialize, relationship_set

    rule = "implements/architecture_association@v1"

    async def classify(db, target, key):
        population = await writes.read(db)
        candidate = population.candidates[0]
        spec = await get_structured_spec_store().get(db, spec_id="spec")
        decision = {
            "candidate_ref": candidate.id, "expected_source_digest": candidate.source_digest,
            **({"disposition": "associate_existing_ir", "integration_requirement_refs": [target]}
               if target else {"disposition": "context_only", "reason": "Informative contract"}),
        }
        await writes.execute(db, ArchitectureClassificationBatch.model_validate({
            "expected_spec_version": spec.version, "expected_spec_edition": spec.edition,
            "idempotency_key": key, "decisions": [decision],
        }))

    async def seed(factory):
        async with factory() as db:
            (await db.get(Board, "board")).owner_id = "author"
            spec = await db.get(Spec, "spec")
            spec.status = "draft"
            spec.decisions = []
            spec.integration_requirements = [
                {"id": identity, "title": identity, "integration_type": "event", "status": "active"}
                for identity in ("ir_one", "ir_two")
            ]
            db.add(writes.design("associate"))
            await db.commit()
            await classify(db, "ir_one", "initial-association")
            # Rebuild owns enumeration in this initial snapshot.
            from sqlalchemy import delete
            await db.execute(delete(ConsolidationQueue))
            await db.commit()

    def targets(graph):
        edges = {edge: count for edge, count in relationship_set(graph).items() if edge[3] == rule}
        assert all(count == 1 for count in edges.values())
        assert all(edge[1] == "architecture_design:associate:interface:boundary" for edge in edges)
        return {edge[2] for edge in edges}

    async def exercise(factory, graph):
        assert targets(graph) == {"spec:spec:integration_requirement:ir_one"}
        for target, key in (("ir_two", "reassociate"), (None, "context-only")):
            async with factory() as db:
                await classify(db, target, key)
                candidate = (await writes.read(db)).candidates[0]
                review = await ArchitectureClassificationService(db).review(
                    board_id="board", spec_id="spec", candidate_id=candidate.id,
                    source_digest=candidate.source_digest,
                )
                assert review["items"][0]["state"] == "current"
                assert review["items"][0]["decisions"][0]["integration_requirement_refs"] == (
                    [target] if target else [])
                # This offline harness does not run the domain-event dispatcher.
                # Enqueue its source explicitly, as in the other parity witnesses.
                db.add(ConsolidationQueue(id=key, board_id="board", artifact_type="spec",
                                         artifact_id="spec", source="state_transition"))
                await db.commit()
            # Projection remains behind the authoritative committed classification.
            assert targets(graph)
            processor = ConsolidationProcessor(relational_scope_factory=factory)
            assert await processor.process_batch() == 1
            async with factory() as db:
                pending = list(await db.scalars(select(ConsolidationQueue.id)))
                assert not pending, pending
            assert targets(graph) == ({f"spec:spec:integration_requirement:{target}"} if target else set())

    await materialize(tmp_path / "association", incremental=False, seed=seed,
                      exercise=exercise, native_schema=True)
