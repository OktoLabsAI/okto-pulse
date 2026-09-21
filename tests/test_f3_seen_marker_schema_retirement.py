"""Backfill keeps resolved scopes when the historical QA parent disappears."""

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import relational_schema_steps as steps


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_seen_backfill_preserves_resolved_boards_across_schema_retirement(monkeypatch, legacy):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    # Minimal historical shapes expose a nullable board_id, unlike the current
    # NOT NULL mapper. These are migration fixtures, not runtime admission.
    tables = {
        "agent_seen_items": "id TEXT PRIMARY KEY, agent_id TEXT, item_id TEXT, item_type TEXT, board_id TEXT",
        "comments": "id TEXT, card_id TEXT",
        "qa_items": "id TEXT, card_id TEXT",
        "cards": "id TEXT, board_id TEXT",
        "spec_qa_items": "id TEXT, spec_id TEXT",
        "specs": "id TEXT, board_id TEXT",
        "ideation_qa_items": "id TEXT, ideation_id TEXT",
        "ideations": "id TEXT, board_id TEXT",
        "refinement_qa_items": "id TEXT, refinement_id TEXT",
        "refinements": "id TEXT, board_id TEXT",
        "activity_logs": "id TEXT, board_id TEXT",
        "agents": "id TEXT, board_id TEXT",
        "agent_boards": "agent_id TEXT, board_id TEXT, granted_at TEXT",
    }
    statements = []
    try:
        async with engine.begin() as connection:
            for name, columns in tables.items():
                await connection.exec_driver_sql(f"CREATE TABLE {name} ({columns})")
            await connection.exec_driver_sql("INSERT INTO cards VALUES ('card', 'artifact-board')")
            await connection.exec_driver_sql("INSERT INTO agents VALUES ('agent', 'default-board')")
            await connection.exec_driver_sql("INSERT INTO agent_seen_items VALUES "
                "('current', 'agent', 'card', 'card', NULL),"
                "('preserved', 'agent', 'missing', 'qa', 'original-board'),"
                "('unresolved', 'absent', 'missing', 'qa', NULL)")
            if legacy:
                await connection.exec_driver_sql("CREATE TABLE sprints(id TEXT, board_id TEXT)")
                await connection.exec_driver_sql("CREATE TABLE sprint_qa_items(id TEXT, sprint_id TEXT)")
                await connection.exec_driver_sql("INSERT INTO sprints VALUES ('old', 'historical-board')")
                await connection.exec_driver_sql("INSERT INTO sprint_qa_items VALUES ('old-qa', 'old')")
                await connection.exec_driver_sql("INSERT INTO agent_seen_items VALUES "
                    "('historical', 'agent', 'old-qa', 'qa', NULL)")
        await steps._migrate_add_agent_seen_board_id()
        async with engine.begin() as connection:
            before = (await connection.exec_driver_sql("SELECT * FROM agent_seen_items ORDER BY id")).all()
            scopes = {row.id: row.board_id for row in before}
            assert scopes == {"current": "artifact-board", "preserved": "original-board", "unresolved": None,
                **({"historical": "historical-board"} if legacy else {})}
            if legacy:
                await connection.exec_driver_sql("DROP TABLE sprint_qa_items")
                await connection.exec_driver_sql("DROP TABLE sprints")
        event.listen(engine.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2].lower()))
        await steps._migrate_add_agent_seen_board_id()
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT * FROM agent_seen_items ORDER BY id")).all() == before
        assert not any("from sprint_qa_items" in sql or "join sprints" in sql for sql in statements)
    finally:
        await engine.dispose()
