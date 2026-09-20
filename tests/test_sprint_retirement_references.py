"""Historical census tests; no archival/cutover or semantic approval is implied."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.sprint_retirement_inventory import read_sprint_retirement_inventory
from okto_pulse.community.adapters.sprint_retirement_references import SprintReferenceInspectionError
from okto_pulse.community.adapters.sqlalchemy_models import Base
import test_sprint_retirement_inventory as relational

database = relational.database


async def receipt(engine, identity="receipt", *, board_id="board-a", subject_id="sprint", entity_type="sprint"):
    values = dict(receipt_id=identity, evaluation_id=identity, board_id=board_id, entity_type=entity_type,
        subject_id=subject_id, subject_version=1, catalog_version="1", ruleset_version="1", outcome="not_applicable",
        state="not_applicable", recorded_currentness="current", evaluator_version="1", evaluated_by="owner",
        evaluated_at=datetime.now(timezone.utc), rule_count=0, failed_rule_count=0, error_rule_count=0,
        finding_count=0, blocking_finding_count=0, waived_finding_count=0, idempotency_key=identity, sealed=True)
    values.update({key: "a" * 64 for key in ("subject_content_digest", "input_digest", "policy_set_digest",
        "binding_head_digest", "receipt_digest", "request_digest")})
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["policy_compliance_receipts"]).values(values))


@pytest.mark.asyncio
async def test_mixed_discriminator_names_and_receipt_children_keep_exact_keys_and_ownership(database):
    engine, path = database
    await receipt(engine)
    await receipt(engine, "unrelated", entity_type="spec", subject_id="spec-a")
    # This census fixture tests the child-to-receipt link, not validation of the
    # guideline/binding authorities (which remain separate live records).
    async with engine.begin() as connection:
        for parent in ("receipt", "unrelated"):
            await connection.execute(insert(Base.metadata.tables["policy_compliance_adopted_revisions"]).values(
                receipt_id=parent, guideline_id="guideline", binding_id="binding", binding_revision=1,
                revision_id="revision", semantic_version="1", revision_digest="b" * 64))
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    inventory = (await read_sprint_retirement_inventory(engine)).historical_references
    inventory.require_resolved_scopes()
    assert len(inventory.references) == 2
    assert {(r.table, r.key) for r in inventory.references} == {
        ("policy_compliance_receipts", (("receipt_id", "receipt"),)),
        ("policy_compliance_adopted_revisions", (("receipt_id", "receipt"), ("guideline_id", "guideline"))),
    }
    assert {(r.owner_board_id, r.reference_board_id, r.sprint_id) for r in inventory.references} == {("board-a", "board-a", "sprint")}
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


@pytest.mark.asyncio
async def test_historical_missing_source_is_opaque_provenance_not_an_invented_sprint(database):
    engine, _ = database
    await receipt(engine, subject_id="historically-removed")
    inventory = (await read_sprint_retirement_inventory(engine)).historical_references
    inventory.require_resolved_scopes()
    assert inventory.references[0].scope_state == "historical_source_absent"
    assert inventory.references[0].sprint_id == "historically-removed"
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM sprints"))).scalar_one() == 1


@pytest.mark.asyncio
async def test_foreign_source_does_not_reassign_the_record_to_source_board(database):
    engine, _ = database
    await receipt(engine, board_id="board-b")
    inventory = (await read_sprint_retirement_inventory(engine)).historical_references
    with pytest.raises(SprintReferenceInspectionError) as captured:
        inventory.require_resolved_scopes()
    reference = captured.value.references[0]
    assert reference.owner_board_id == "board-b"
    assert reference.reference_board_id == "board-b"
    assert reference.scope_state == "cross_board_reference"


@pytest.mark.asyncio
@pytest.mark.parametrize(("spec_id", "state"), [("spec-a", "current_source"), ("missing", "reference_scope_missing")])
async def test_knowledge_base_owner_comes_from_its_parent_not_the_source(database, spec_id, state):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["spec_knowledge_bases"]).values(id="kb", spec_id=spec_id,
            title="History", content="Unmodified knowledge", source_type="sprint", source_id="sprint", created_by="owner"))
    inventory = (await read_sprint_retirement_inventory(engine)).historical_references
    reference = inventory.references[0]
    assert reference.scope_state == state
    assert reference.owner_board_id == ("board-a" if spec_id == "spec-a" else None)


@pytest.mark.asyncio
async def test_unclassified_polymorphic_schema_is_not_silently_skipped(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE custom_subjects (id TEXT PRIMARY KEY, subject_type TEXT, subject_id TEXT)"))
    with pytest.raises(SprintReferenceInspectionError, match="unclassified_subject_column:custom_subjects.subject_type"):
        await read_sprint_retirement_inventory(engine)


@pytest.mark.asyncio
async def test_reference_budget_is_shared_and_does_not_return_a_partial_census(database):
    engine, _ = database
    await receipt(engine)
    with pytest.raises(SprintReferenceInspectionError, match="reference_row_limit"):
        await read_sprint_retirement_inventory(engine, max_rows=1)
    assert len((await read_sprint_retirement_inventory(engine)).historical_references.references) == 1


@pytest.mark.asyncio
async def test_orphan_history_child_blocks_environment_with_its_key(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["policy_compliance_adopted_revisions"]).values(
            receipt_id="missing-receipt", guideline_id="guideline", binding_id="binding", binding_revision=1,
            revision_id="revision", semantic_version="1", revision_digest="b" * 64))
    with pytest.raises(SprintReferenceInspectionError, match="history_parent_invalid") as captured:
        await read_sprint_retirement_inventory(engine)
    reference = captured.value.references[0]
    assert reference.key == (("receipt_id", "missing-receipt"), ("guideline_id", "guideline"))
    assert reference.sprint_id is None  # Cannot invent a source for the orphan.


@pytest.mark.asyncio
@pytest.mark.parametrize("event_board", ["board-a", "board-b"])
async def test_waiver_event_composite_owner_link_is_verified_before_join(database, event_board):
    engine, _ = database
    await receipt(engine)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=1)
    async with engine.begin() as connection:
        # Like the revision census fixture, this isolates history linkage; it
        # does not mint or approve a valid guideline waiver through the service.
        await connection.execute(insert(Base.metadata.tables["policy_waivers"]).values(
            waiver_id="waiver", board_id="board-a", finding_id="finding", receipt_id="receipt", guideline_id="guideline",
            revision_id="revision", rule_id="rule", entity_type="sprint", subject_id="sprint", subject_version=1,
            scope_digest="a" * 64, justification="Historical request", requested_by="owner", requested_at=now,
            original_expires_at=expires, status="requested", waiver_revision=1, expires_at=expires,
            last_event_id="waiver-event", last_event_type="request", last_event_at=now,
            head_digest="b" * 64, idempotency_key="waiver", request_digest="c" * 64))
        await connection.execute(insert(Base.metadata.tables["policy_waiver_events"]).values(
            event_id="waiver-event", waiver_id="waiver", board_id=event_board, waiver_revision=1,
            event_type="request", to_status="requested", actor_id="owner", occurred_at=now, reason="Historical request",
            expires_at=expires, scope_digest="a" * 64, waiver_digest="b" * 64, idempotency_key="event", request_digest="c" * 64))
    if event_board == "board-b":
        with pytest.raises(SprintReferenceInspectionError, match="history_parent_invalid:policy_waiver_events") as captured:
            await read_sprint_retirement_inventory(engine)
        assert captured.value.references[0].key == (("event_id", "waiver-event"),)
        assert captured.value.references[0].owner_board_id == "board-b"
    else:
        inventory = (await read_sprint_retirement_inventory(engine)).historical_references
        inventory.require_resolved_scopes()
        assert next(r for r in inventory.references if r.table == "policy_waiver_events").owner_board_id == "board-a"


@pytest.mark.asyncio
async def test_quality_findings_do_not_support_sprint_anchors_in_current_physical_contract(database):
    engine, _ = database
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="ck_quality_finding_subject_types"):
            await connection.execute(insert(Base.metadata.tables["quality_findings"]).values(
                id="finding", receipt_id="receipt", board_id="board-a", subject_type="spec", subject_id="spec-a",
                assessment_kind="spec_validation", finding_key="key", category_code="code", taxonomy_version="1",
                severity="high", confidence=1.0, deterministic=True, blocking_eligible=True, title="Finding", detail="Detail",
                anchor_board_id="board-b", anchor_subject_type="sprint", anchor_subject_id="sprint", anchor_subject_version=1,
                anchor_input_digest="c" * 64, anchor_type="whole_artifact", lifecycle="open", created_at=datetime.now(timezone.utc)))
