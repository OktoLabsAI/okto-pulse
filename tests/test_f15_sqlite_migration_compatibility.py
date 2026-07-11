"""F15 characterization of upgrades from an existing Local First database."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from sqlalchemy import text

from okto_pulse.community.adapters import relational_schema_steps
from okto_pulse.community.adapters.sqlalchemy_database import (
    configure_community_database,
)


def _create_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE cards (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE boards (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE comments (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL
            );
            CREATE TABLE specs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL
            );
            INSERT INTO cards (id, title, status)
            VALUES ('card-1', 'preserve-card', 'nao_iniciado');
            INSERT INTO boards (id, name)
            VALUES ('board-1', 'preserve-board');
            INSERT INTO comments (id, content)
            VALUES ('comment-1', 'preserve-comment');
            INSERT INTO specs (id, title)
            VALUES ('spec-1', 'preserve-spec');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_f15_existing_sqlite_database_upgrades_twice_without_data_drift(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(database_path)
    runtime = configure_community_database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )

    async def drive_upgrade() -> tuple[dict[str, set[str]], tuple[str, ...]]:
        steps = (
            relational_schema_steps._migrate_card_statuses,
            relational_schema_steps._migrate_add_priority_column,
            relational_schema_steps._migrate_add_bug_card_columns,
            relational_schema_steps._migrate_add_realm_id,
            relational_schema_steps._migrate_add_comment_choice_columns,
            relational_schema_steps._migrate_add_skip_rules_coverage,
            relational_schema_steps._migrate_add_skip_trs_coverage,
            relational_schema_steps._migrate_add_decisions_columns,
        )
        try:
            for _ in range(2):
                for step in steps:
                    await step()

            async with runtime.engine.connect() as connection:
                columns: dict[str, set[str]] = {}
                for table in ("cards", "boards", "comments", "specs"):
                    result = await connection.execute(
                        text(f"PRAGMA table_info({table})")
                    )
                    columns[table] = {str(row[1]) for row in result}

                card = (
                    await connection.execute(
                        text("SELECT id, title, status FROM cards WHERE id = 'card-1'")
                    )
                ).one()
                board = (
                    await connection.execute(
                        text("SELECT id, name FROM boards WHERE id = 'board-1'")
                    )
                ).one()
                comment = (
                    await connection.execute(
                        text("SELECT id, content FROM comments WHERE id = 'comment-1'")
                    )
                ).one()
                spec = (
                    await connection.execute(
                        text("SELECT id, title FROM specs WHERE id = 'spec-1'")
                    )
                ).one()
                return columns, (*card, *board, *comment, *spec)
        finally:
            await runtime.engine.dispose()

    columns, sentinel = asyncio.run(drive_upgrade())

    assert {"priority", "card_type", "expected_behavior"} <= columns["cards"]
    assert "realm_id" in columns["boards"]
    assert {"comment_type", "choices", "responses", "allow_free_text"} <= columns[
        "comments"
    ]
    assert {
        "skip_rules_coverage",
        "skip_trs_coverage",
        "decisions",
        "skip_decisions_coverage",
    } <= columns["specs"]
    assert sentinel == (
        "card-1",
        "preserve-card",
        "not_started",
        "board-1",
        "preserve-board",
        "comment-1",
        "preserve-comment",
        "spec-1",
        "preserve-spec",
    )
