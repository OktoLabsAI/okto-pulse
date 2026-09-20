"""Offline SQL retirement composed with the actual Grafx removal plans."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.core.application.global_outbox_dead_letter import GlobalOutboxDeadLetterError, GlobalOutboxDeadLetterOperations
from okto_pulse.core.ports.global_outbox import GLOBAL_OUTBOX_RETIRED_SENTINEL, GlobalOutboxMutationConflict
from okto_pulse.core.ports.global_retirement_graph import GlobalGraphRetirementPlan, GlobalRetirementSourceFacts
from okto_pulse.core.ports.retirement_graph import GraphRetirementPlan
from okto_pulse.community.adapters import global_outbox_retirement as retirement
from okto_pulse.community.adapters.coordination import CommunitySqlAlchemyClaimRepository
from okto_pulse.community.adapters.grafx_global_retirement import apply_global_graph_retirement
from okto_pulse.community.adapters.grafx_sprint_retirement import apply_sprint_graph_retirement
from okto_pulse.community.adapters.sqlalchemy_global_outbox import CommunitySqlAlchemyGlobalOutboxStore
from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit, GlobalUpdateOutbox, KuzuNodeRef
from okto_pulse.community.adapters.sqlalchemy_queue_health import CommunitySqlAlchemyQueueHealthReader
from test_grafx_global_retirement import graphs as _graphs, prepare as graph_plans

graphs = _graphs

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
ORIGINS = (("board-a", ("origin",)),)


def logical_plan():
    board = GraphRetirementPlan("board-a", "1" * 64, "2" * 64, (("Criterion", "outcome"), ("Entity", "root")), 4)
    return GlobalGraphRetirementPlan((GlobalRetirementSourceFacts(board, ("outcome", "root"), 4, 2),),
        "3" * 64, "4" * 64, ("digest",), 1, (("board-a", 4, 2),))


async def seed(engine):
    async with engine.begin() as connection:
        for model in retirement._MODELS:
            await connection.run_sync(model.__table__.create)
        for identity in ("exclusive", "failed", "done", "mixed", "other"):
            board = "board-b" if identity == "other" else "board-a"
            nodes = [("Entity", "root"), ("Criterion", "outcome")]
            if identity == "mixed":
                nodes.append(("Entity", "spec"))
            counts = dict(nodes_added=len(nodes), nodes_updated=0, nodes_superseded=0, edges_added=4)
            await connection.execute(insert(ConsolidationAudit).values(session_id=identity, board_id=board,
                artifact_type="sprint", artifact_id="origin", agent_id="system:historical_consolidation",
                started_at=NOW, committed_at=NOW, summary_text=" private original \n Ω ", undo_status="none", **counts))
            for index, (kind, key) in enumerate(nodes):
                await connection.execute(insert(KuzuNodeRef).values(id=f"{identity}-{index}", session_id=identity,
                    board_id=board, kuzu_node_type=kind, kuzu_node_id=key, operation="add", timestamp=NOW))
            await connection.execute(insert(GlobalUpdateOutbox).values(id=identity, event_id=f"evt-{identity}",
                session_id=identity, board_id=board, event_type="consolidation_committed", created_at=NOW,
                payload={"session_id": identity, "artifact_id": "origin", **counts},
                retry_count=-1 if identity == "failed" else 2, last_error=" original diagnostic \n Ω ",
                processed_at=NOW if identity == "done" else None))


@pytest.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox-retirement.db'}")
    await seed(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


async def snapshot(engine):
    async with engine.connect() as connection:
        return await connection.run_sync(retirement._snapshot)


async def prepare(engine, graph_plan=None):
    return await retirement.prepare_outbox_retirement(engine, graph_plan=graph_plan or logical_plan(), archived_origins=ORIGINS)


@pytest.mark.asyncio
async def test_complete_graph_sql_sequence_rollback_replay_and_stale_delivery_protection(database, graphs):
    board, global_db, *_ = graphs
    board_plan, global_plan = graph_plans(graphs)
    plan = await prepare(database, global_plan)
    assert plan.selected_ids == ("exclusive", "failed")
    original = await snapshot(database)
    store = CommunitySqlAlchemyGlobalOutboxStore()
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as session:
        stale = await store.get_events_by_ids(session, ids=("exclusive", "mixed"))
    async def apply(candidate=plan):
        return await retirement.apply_outbox_retirement(database, candidate, global_database=global_db,
            board_databases={"board-a": board})

    with pytest.raises(ValueError, match="board_not_retired"):
        await apply()
    assert await snapshot(database) == original
    apply_sprint_graph_retirement(board, board_plan)
    with pytest.raises(ValueError, match="global_not_retired"):
        await apply()
    apply_global_graph_retirement(global_db, global_plan, board_databases={"board-a": board})

    # A mutation after the first UPDATE must roll back every SQL effect.
    async with database.begin() as connection:
        await connection.execute(text("""CREATE TRIGGER corrupt_outbox_retirement AFTER UPDATE OF retry_count
            ON global_update_outbox WHEN NEW.id='exclusive' BEGIN
            UPDATE kuzu_node_refs SET operation='update' WHERE id='mixed-0'; END"""))
    with pytest.raises(ValueError, match="after_mismatch"):
        await apply()
    assert await snapshot(database) == original
    async with database.begin() as connection:
        await connection.execute(text("DROP TRIGGER corrupt_outbox_retirement"))
    with pytest.raises(ValueError, match="plan_mismatch"):
        await apply(replace(plan, selected_ids=("mixed",)))
    async with database.begin() as connection:
        await connection.execute(text("UPDATE consolidation_audit SET summary_text='changed' WHERE session_id='mixed'"))
    with pytest.raises(ValueError, match="before_mismatch"):
        await apply()
    async with database.begin() as connection:
        await connection.execute(text("UPDATE consolidation_audit SET summary_text=:original WHERE session_id='mixed'"),
            {"original": " private original \n Ω "})
    assert await snapshot(database) == original
    assert await apply() == plan
    after = await snapshot(database)
    assert retirement._sha(after) == plan.after_sha256
    before_doc, after_doc = json.loads(original), json.loads(after)
    for table in ("consolidation_audit", "kuzu_node_refs"):
        assert before_doc[table] == after_doc[table]
    names = before_doc["global_update_outbox"]["columns"]
    for a, b in zip(before_doc["global_update_outbox"]["rows"], after_doc["global_update_outbox"]["rows"], strict=True):
        if a[names.index("id")][1] in plan.selected_ids:
            a[names.index("retry_count")] = ["integer", "-2"]
        assert a == b
    await database.dispose()  # Replay after all SQL connections are reopened.
    assert await apply() == plan
    assert await snapshot(database) == after
    with pytest.raises(ValueError, match="retry_state_requires_review"):
        await prepare(database, global_plan)  # No recapture of transformed history.

    async with sessions() as session:
        claimed = await CommunitySqlAlchemyClaimRepository().claim_global_outbox(session, limit=50)
        assert {row.id for row in claimed} == {"mixed", "other"}
        assert await store.list_terminal_events(session, limit=50) == ()
        health = await CommunitySqlAlchemyQueueHealthReader().global_outbox_dead_letter_snapshot(
            session, board_id="board-a", limit=10, max_outbox_retries=5, dead_letter_retry_sentinel=-1)
        assert health.total_count == 0
        operations = GlobalOutboxDeadLetterOperations(store=store, clock=lambda: NOW)
        verified = await operations.verify(context=session, dead_letter_ids=["exclusive"])
        assert verified["items"][0]["state"] == "superseded"
        assert verified["items"][0]["authoritative_id"] is None
        with pytest.raises(GlobalOutboxDeadLetterError):
            await operations.reprocess(context=session, dead_letter_ids=["exclusive"], reason="attempt_retired_work")

    # A stale writer cannot reopen an event or mark it delivered. Earlier
    # updates in the same save batch also roll back on the retirement fence.
    by_id = {event.id: event for event in stale}
    by_id["mixed"].processed_at = NOW
    by_id["exclusive"].processed_at = NOW
    async with sessions() as session:
        with pytest.raises(GlobalOutboxMutationConflict):
            await store.requeue_terminal_events(session, [by_id["exclusive"]])
        with pytest.raises(GlobalOutboxMutationConflict):
            await store.save_events(session, [by_id["mixed"], by_id["exclusive"]])
        await session.commit()
    assert await snapshot(database) == after
    by_id["mixed"].retry_count = GLOBAL_OUTBOX_RETIRED_SENTINEL
    async with sessions() as session:
        with pytest.raises(GlobalOutboxMutationConflict, match="requires_migration"):
            await store.save_events(session, [by_id["mixed"]])
    assert await snapshot(database) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ("missing_audit", "origin_missing"), ("unknown_payload", "contract_drift"),
    ("wrong_session", "contract_drift"), ("unknown_event", "contract_drift"),
    ("cross_board_ref", "scope_mismatch"), ("missing_ref", "census_mismatch"),
    ("duplicate_ref", "census_mismatch"), ("update", "unproved_update_effects"),
    ("unarchived_origin", "origin_not_archived"), ("undone", "not_current"),
])
async def test_unproved_exclusivity_never_mutates(database, change, reason):
    async with database.begin() as connection:
        if change == "missing_audit":
            await connection.execute(text("DELETE FROM consolidation_audit WHERE session_id='exclusive'"))
        elif change in {"unknown_payload", "wrong_session", "update"}:
            payload = (await connection.execute(select(GlobalUpdateOutbox.payload).where(GlobalUpdateOutbox.id == "exclusive"))).scalar_one()
            if change == "unknown_payload":
                payload["delivery_key"] = "governed"
            elif change == "wrong_session":
                payload["session_id"] = "mixed"
            else:
                payload["nodes_updated"] = 1
                await connection.execute(text("UPDATE consolidation_audit SET nodes_updated=1 WHERE session_id='exclusive'"))
            await connection.execute(GlobalUpdateOutbox.__table__.update().where(GlobalUpdateOutbox.id == "exclusive").values(payload=payload))
        else:
            commands = {
                "unknown_event": "UPDATE global_update_outbox SET event_type='future' WHERE id='exclusive'",
                "cross_board_ref": "UPDATE kuzu_node_refs SET board_id='outside-targets' WHERE id='exclusive-0'",
                "missing_ref": "DELETE FROM kuzu_node_refs WHERE id='exclusive-0'",
                "duplicate_ref": "UPDATE kuzu_node_refs SET kuzu_node_type='Entity',kuzu_node_id='root' WHERE id='exclusive-1'",
                "unarchived_origin": "UPDATE consolidation_audit SET artifact_id='unknown' WHERE session_id='exclusive'",
                "undone": "UPDATE consolidation_audit SET undo_status='undone' WHERE session_id='exclusive'",
            }
            await connection.execute(text(commands[change]))
    before = await snapshot(database)
    with pytest.raises(ValueError, match=reason):
        await prepare(database)
    assert await snapshot(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["_MAX_ROWS", "_MAX_BYTES", "_MAX_ROW_BYTES"])
async def test_snapshot_budgets_fail_closed(database, monkeypatch, budget):
    monkeypatch.setattr(retirement, budget, 1)
    with pytest.raises(ValueError, match="snapshot_limit"):
        await prepare(database)


@pytest.mark.asyncio
async def test_node_membership_does_not_override_non_sprint_session_ownership(database):
    async with database.begin() as connection:
        await connection.execute(text("UPDATE consolidation_audit SET artifact_type='spec' WHERE session_id='exclusive'"))
    before = await snapshot(database)
    assert (await prepare(database)).selected_ids == ("failed",)
    assert await snapshot(database) == before


@pytest.mark.asyncio
async def test_redrive_cannot_assign_the_migration_state_to_a_live_terminal_row(database):
    store = CommunitySqlAlchemyGlobalOutboxStore()
    sessions = async_sessionmaker(database, expire_on_commit=False)
    before = await snapshot(database)
    async with sessions() as session:
        event, = await store.get_events_by_ids(session, ids=("failed",))
        event.retry_count = GLOBAL_OUTBOX_RETIRED_SENTINEL
        with pytest.raises(GlobalOutboxMutationConflict, match="requires_migration"):
            await store.requeue_terminal_events(session, [event])
        await session.commit()
    assert await snapshot(database) == before
