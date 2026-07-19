from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from okto_pulse.community.adapters import relational_schema_steps as steps


async def _engine_with_minimal_schema(path: Path) -> AsyncEngine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for statement in (
            "CREATE TABLE boards (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "owner_id TEXT NOT NULL, realm_id TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE sprints (id TEXT PRIMARY KEY, board_id TEXT, "
            "FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE)",
            "CREATE TABLE cards (id TEXT PRIMARY KEY, board_id TEXT NOT NULL, "
            "sprint_id TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, "
            "FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE, "
            "FOREIGN KEY(sprint_id) REFERENCES sprints(id) ON DELETE SET NULL)",
            "CREATE TABLE sprint_history (id TEXT PRIMARY KEY, sprint_id TEXT NOT NULL, "
            "actor_id TEXT NOT NULL, created_at TEXT NOT NULL, "
            "FOREIGN KEY(sprint_id) REFERENCES sprints(id) ON DELETE CASCADE)",
            "CREATE TABLE consolidation_dead_letter (id TEXT PRIMARY KEY, "
            "board_id TEXT NOT NULL, artifact_type TEXT NOT NULL, created_at TEXT NOT NULL, "
            "FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE)",
        ):
            await conn.exec_driver_sql(statement)
    return engine


@pytest.mark.asyncio
async def test_known_fixture_fk_pollution_is_repaired_atomically_and_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "data" / "pulse.db"
    fixture_graph_dir = tmp_path / "boards" / "sprint-crud-board-001"
    fixture_graph_dir.mkdir(parents=True)
    (fixture_graph_dir / "graph.lbug").write_bytes(b"synthetic-fixture")
    engine = await _engine_with_minimal_schema(database_path)
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO boards VALUES "
                    "('real-board', 'Real Board', 'real-agent', 'local', "
                    "'2026-07-14 12:00:00'), "
                    "('sprint-crud-board-001', 'Sprint CRUD Board', "
                    "'sprint-crud-agent-001', 'local', '2026-06-27 12:00:00')"
                )
            )
            await conn.execute(
                text("INSERT INTO sprints(id, board_id) VALUES ('real-sprint', 'real-board')")
            )
            await conn.execute(
                text(
                    "INSERT INTO cards VALUES "
                    "('sprint-crud-card-001', 'sprint-crud-board-001', "
                    "'missing-sprint', 'sprint-crud-agent-001', '2026-06-27 12:00:00'), "
                    "('real-card', 'real-board', 'real-sprint', 'real-agent', "
                    "'2026-07-14 12:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO sprint_history VALUES "
                    "('fixture-history', 'missing-sprint', 'sprint-crud-agent-001', "
                    "'2026-07-02 12:00:00'), "
                    "('real-history', 'real-sprint', 'real-agent', '2026-07-14 12:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO consolidation_dead_letter VALUES "
                    "('fixture-dlq', 'rkg04mcp-deadbeef', 'spec', "
                    "'2026-06-30 12:00:00'), "
                    "('real-dlq', 'real-board', 'spec', '2026-07-14 12:00:00')"
                )
            )

        result = await steps._migrate_repair_known_fixture_fk_orphans()
        assert result is None
        assert not fixture_graph_dir.exists()

        async with engine.connect() as conn:
            assert (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
            cards = dict(
                (await conn.execute(text("SELECT id, sprint_id FROM cards"))).all()
            )
            assert cards == {"real-card": "real-sprint"}
            history_ids = {
                row[0]
                for row in (
                    await conn.execute(text("SELECT id FROM sprint_history"))
                ).all()
            }
            dlq_ids = {
                row[0]
                for row in (
                    await conn.execute(text("SELECT id FROM consolidation_dead_letter"))
                ).all()
            }
            assert history_ids == {"real-history"}
            assert dlq_ids == {"real-dlq"}
            assert (
                await conn.execute(
                    text("SELECT COUNT(*) FROM boards WHERE id='sprint-crud-board-001'")
                )
            ).scalar_one() == 0

        assert await steps._migrate_repair_known_fixture_fk_orphans() == "skipped"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_fk_violation_aborts_without_partial_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _engine_with_minimal_schema(tmp_path / "unknown-drift.db")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO sprint_history VALUES "
                    "('known', 'missing-known', 'sprint-crud-agent-001', "
                    "'2026-07-02 12:00:00'), "
                    "('unknown', 'missing-unknown', 'production-agent', "
                    "'2026-07-14 12:00:00')"
                )
            )

        with pytest.raises(RuntimeError, match="unknown sprint-history orphan"):
            await steps._migrate_repair_known_fixture_fk_orphans()

        async with engine.connect() as conn:
            ids = {
                row[0]
                for row in (
                    await conn.execute(text("SELECT id FROM sprint_history"))
                ).all()
            }
            assert ids == {"known", "unknown"}
            assert len(
                (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            ) == 2
    finally:
        await engine.dispose()
