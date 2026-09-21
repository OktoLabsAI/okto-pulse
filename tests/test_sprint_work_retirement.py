from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
import time

import pytest
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from okto_pulse.core.events.types import CardCreated, SprintClosed
from okto_pulse.core.ports.domain_event_delivery import DomainEventFailure
from okto_pulse.core.ports.relational_effects import ConsolidationQueueUpsert
from okto_pulse.core.ports.reconcile_intent import ReconcileIntentCreate
from okto_pulse.core.ports.materialization_health import HealthProbeDeadline
from okto_pulse.community.adapters import sprint_work_retirement as retirement
from okto_pulse.community.adapters.kg_operational import CommunitySqlAlchemyKGWorkerQueue
from okto_pulse.community.adapters.materialization_health import CommunitySqlAlchemyMaterializationCensus
from okto_pulse.community.adapters.relational_effects import CommunitySqlAlchemyRelationalEffects
from okto_pulse.community.adapters.sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import CommunitySqlAlchemyDomainEventDeliveryStore
from okto_pulse.community.adapters.sqlalchemy_queue_health import CommunitySqlAlchemyQueueHealthReader
from okto_pulse.community.adapters.sqlalchemy_kg_health import CommunitySqlAlchemyKGHealthReader
from okto_pulse.community.adapters.sqlalchemy_models import (
    ConsolidationDeadLetter, ConsolidationQueue, DomainEventHandlerExecution, DomainEventRow,
)
from okto_pulse.community.adapters.sprint_retirement_work import SprintRetirementWorkError
from test_card_validation_retirement import prepare as prepare_cards, run as preserve_cards
from test_card_validation_retirement import raw_cards
import test_sprint_retirement_inventory as relational

database = relational.database


async def prepare(engine, tmp_path, *, status="pending", handler="ConsolidationEnqueuer", queue_status="pending", materialize=True):
    async with engine.begin() as connection:
        for identity, model in (("event", SprintClosed(board_id="board-a", sprint_id="sprint")),
                ("done-event", SprintClosed(board_id="board-a", sprint_id="sprint")),
                ("mixed", CardCreated(board_id="board-a", card_id="c1", spec_id="spec-a"))):
            await connection.execute(insert(DomainEventRow).values(id=identity, board_id="board-a",
                event_type=model.event_type, payload_json={**model.payload_for_storage(), **({"sprint_id": "sprint"} if identity == "mixed" else {})}))
        for identity, parent, state, name in (("execution", "event", status, handler),
                ("done-execution", "done-event", "done", "ConsolidationEnqueuer"),
                ("mixed-execution", "mixed", "pending", "ConsolidationEnqueuer")):
            await connection.execute(insert(DomainEventHandlerExecution).values(id=identity, event_id=parent,
                handler_name=name, status=state, attempts=3, last_error="original error",
                processed_at=datetime(2025, 1, 1, tzinfo=timezone.utc) if state == "done" else None))
        for identity, origin, state in (("queue", "sprint", queue_status), ("done-queue", "second", "done")):
            await connection.execute(insert(ConsolidationQueue).values(id=identity, board_id="board-a", artifact_type="sprint",
                artifact_id=origin, status=state, attempts=4, last_error="original queue error"))
    storage, references = await prepare_cards(engine, tmp_path)
    card_receipt = await preserve_cards(engine, storage, references) if materialize else None
    return storage, references, card_receipt


async def run(engine, prepared, **kwargs):
    storage, references, receipt = prepared
    return await retirement.supersede_archived_sprint_work(engine, storage, references, card_receipt=receipt, **kwargs)


async def snapshot(engine):
    async with engine.connect() as connection:
        return {table: {row["id"]: dict(row) for row in (await connection.execute(text(
            f'SELECT * FROM "{table}" ORDER BY id'))).mappings()} for table in retirement._TABLES}


@pytest.mark.asyncio
async def test_supersession_preserves_mixed_events_completed_history_and_original_failures(database, tmp_path):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    before = await snapshot(engine)
    receipt = await run(engine, prepared)
    assert (receipt.events, receipt.executions, receipt.queue_items) == (2, 1, 1)
    assert receipt.origins == 3
    after = await snapshot(engine)
    for table, rows in before.items():
        for identity, row in rows.items():
            expected = {**row, "status": "superseded"} if (table, identity) in {
                ("domain_event_handler_executions", "execution"), ("consolidation_queue", "queue")} else row
            assert after[table][identity] == expected
    assert after["domain_event_handler_executions"]["execution"]["processed_at"] is None
    assert await run(engine, prepared, expected_receipt=receipt) == receipt
    assert await snapshot(engine) == after
    inventory = await relational.read_sprint_retirement_inventory(engine)
    inventory.work.require_classified_work()
    # Pending Card work remains deliverable; retired work is never claimed.
    store = CommunitySqlAlchemyDomainEventDeliveryStore(async_sessionmaker(engine, expire_on_commit=False))
    assert await store.claim_ready(limit=10, now=datetime.now(timezone.utc)) == [("mixed-execution", "mixed")]
    assert await store.begin_attempt("execution") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"status": "processing"}, {"handler": "UnknownHandler"}, {"queue_status": "claimed"}])
async def test_inflight_or_unknown_effects_block_without_changing_any_work(database, tmp_path, arguments):
    engine, _ = database
    prepared = await prepare(engine, tmp_path, **arguments, materialize=False)
    before = await snapshot(engine)
    cards_before = await raw_cards(engine)
    with pytest.raises(SprintRetirementWorkError, match="requires_review"):
        await preserve_cards(engine, *prepared[:2])
    assert await snapshot(engine) == before
    assert await raw_cards(engine) == cards_before


@pytest.mark.asyncio
async def test_work_becoming_inflight_after_card_step_still_blocks_retirement(database, tmp_path):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    async with engine.begin() as connection:
        await connection.execute(update(DomainEventHandlerExecution).where(
            DomainEventHandlerExecution.id == "execution").values(status="processing"))
    before = await snapshot(engine)
    with pytest.raises(SprintRetirementWorkError, match="requires_review"):
        await run(engine, prepared)
    assert await snapshot(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["source", "card_receipt", "journal", "delete", "resurrect"])
async def test_source_and_receipt_drift_are_never_repaired(database, tmp_path, mutation):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    receipt = None
    if mutation in {"journal", "delete", "resurrect"}:
        receipt = await run(engine, prepared)
    if mutation == "card_receipt":
        prepared = (*prepared[:2], replace(prepared[2], evidence_sha256="0" * 64))
    else:
        async with engine.begin() as connection:
            if mutation == "source":
                await connection.execute(update(DomainEventHandlerExecution).where(
                    DomainEventHandlerExecution.id == "execution").values(attempts=9))
            elif mutation == "delete":
                await connection.execute(delete(DomainEventRow).where(DomainEventRow.event_type.in_(
                    (retirement._EVENT, retirement._COMPLETE, retirement.WORK_RETIRED_ORIGIN_EVENT))))
            elif mutation == "resurrect":
                await connection.execute(update(ConsolidationQueue).where(ConsolidationQueue.id == "queue").values(status="pending"))
            else:
                row = (await connection.execute(select(DomainEventRow.__table__).where(DomainEventRow.event_type == retirement._EVENT))).mappings().first()
                payload = deepcopy(row["payload_json"])
                payload["before_status"] = "done"
                await connection.execute(update(DomainEventRow).where(DomainEventRow.id == row["id"]).values(payload_json=payload))
    before = await snapshot(engine)
    with pytest.raises(ValueError, match="(mismatch|source_changed)"):
        await run(engine, prepared, expected_receipt=receipt)
    assert await snapshot(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["abort", "mixed"])
async def test_failure_rolls_back_statuses_and_journal_together(database, tmp_path, trigger):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    async with engine.begin() as connection:
        action = "SELECT RAISE(ABORT,'injected')" if trigger == "abort" else "UPDATE domain_event_handler_executions SET attempts=99 WHERE id='mixed-execution'"
        await connection.execute(text(f"CREATE TRIGGER fail_retirement AFTER INSERT ON domain_events "
            f"WHEN NEW.event_type='{retirement._EVENT}' BEGIN {action}; END"))
    before = await snapshot(engine)
    with pytest.raises(Exception, match="(injected|source_changed)"):
        await run(engine, prepared)
    assert await snapshot(engine) == before


@pytest.mark.asyncio
async def test_source_writer_cannot_race_retirement(database, tmp_path):
    engine, path = database
    prepared = await prepare(engine, tmp_path)
    blocked = []

    def compete(connection, cursor, statement, parameters, context, many):
        if statement.startswith('UPDATE "consolidation_queue"'):
            with sqlite3.connect(path, timeout=0) as rival:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    rival.execute("UPDATE consolidation_queue SET status='claimed' WHERE id='queue'")
                blocked.append(True)
    event.listen(engine.sync_engine, "before_cursor_execute", compete)
    try:
        await run(engine, prepared)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", compete)
    assert blocked == [True]


@pytest.mark.asyncio
async def test_surviving_delivery_may_progress_but_retired_work_cannot_reopen(database, tmp_path):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    receipt = await run(engine, prepared)
    async with engine.begin() as connection:
        await connection.execute(update(DomainEventHandlerExecution).where(
            DomainEventHandlerExecution.id == "mixed-execution").values(status="done", attempts=4))
    assert await run(engine, prepared, expected_receipt=receipt) == receipt
    before = await snapshot(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = CommunitySqlAlchemyDomainEventDeliveryStore(factory)
    await store.mark_event_missing("execution", processed_at=datetime.now(timezone.utc))
    await store.mark_failed("execution", DomainEventFailure("late", False, None, None))
    async with factory() as session:
        effects = CommunitySqlAlchemyRelationalEffects()
        for coalesce in (False, True):
            assert not await effects.upsert_consolidation_queue_unless_tombstoned(session,
                ConsolidationQueueUpsert("board-a", "sprint", "sprint", "high", "live", "late", coalesce_active=coalesce))
            # An archived empty origin never had a queue row to protect.
            assert not await effects.upsert_consolidation_queue_unless_tombstoned(session,
                ConsolidationQueueUpsert("board-b", "sprint", "empty", "high", "live", "late", coalesce_active=coalesce))
        assert await CommunitySqlAlchemyKGWorkerQueue().retry_pending_entry(session,
            board_id="board-a", queue_entry_id="queue") is None
        persistence = CommunitySqlAlchemyConsolidationPersistence()
        with pytest.raises(ValueError, match="artifact_work_retired"):
            await persistence.persist_reconcile_intent(session,
                ReconcileIntentCreate("board-b", "sprint", "empty", 1, "late-delete", ()))
        stale = await persistence.get_queue_entry(session, entry_id="queue")
        with pytest.raises(ValueError, match="artifact_work_retired"):
            await CommunitySqlAlchemyKGWorkerQueue().route_to_dead_letter(session, queue_entry=stale, errors=[])
        stale.status = "pending"
        await persistence.save_queue_entries(session, [stale])
        await persistence.delete_queue_entry(session, entry_id="queue")
        await session.commit()
    assert await snapshot(engine) == before


@pytest.mark.asyncio
async def test_dlq_replay_is_blocked_before_mutating_a_mixed_selection(database, tmp_path):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    await run(engine, prepared)
    async with engine.begin() as connection:
        await connection.execute(delete(ConsolidationQueue).where(ConsolidationQueue.id == "done-queue"))
        for identity, kind, artifact in (("retired", "sprint", "second"), ("live", "card", "c1")):
            await connection.execute(insert(ConsolidationDeadLetter).values(id=identity, board_id="board-a",
                artifact_type=kind, artifact_id=artifact, errors=[], attempts=9 if identity == "retired" else 3))
    before = await snapshot(engine)
    async with AsyncSession(engine) as session:
        result = await CommunitySqlAlchemyKGWorkerQueue().reprocess_dead_letter_rows(session, board_id="board-a",
            dead_letter_ids=("retired", "live"), limit=10)
        assert result["blocked"] and not result["mutated"] and result["error"] == "work_superseded"
        assert len((await session.execute(select(ConsolidationDeadLetter))).scalars().all()) == 2
        await session.commit()
    assert await snapshot(engine) == before
    async with AsyncSession(engine) as session:
        persistence = CommunitySqlAlchemyConsolidationPersistence()
        assert await persistence.count_dead_letters(session, board_id="board-a") == 1
        assert (await CommunitySqlAlchemyQueueHealthReader().health_snapshot(session)).dead_letter_count == 1
        assert (await CommunitySqlAlchemyKGHealthReader().queue_snapshot(session, board_id="board-a")).dead_letter_count == 1
        assert await persistence.delete_poison_dead_letters(session, board_id="board-a", max_attempts=4) == ()
        result = await CommunitySqlAlchemyKGWorkerQueue().reprocess_dead_letter_rows(session, board_id="board-a",
            dead_letter_ids=(), limit=10)
        assert not result["blocked"] and result["requeued_count"] == 1
        remaining = (await session.execute(select(ConsolidationDeadLetter))).scalars().all()
        assert [row.id for row in remaining] == ["retired"]
        assert await persistence.count_dead_letters(session, board_id="board-a") == 0
        assert (await CommunitySqlAlchemyQueueHealthReader().health_snapshot(session)).dead_letter_count == 0
        assert (await CommunitySqlAlchemyKGHealthReader().queue_snapshot(session, board_id="board-a")).dead_letter_count == 0
        await session.commit()
    census = await CommunitySqlAlchemyMaterializationCensus(async_sessionmaker(engine)).snapshot(
        "board-a", generation="test", deadline=HealthProbeDeadline(time.monotonic() + 5))
    assert census.dead_letter_count == 0
    assert census.queue_depth == 1  # The live Card was requeued, not the archived Sprint.
