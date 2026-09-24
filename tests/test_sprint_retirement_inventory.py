"""F2A/F2C structural preflight on disposable physical relational databases."""

from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sprint_retirement_inventory import (
    SprintRetirementInspectionError,
    SprintRetirementRelationsInvalid,
    read_sprint_retirement_inventory,
)
from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec
from legacy_sprint_schema import Base, Card, Sprint


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "unknown"])
async def test_inventory_refuses_non_community_backend_before_connection(dialect):
    def forbidden():
        pytest.fail("unsupported backend connection attempted")

    engine = SimpleNamespace(dialect=SimpleNamespace(name=dialect), connect=forbidden)
    with pytest.raises(SprintRetirementInspectionError, match="backend_unsupported"):
        await read_sprint_retirement_inventory(engine)


@pytest_asyncio.fixture
async def database(tmp_path):
    path = tmp_path / "retirement.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(Base.metadata.create_all)
        for suffix in ("a", "b"):
            await connection.execute(insert(Board.__table__).values(id=f"board-{suffix}", name=suffix, owner_id="owner"))
            await connection.execute(insert(Spec.__table__).values(id=f"spec-{suffix}", board_id=f"board-{suffix}",
                title=suffix, created_by="owner"))
        await connection.execute(insert(Sprint.__table__).values(id="sprint", board_id="board-a", spec_id="spec-a",
            title="Historical Sprint", created_by="owner", status="closed"))
    try:
        yield engine, path
    finally:
        await engine.dispose()


async def add_card(engine, **values):
    async with engine.begin() as connection:
        await connection.execute(insert(Card.__table__).values({
            "id": "card", "board_id": "board-a", "spec_id": "spec-a", "sprint_id": "sprint",
            "title": "Task", "created_by": "owner", "status": "done", **values,
        }))


@pytest.mark.asyncio
async def test_empty_sprint_is_counted_without_fabricating_card_or_history(database):
    engine, path = database
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    result = await read_sprint_retirement_inventory(engine)
    assert dict(result.counts) == {"sprints": 1, "linked_cards": 0, "sprint_history": 0,
        "sprint_qa_items": 0, "sprint_activation_baselines": 0}
    result.require_valid_relations()
    assert result.violations == ()
    assert result == await read_sprint_retirement_inventory(engine)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


@pytest.mark.asyncio
async def test_cross_spec_test_and_opaque_historical_baseline_members_are_preserved(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Spec.__table__).values(id="amendment", board_id="board-a", title="Amendment", created_by="owner"))
        await connection.execute(insert(Base.metadata.tables["sprint_activation_baselines"]).values(
            baseline_ref="historical-ref", board_id="board-a", sprint_id="sprint", spec_id="spec-a",
            sprint_version=1, activated_at=datetime.now(timezone.utc), activated_by="owner", member_count=1,
            members=[{"card_id": "historically-removed-card", "card_type": "normal", "card_version": 1}]))
        await connection.execute(insert(Base.metadata.tables["sprint_history"]).values(id="history", sprint_id="sprint",
            action="closed", actor_type="user", actor_id="owner", actor_name="Owner", changes=[{"old": "review"}]))
        await connection.execute(insert(Base.metadata.tables["sprint_qa_items"]).values(id="question", sprint_id="sprint",
            question="Still applicable?", asked_by="owner"))
    await add_card(engine, card_type="test", spec_id="amendment", linked_test_task_ids=["bug"])
    result = await read_sprint_retirement_inventory(engine)
    result.require_valid_relations()
    assert all(count == 1 for _, count in result.counts)
    # Read-only inventory doesn't declare the unanswered question resolved,
    # validate a historical hash, or confer new approval to the amendment.
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT answer FROM sprint_qa_items"))).scalar_one() is None
        assert (await connection.execute(text("SELECT status FROM cards"))).scalar_one() == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(("changes", "relation", "reason"), [
    ({"sprint_id": "missing"}, "cards.sprint_id", "orphan"),
    ({"board_id": "board-b", "spec_id": "spec-b"}, "cards.sprint_id", "cross_board"),
    ({"spec_id": "missing"}, "cards.spec_id", "orphan"),
    ({"spec_id": "spec-b"}, "cards.spec_id", "cross_board"),
    ({"board_id": "missing", "spec_id": None}, "cards.board_id", "orphan"),
])
async def test_invalid_card_relations_block_whole_inventory_with_ids(database, changes, relation, reason):
    engine, _ = database
    await add_card(engine, **changes)
    result = await read_sprint_retirement_inventory(engine)
    with pytest.raises(SprintRetirementRelationsInvalid) as captured:
        result.require_valid_relations()
    assert captured.value.inventory is result
    assert any(v.row_id == "card" and v.relation == relation and v.reason == reason for v in result.violations)


@pytest.mark.asyncio
async def test_all_orphan_children_are_reported_beyond_twenty_row_health_sample(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["sprint_qa_items"]), [
            {"id": f"q-{index:03}", "sprint_id": "missing", "question": "Question", "asked_by": "owner"}
            for index in range(25)
        ])
    result = await read_sprint_retirement_inventory(engine)
    assert dict(result.counts)["sprint_qa_items"] == 25
    assert len(result.violations) == 25
    assert {v.row_id for v in result.violations} == {f"q-{index:03}" for index in range(25)}
    assert {v.target_id for v in result.violations} == {"missing"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("assignment", "relation", "reason"), [
    ("board_id='missing'", "sprints.board_id", "orphan"),
    ("spec_id='missing'", "sprints.spec_id", "orphan"),
    ("spec_id='spec-b'", "sprints.spec_id", "cross_board"),
    ("origin_sprint_id='missing'", "sprints.origin_sprint_id", "orphan"),
    ("origin_bug_id='missing'", "sprints.origin_bug_id", "orphan"),
])
async def test_sprint_ancestry_scope_errors_are_not_silently_repaired(database, assignment, relation, reason):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text(f"UPDATE sprints SET {assignment}"))
    result = await read_sprint_retirement_inventory(engine)
    assert any(v.row_id == "sprint" and v.relation == relation and v.reason == reason for v in result.violations)


@pytest.mark.asyncio
async def test_foreign_origins_and_baseline_scope_block_with_source_ids(database):
    engine, _ = database
    await add_card(engine, board_id="board-b", spec_id="spec-b", sprint_id=None, card_type="bug")
    async with engine.begin() as connection:
        await connection.execute(insert(Sprint.__table__).values(id="foreign-sprint", board_id="board-b", spec_id="spec-b",
            title="Foreign", created_by="owner"))
        await connection.execute(text("UPDATE sprints SET origin_sprint_id='foreign-sprint', origin_bug_id='card' WHERE id='sprint'"))
        await connection.execute(insert(Base.metadata.tables["sprint_activation_baselines"]).values(
            baseline_ref="wrong-scope", board_id="board-b", sprint_id="sprint", spec_id="spec-b",
            sprint_version=1, activated_at=datetime.now(timezone.utc), activated_by="owner", member_count=1,
            members=[{"card_id": "historical", "card_type": "normal", "card_version": 1}]))
    result = await read_sprint_retirement_inventory(engine)
    assert {(v.relation, v.row_id, v.reason) for v in result.violations} == {
        ("sprints.origin_sprint_id", "sprint", "cross_board"),
        ("sprints.origin_bug_id", "sprint", "cross_board"),
        ("sprint_activation_baselines.sprint_id", "wrong-scope", "cross_board"),
        ("sprint_activation_baselines.spec_id", "wrong-scope", "scope_mismatch"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("ddl", [
    "CREATE TABLE extension (id TEXT PRIMARY KEY, parent TEXT REFERENCES sprints(id))",
    "CREATE TABLE extension (id TEXT PRIMARY KEY, parent TEXT REFERENCES sprint_history(id))",
    "CREATE TABLE extension (id TEXT PRIMARY KEY, origin_sprint_id TEXT)",
])
async def test_unclassified_physical_references_refuse_even_an_empty_extension(database, ddl):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text(ddl))
    with pytest.raises(SprintRetirementInspectionError, match="unclassified"):
        await read_sprint_retirement_inventory(engine)


@pytest.mark.asyncio
async def test_incomplete_schema_and_budget_exhaustion_are_not_empty_success(database):
    engine, _ = database
    await add_card(engine)
    with pytest.raises(SprintRetirementInspectionError, match="row_limit"):
        await read_sprint_retirement_inventory(engine, max_rows=1)
    # A subsequent complete read proves connection/transaction cleanup on failure.
    assert dict((await read_sprint_retirement_inventory(engine)).counts)["linked_cards"] == 1
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE sprint_qa_items"))
    with pytest.raises(SprintRetirementInspectionError, match="missing_table:sprint_qa_items"):
        await read_sprint_retirement_inventory(engine)


@pytest.mark.asyncio
async def test_wal_writer_between_queries_cannot_mix_snapshots(database):
    engine, path = database
    committed = False

    def between_queries(connection, cursor, statement, parameters, context, executemany):
        nonlocal committed
        if "FROM cards c LEFT JOIN sprints" in statement and not committed:
            with sqlite3.connect(path, timeout=1) as writer:
                writer.execute("INSERT INTO sprint_qa_items (id,sprint_id,question,asked_by) VALUES ('concurrent','sprint','New','owner')")
            committed = True

    event.listen(engine.sync_engine, "before_cursor_execute", between_queries)
    try:
        result = await read_sprint_retirement_inventory(engine)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", between_queries)
    assert committed
    assert dict(result.counts)["sprint_qa_items"] == 0
    # Next transaction sees the committed WAL row, proving the writer succeeded.
    assert dict((await read_sprint_retirement_inventory(engine)).counts)["sprint_qa_items"] == 1
