"""F2C/F3 mixed-event preservation in the integrated disposable preflight."""

import sqlite3

import pytest
from sqlalchemy import insert, text

from okto_pulse.community.adapters.sprint_retirement_inventory import read_sprint_retirement_inventory
from okto_pulse.community.adapters.sprint_retirement_work import SprintRetirementWorkError
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.core.events.types import CardCreated, SprintClosed
import test_sprint_retirement_inventory as relational

database = relational.database


async def seed(engine, *, handler="ConsolidationEnqueuer", status="pending", event_type=None, payload=None, board_id="board-a"):
    event = SprintClosed(event_id="event", board_id=board_id, sprint_id="sprint")
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["domain_events"]).values(
            id=event.event_id, board_id=event.board_id, event_type=event_type or event.event_type,
            payload_json=event.payload_for_storage() if payload is None else payload))
        await connection.execute(insert(Base.metadata.tables["domain_event_handler_executions"]).values(
            id="execution", event_id=event.event_id, handler_name=handler, status=status, attempts=3,
            last_error="historical error"))


@pytest.mark.asyncio
async def test_mixed_and_exclusive_events_with_jobs_are_inventoried_without_processing(database):
    engine, path = database
    await seed(engine)
    card = CardCreated(board_id="board-a", card_id="card", spec_id="spec-a")
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["domain_events"]).values(id="card-event", board_id=card.board_id,
            event_type=card.event_type, payload_json={**card.payload_for_storage(), "sprint_id": "sprint"}))
        await connection.execute(insert(Base.metadata.tables["domain_event_handler_executions"]).values(
            id="card-execution", event_id="card-event", handler_name="ConsolidationEnqueuer", status="pending"))
        await connection.execute(insert(Base.metadata.tables["consolidation_queue"]).values(
            id="queue", board_id="board-a", artifact_type="sprint", artifact_id="sprint", status="pending"))
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    result = await read_sprint_retirement_inventory(engine)
    result.work.require_classified_work()
    assert dict(result.work.scanned_counts) == {"domain_events": 2, "domain_event_handler_executions": 2, "consolidation_queue": 1}
    assert {item.row_id: item.action for item in result.work.items} == {
        "event": "supersede", "execution": "supersede", "queue": "supersede",
        "card-event": "preserve", "card-execution": "preserve",
    }
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "reason"), [
    ({"handler": "NewHandler"}, "unclassified_handler_effects"),
    ({"status": "processing"}, "in_flight_execution"),
    ({"event_type": "sprint.new_event"}, "unknown_sprint_event"),
    ({"payload": {"sprint_id": "sprint", "card_id": "survives"}}, "exclusive_contract_drift"),
    ({"board_id": "board-b"}, "cross_board_sprint_reference"),
])
async def test_ambiguity_requires_review_with_exact_work_ids(database, arguments, reason):
    engine, _ = database
    await seed(engine, **arguments)
    inventory = (await read_sprint_retirement_inventory(engine)).work
    with pytest.raises(SprintRetirementWorkError) as captured:
        inventory.require_classified_work()
    assert captured.value.items
    assert any(item.reason == reason for item in captured.value.items)
    assert {item.row_id for item in captured.value.items} <= {"event", "execution"}


@pytest.mark.asyncio
async def test_completed_execution_history_stays_completed(database):
    engine, _ = database
    await seed(engine, status="done")
    inventory = (await read_sprint_retirement_inventory(engine)).work
    execution = next(item for item in inventory.items if item.row_id == "execution")
    assert execution.action == "preserve" and execution.reason == "completed_execution_history"
    assert execution.status == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [False, 0, [], "", {"text": "x" * 131073}])
async def test_invalid_or_oversized_queue_payload_is_not_hidden(database, payload):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["consolidation_queue"]).values(id="queue", board_id="board-a",
            artifact_type="sprint", artifact_id="sprint", payload=payload))
    with pytest.raises(SprintRetirementWorkError, match="queue_invalid|payload_limit"):
        await read_sprint_retirement_inventory(engine)


@pytest.mark.asyncio
async def test_combined_row_limit_and_orphan_execution_fail_closed(database):
    engine, _ = database
    await seed(engine)
    # One Sprint + one event consume the shared budget; execution must not vanish.
    with pytest.raises(SprintRetirementWorkError, match="row_limit"):
        await read_sprint_retirement_inventory(engine, max_rows=2)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE domain_event_handler_executions SET event_id='missing'"))
    with pytest.raises(SprintRetirementWorkError, match="orphan_execution:execution:missing"):
        await read_sprint_retirement_inventory(engine)
