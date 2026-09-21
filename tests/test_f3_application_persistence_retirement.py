"""Operational persistence cannot traverse retired entities or repair their history."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec, SpecQAItem
from legacy_sprint_schema import Base, Sprint, SprintQAItem
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter, ApplicationGroupCountQuery, ApplicationQuery, ApplicationRecord,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["sprint", "sprint_history", "sprint_qa_item"])
@pytest.mark.parametrize("operation", ["get", "list", "list_with_count", "count", "group_count", "add", "delete", "fence", "refresh"])
async def test_retired_entities_are_refused_before_database_effects(entity, operation):
    adapter = CommunitySqlAlchemyApplicationPersistence()
    context = AsyncMock()
    query = ApplicationQuery(entity=entity)
    record = ApplicationRecord(entity, {"id": "historical"})
    with pytest.raises(ValueError, match="unsupported_application_entity"):
        if operation == "get":
            await adapter.get(context, entity=entity, record_id="historical")
        elif operation == "fence":
            await adapter.fence(context, entity=entity, record_id="historical", expected_values={})
        elif operation in {"add", "delete", "refresh"}:
            await getattr(adapter, operation)(context, record)
        elif operation == "group_count":
            await adapter.group_count(context, ApplicationGroupCountQuery(entity, ("id",)))
        else:
            await getattr(adapter, operation)(context, query)
    for method in ("execute", "get", "flush", "commit", "delete", "refresh"):
        getattr(context, method).assert_not_awaited()


_RETIRED_RELATION_QUERIES = [
    ApplicationQuery("board", includes=("sprints",)),
    ApplicationQuery("board", includes=("specs.sprints",)),
    ApplicationQuery("card", includes=("sprint",)),
    ApplicationQuery("spec", select_fields=("sprints",)),
    ApplicationQuery("card", filters=(ApplicationFilter("sprint", "eq", None),)),
    ApplicationQuery("board", order_by=(("sprints", False),)),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,query", [
    (operation, query) for operation in ("list", "list_with_count") for query in _RETIRED_RELATION_QUERIES
] + [("group_count", ApplicationGroupCountQuery("board", ("sprints",)))])
async def test_retired_relationships_cannot_bypass_catalog_via_query_shapes(operation, query):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements = []
    event.listen(engine.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    try:
        sessions = build_community_session_factory(engine)
        async with sessions() as db:
            adapter = CommunitySqlAlchemyApplicationPersistence()
            with pytest.raises(ValueError, match="unsupported_application_(relationship|attribute|projection|group_field)"):
                await getattr(adapter, operation)(db, query)
        assert statements == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_table", [False, True])
async def test_startup_backfill_only_repairs_current_qa_and_preserves_legacy_rows(legacy_table):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements = []
    try:
        tables = [table for table in Base.metadata.tables.values()
                  if legacy_table or not table.name.startswith("sprint")]
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        sessions = build_community_session_factory(engine)
        async with sessions() as db:
            db.add_all([
                Board(id="board", name="Board", owner_id="owner", realm_id=RealmScope.local().realm_id),
                Spec(id="spec", board_id="board", title="Spec", created_by="owner"),
                SpecQAItem(id="answered", spec_id="spec", question="Answered", answer="Preserved", asked_by="owner"),
                SpecQAItem(id="open", spec_id="spec", question="Open", asked_by="owner"),
            ])
            if legacy_table:
                db.add_all([
                    Sprint(id="legacy", board_id="board", spec_id="spec", title="Historical", created_by="owner"),
                    SprintQAItem(id="historical", sprint_id="legacy", question="Original", answer="Original answer", asked_by="owner"),
                ])
            await db.commit()

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with sessions() as db:
            adapter = CommunitySqlAlchemyApplicationPersistence()
            assert await adapter.backfill_qa_answered_at(db) == {"spec_qa_items": 1}
            assert await adapter.backfill_qa_answered_at(db) == {}
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert not any("sprint" in sql for sql in statements)
        async with sessions() as db:
            rows = (await db.execute(select(SpecQAItem))).scalars().all()
            assert {row.id: row.answered_at is not None for row in rows} == {"answered": True, "open": False}
            if legacy_table:
                row = await db.get(SprintQAItem, "historical")
                assert row.answered_at is None and row.answer == "Original answer"
    finally:
        await engine.dispose()
