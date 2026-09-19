"""Real write UoW: atomic classification, actor-bound replay and history."""

import asyncio
import copy
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureCandidateDecisionRow,
    ArchitectureClassificationReceiptRow,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_structured_spec import (
    CommunitySqlAlchemyStructuredSpecStore,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.ports.architecture_classification import (
    ArchitectureClassificationPersistenceState as State,
    ArchitectureClassificationReceipt,
    ArchitectureDecisionRecord,
)

import test_architecture_candidates_integration as candidate_fixtures

adopted_context = candidate_fixtures.adopted_context


async def prepared(db, *, key="batch-1", actor="author", candidate="arqc_candidate"):
    await CommunityUnitOfWork(db).begin_write()
    record = await CommunitySqlAlchemyStructuredSpecStore().get(db, spec_id="spec")
    expected = record.version
    record.version += 1
    record.integration_requirements = [
        *(record.integration_requirements or []),
        {
            "id": f"ir_{key}",
            "title": "Publish orders",
            "integration_type": "event",
            "status": "active",
        },
    ]
    decision = ArchitectureDecisionRecord(
        id=f"decision-{key}",
        spec_id=record.id,
        spec_edition=record.edition,
        spec_version=record.version,
        candidate_id=candidate,
        source_digest="a" * 64,
        root_design_id="root",
        interface_id="boundary",
        source_contract_json='{"event_schema":{},"schema_ref":"https://private.invalid/schema"}',
        adopted_sources=(("adopted", 2),),
        actor_id=actor,
        classified_at=datetime(2026, 9, 19, 15, 0, tzinfo=UTC),
        disposition="promote_to_ir",
        integration_requirement_ids=(f"ir_{key}",),
    )
    receipt = ArchitectureClassificationReceipt(
        board_id=record.board_id,
        spec_id=record.id,
        actor_id=actor,
        idempotency_key=key,
        request_digest="b" * 64,
        result={
            "spec_version": record.version,
            "decisions": [decision.id],
            "ir_ids": [f"ir_{key}"],
        },
    )
    return record, expected, (decision,), receipt


async def save(db, batch, *, changed_fields=("integration_requirements",)):
    record, expected, decisions, receipt = batch
    return (
        await CommunitySqlAlchemyStructuredSpecStore().save_architecture_classification(
            db,
            record,
            expected_spec_version=expected,
            expected_spec_edition=record.edition,
            changed_fields=changed_fields,
            decisions=decisions,
            receipt=receipt,
        )
    )


async def snapshot(db):
    row = await CommunitySqlAlchemyStructuredSpecStore().get(db, spec_id="spec")
    counts = [
        await db.scalar(select(func.count()).select_from(table))
        for table in (
            ArchitectureClassificationReceiptRow,
            ArchitectureCandidateDecisionRow,
        )
    ]
    return row, counts


@pytest.mark.asyncio
async def test_classification_is_atomic_and_exact_replay_is_read_only(adopted_context):
    db = adopted_context
    before = await snapshot(db)
    batch = await prepared(db)
    result = await save(db, batch)
    assert result.state == State.APPLIED and result.receipt == batch[3]
    # A second physical connection cannot see any part before caller commit.
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as observer:
        assert await snapshot(observer) == before
    await db.commit()
    db.expire_all()
    after, counts = await snapshot(db)
    assert after.version == before[0].version + 1 and after.edition == 2
    assert after.integration_requirements == batch[0].integration_requirements
    assert counts == [1, 1]
    store = CommunitySqlAlchemyStructuredSpecStore()
    assert (
        await store.list_architecture_decisions(db, spec_id="spec", spec_edition=2)
        == batch[2]
    )
    assert (
        await store.list_architecture_decisions(db, spec_id="spec", spec_edition=1)
        == ()
    )
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        replay = await save(db, batch)
        await db.commit()
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert replay.state == State.REPLAYED and replay.receipt == batch[3]
    assert statements and all(
        item.lstrip().upper().startswith("SELECT") for item in statements
    )
    assert await snapshot(db) == (after, counts)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["payload", "actor"])
async def test_key_reuse_conflicts_without_revealing_another_actors_result(
    adopted_context, case
):
    db = adopted_context
    batch = await prepared(db)
    assert (await save(db, batch)).state == State.APPLIED
    await db.commit()
    before = await snapshot(db)
    record, expected, decisions, receipt = copy.deepcopy(batch)
    if case == "actor":
        receipt = replace(receipt, actor_id="different-actor")
        decisions = tuple(
            replace(item, actor_id=receipt.actor_id) for item in decisions
        )
    else:
        receipt = replace(receipt, request_digest="c" * 64)
    result = await save(db, (record, expected, decisions, receipt))
    assert result.state == State.IDEMPOTENCY_CONFLICT and result.receipt is None
    await db.commit()
    assert await snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["version", "edition", "board"])
async def test_failed_fence_rolls_back_key_decisions_and_irs(adopted_context, case):
    db = adopted_context
    batch = await prepared(db)
    # Deliberately exercise each SQL predicate independently, including an
    # edition changed without a version bump by an out-of-band legacy writer.
    changes = {
        "version": "version=version+1",
        "edition": "edition=edition+1",
        "board": "board_id='other-board'",
    }
    await db.execute(text(f"UPDATE specs SET {changes[case]} WHERE id='spec'"))
    db.expire_all()
    before = await snapshot(db)
    result = await save(db, batch)
    assert result.state == State.VERSION_CONFLICT
    await db.commit()
    assert await snapshot(db) == before


@pytest.mark.asyncio
async def test_outer_rollback_removes_every_part_of_an_applied_batch(adopted_context):
    db = adopted_context
    before = await snapshot(db)
    batch = await prepared(db)
    assert (await save(db, batch)).state == State.APPLIED
    await db.rollback()
    assert await snapshot(db) == before


@pytest.mark.asyncio
async def test_decision_payload_cannot_disagree_with_persisted_scope(adopted_context):
    db = adopted_context
    batch = await prepared(db)
    assert (await save(db, batch)).state == State.APPLIED
    await db.commit()
    row = await db.get(ArchitectureCandidateDecisionRow, batch[2][0].id)
    row.payload = {**row.payload, "spec_id": "different-spec"}
    await db.commit()
    with pytest.raises(ValueError, match="architecture_decision_storage_drift"):
        await CommunitySqlAlchemyStructuredSpecStore().list_architecture_decisions(
            db, spec_id="spec", spec_edition=2
        )


@pytest.mark.asyncio
async def test_decision_insert_failure_cannot_leave_irs_or_claimed_key(adopted_context):
    db = adopted_context
    batch = await prepared(db)
    before = await snapshot(db)

    def fail_decision(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().startswith(
            "INSERT INTO architecture_candidate_decisions"
        ):
            raise RuntimeError("injected decision persistence failure")

    event.listen(db.bind.sync_engine, "before_cursor_execute", fail_decision)
    try:
        with pytest.raises(RuntimeError, match="injected decision"):
            await save(db, batch)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", fail_decision)
    # Committing the caller transaction must not resurrect the good half.
    await db.commit()
    assert await snapshot(db) == before
    # A retry can use the key: the failed attempt did not reserve it.
    await CommunityUnitOfWork(db).begin_write()
    assert (await save(db, batch)).state == State.APPLIED
    await db.commit()


@pytest.mark.asyncio
async def test_latest_decision_group_preserves_history_and_all_scope_fragments(
    adopted_context,
):
    db = adopted_context
    record, expected, decisions, receipt = await prepared(db)
    first = replace(
        decisions[0],
        scope_paths=("/event_schema",),
        remainder_reason="Other clauses remain context",
    )
    second = replace(first, id="second-fragment", scope_paths=("/error_contract",))
    unrelated = replace(first, id="unrelated", candidate_id="other-candidate")
    assert (
        await save(db, (record, expected, (first, second, unrelated), receipt))
    ).state == State.APPLIED
    await db.commit()
    store = CommunitySqlAlchemyStructuredSpecStore()
    assert set(
        await store.list_architecture_decisions(db, spec_id="spec", spec_edition=2)
    ) == {first, second, unrelated}
    record, expected, decisions, receipt = await prepared(db, key="batch-2")
    revised = replace(
        decisions[0],
        disposition="context_only",
        integration_requirement_ids=(),
        reason="Outside this scope",
    )
    # A context-only decision never deletes or edits the previously created IR.
    assert (
        await save(db, (record, expected, (revised,), receipt), changed_fields=())
    ).state == State.APPLIED
    await db.commit()
    assert set(
        await store.list_architecture_decisions(db, spec_id="spec", spec_edition=2)
    ) == {revised, unrelated}
    assert (
        await db.scalar(
            select(func.count()).select_from(ArchitectureCandidateDecisionRow)
        )
        == 4
    )
    row = await store.get(db, spec_id="spec")
    assert [item["id"] for item in row.integration_requirements] == ["ir_batch-1"]
    assert row.version == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("same_key", [True, False])
async def test_competing_write_uows_replay_or_conflict_without_duplicate_irs(
    adopted_context, same_key
):
    db = adopted_context
    batch = await prepared(db)
    await db.rollback()
    factory = async_sessionmaker(
        db.bind, expire_on_commit=False, sync_session_class=CommunitySemanticSession
    )

    async def writer(second):
        async with factory() as session:
            await CommunityUnitOfWork(session).begin_write()
            requested = copy.deepcopy(batch)
            if second and not same_key:
                record, expected, decisions, receipt = requested
                requested = (
                    record,
                    expected,
                    tuple(replace(item, id="competing-decision") for item in decisions),
                    replace(receipt, idempotency_key="competing-key"),
                )
            result = await save(session, requested)
            await session.commit()
            return result

    results = await asyncio.gather(writer(False), writer(True))
    assert {item.state for item in results} == {
        State.APPLIED,
        State.REPLAYED if same_key else State.VERSION_CONFLICT,
    }
    db.expire_all()
    row, counts = await snapshot(db)
    assert row.version == 2 and len(row.integration_requirements) == 1
    assert counts == [1, 1]
