"""Persisted witnesses for gaps found in the consolidated acceptance audit."""
import copy
import socket

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_models import ArchitectureDesign, Spec
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_structured_spec import CommunitySqlAlchemyStructuredSpecStore
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.application.use_cases.architecture_classification_review import (
    GetArchitectureClassificationsCommand, GetArchitectureClassificationsUseCase,
)
from okto_pulse.core.domain.architecture_classification import ArchitectureClassificationBatch
from okto_pulse.core.services.architecture_candidates import load_spec_architecture_candidates

import test_architecture_classification_use_case as writes
import test_architecture_classification_transports as transports
from test_architecture_candidates_integration import design

classified_context = writes.classified_context
adopted_context = writes.adopted_context


async def review(db, spec_id="spec", **options):
    factory = async_sessionmaker(db.bind, expire_on_commit=False, sync_session_class=CommunitySemanticSession)
    who = writes.actor()
    async with factory() as session, CommunityUnitOfWork(session, actor=who) as uow:
        return await GetArchitectureClassificationsUseCase().execute(
            GetArchitectureClassificationsCommand("board", spec_id, **options), actor=who, uow=uow,
        )


async def batch(db, spec_id, candidate, intent, key):
    record = await CommunitySqlAlchemyStructuredSpecStore().get(db, spec_id=spec_id)
    return ArchitectureClassificationBatch.model_validate({
        "expected_spec_version": record.version, "expected_spec_edition": record.edition,
        "idempotency_key": key, "decisions": [{"candidate_ref": candidate.id,
            "expected_source_digest": candidate.source_digest, **intent}],
    })


@pytest.mark.asyncio
async def test_same_origin_has_independent_persisted_decisions_in_two_specs(classified_context):
    db = classified_context
    db.add(Spec(id="second", board_id="board", title="Second", created_by="author", edition=2))
    await db.flush()
    db.add_all([design("copy-one", root="shared"), design("copy-two", owner_id="second", root="shared")])
    await db.commit()
    first = next(item for item in (await load_spec_architecture_candidates(
        db, board_id="board", spec_id="spec")).candidates if item.root_design_id == "shared")
    second = (await load_spec_architecture_candidates(db, board_id="board", spec_id="second")).candidates[0]
    assert first.id != second.id and first.source_digest == second.source_digest
    context = await batch(db, "spec", first, {"disposition": "context_only", "reason": "Other team's boundary"}, "local-one")
    await writes.execute(db, context)
    promoted = await writes.execute(db, await batch(db, "second", second, {
        "disposition": "promote_to_ir", "integration_requirements": [{"title": "Publish", "integration_type": "event"}],
    }, "local-two"), spec_id="second")
    one = (await review(db, candidate_id=first.id, source_digest=first.source_digest))["items"][0]
    two = (await review(db, "second", candidate_id=second.id, source_digest=second.source_digest))["items"][0]
    assert one["state"] == two["state"] == "current"
    assert one["decisions"][0]["disposition"] == "context_only"
    assert one["decisions"][0]["integration_requirement_refs"] == []
    assert two["decisions"][0]["disposition"] == "promote_to_ir"
    assert two["decisions"][0]["integration_requirement_refs"] == promoted["created_ir_ids"]
    assert not set(promoted["created_ir_ids"]) & {item["id"] for item in
        (await CommunitySqlAlchemyStructuredSpecStore().get(db, spec_id="spec")).integration_requirements}


@pytest.mark.asyncio
async def test_accepted_suggestion_preserves_complete_contract_and_workflow(classified_context):
    db = classified_context
    contract = {"id": "boundary", "name": "Publish orders", "contract_type": "event",
        "event_schema": {"properties": {"order_id": {"type": "string"}}},
        "error_contract": "Reject malformed messages; never change approval or skip gates.",
        "schema_ref": "urn:orders-contract:revision-7"}
    await db.execute(update(ArchitectureDesign).where(ArchitectureDesign.id == "promote").values(interfaces=[contract]))
    await db.commit()
    candidate = next(item for item in (await load_spec_architecture_candidates(
        db, board_id="board", spec_id="spec")).candidates if item.root_design_id == "promote")
    before = copy.deepcopy((await db.get(Spec, "spec")).status)
    detail = (await review(db, candidate_id=candidate.id, source_digest=candidate.source_digest))["items"][0]
    suggestion = detail["promotion_suggestion"]
    assert suggestion["requires_author_review"] and suggestion["missing_required_fields"] == []
    saved = await writes.execute(db, await batch(db, "spec", candidate, {
        "disposition": "promote_to_ir", "integration_requirements": [suggestion["proposed_ir"]],
    }, "accept-source-backed-suggestion"))
    db.expire_all()
    persisted = await db.get(Spec, "spec")
    assert persisted.status == before
    ir = next(item for item in persisted.integration_requirements if item["id"] == saved["created_ir_ids"][0])
    assert ir["integration_type"] == "event" and not ir.get("method")
    assert ir["contract_ref"] == contract["schema_ref"]
    assert ir["data_contract"]["event_schema"] == contract["event_schema"]
    assert ir["data_contract"]["error_contract"] == contract["error_contract"]
    recalled = (await review(db, candidate_id=candidate.id, source_digest=candidate.source_digest))["items"][0]
    assert recalled["analyzed_contract"] == candidate.contract
    assert recalled["decisions"][0]["actor_id"] == "author"
    assert recalled["decisions"][0]["integration_requirement_refs"] == saved["created_ir_ids"]


@pytest.mark.asyncio
async def test_new_candidate_after_batch_is_pending_and_homonymous_origins_stay_distinct(classified_context):
    db = classified_context
    await writes.execute(db, await writes.batch_for(db))
    original = await review(db)
    db.add(design("later-origin", interfaces=[{"id": "boundary", "name": "Boundary", "event_schema": {"const": "adopted"}}]))
    await db.commit()
    result = await review(db, limit=1)
    assert original["classification_complete"] and not result["classification_complete"]
    assert result["state_counts"]["current"] == 3 and result["state_counts"]["pending"] == 1
    population = await load_spec_architecture_candidates(db, board_id="board", spec_id="spec")
    assert len({item.id for item in population.candidates}) == 4
    assert len({item.interface_id for item in population.candidates}) == 1
    assert len({item.root_design_id for item in population.candidates}) == 4
    assert len(result["items"]) == 1 and result["has_more"]
    # The old key carries only its old intent, even after the population expands.
    records = await db.scalars(select(Spec).where(Spec.id == "spec"))
    assert len(records.one().integration_requirements) == 3


@pytest.mark.asyncio
async def test_partial_publication_keeps_consumption_in_explicit_context(classified_context):
    db = classified_context
    contract = {"id": "boundary", "name": "Orders", "contract_type": "event",
        "event_schema": {"publish": {"event": "OrderPlaced"}, "consume": {"event": "OrderAccepted"}},
        "error_contract": "Preserve the existing dead-letter policy", "schema_ref": "urn:orders@7"}
    await db.execute(update(ArchitectureDesign).where(ArchitectureDesign.id == "promote").values(interfaces=[contract]))
    await db.commit()
    candidate = next(item for item in (await load_spec_architecture_candidates(
        db, board_id="board", spec_id="spec")).candidates if item.root_design_id == "promote")
    saved = await writes.execute(db, await batch(db, "spec", candidate, {
        "disposition": "promote_to_ir", "scope_paths": ["/event_schema/publish"],
        "remainder_reason": "Consumption and error policy are unchanged context",
        "integration_requirements": [{"title": "Publish OrderPlaced", "integration_type": "event",
            "data_contract": {"event_schema": contract["event_schema"]["publish"]}}],
    }, "publication-only"))
    detail = (await review(db, candidate_id=candidate.id, source_digest=candidate.source_digest))["items"][0]
    assert detail["analyzed_contract"] == candidate.contract
    assert detail["decisions"][0]["scope_paths"] == ["/event_schema/publish"]
    assert detail["remainder_state"] == "current"
    assert detail["decisions"][0]["remainder_reason"] == "Consumption and error policy are unchanged context"
    db.expire_all()
    ir = next(item for item in (await db.get(Spec, "spec")).integration_requirements
        if item["id"] == saved["created_ir_ids"][0])
    assert ir["data_contract"] == {"event_schema": {"event": "OrderPlaced"}}
    assert "OrderAccepted" not in str(ir)


@pytest.mark.asyncio
async def test_reference_only_http_read_has_no_remote_io_or_domain_writes(classified_context, monkeypatch):
    db = classified_context
    await db.execute(update(ArchitectureDesign).where(ArchitectureDesign.id == "context").values(
        interfaces=[{"id": "boundary", "schema_ref": "http://127.0.0.1:1/private-schema"}]))
    await db.commit()
    before = await writes.snapshot(db)
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(args)
        raise AssertionError("Schema references are data, never remote reads")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    app, _ = transports.application(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/api/v1/boards/board/specs/spec/architecture-candidates"
        for _ in range(2):
            response = await client.get(path)
            assert response.status_code == 200, response.text
            item = next(row for row in response.json()["candidates"] if row["root_design_id"] == "context")
            assert item["signals"] == ["reference_only"]
            detail = await client.get(path, params={"candidate_id": item["id"], "source_digest": item["source_digest"]})
            assert detail.status_code == 200, detail.text
            assert detail.json()["candidates"][0]["contract"]["schema_ref"] == "http://127.0.0.1:1/private-schema"
    assert attempted == []
    assert await writes.snapshot(db) == before
