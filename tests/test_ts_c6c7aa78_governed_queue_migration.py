"""TS14 — governed ConsolidationQueue migration and replay contract."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_add_consolidation_work_kinds,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    configure_community_database,
)


def _create_legacy_queue(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE boards (
                id VARCHAR(36) PRIMARY KEY
            );
            CREATE TABLE consolidation_queue (
                id VARCHAR(36) PRIMARY KEY,
                board_id VARCHAR(36) NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                artifact_type VARCHAR(50) NOT NULL,
                artifact_id VARCHAR(36) NOT NULL,
                priority VARCHAR(10) NOT NULL,
                source VARCHAR(50) NOT NULL,
                status VARCHAR(20) NOT NULL,
                triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                triggered_by_event VARCHAR(100),
                claimed_by_session_id VARCHAR(36),
                claimed_at TIMESTAMP,
                last_error TEXT,
                worker_id VARCHAR(64),
                claim_timeout_at TIMESTAMP,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TIMESTAMP,
                CONSTRAINT uq_queue_board_artifact
                    UNIQUE (board_id, artifact_type, artifact_id)
            );
            CREATE INDEX ix_consolidation_queue_board_id
                ON consolidation_queue(board_id);
            CREATE INDEX ix_consolidation_queue_status
                ON consolidation_queue(status);

            INSERT INTO boards(id) VALUES ('board-1');
            INSERT INTO consolidation_queue(
                id, board_id, artifact_type, artifact_id, priority, source,
                status, triggered_by_event, claimed_by_session_id, attempts,
                last_error
            ) VALUES
                ('q-pending', 'board-1', 'card', 'card-1', 'high',
                 'state_transition', 'pending', 'card.moved', NULL, 0, NULL),
                ('q-claimed', 'board-1', 'spec', 'spec-1', 'low',
                 'historical_backfill', 'claimed', 'spec.moved', 'session-1',
                 2, 'previous failure'),
                ('q-done', 'board-1', 'ideation', 'ideation-1', 'high',
                 'state_transition', 'done', NULL, NULL, 3, NULL);
            """
        )


async def _snapshot(engine) -> tuple[tuple[object, ...], ...]:
    async with engine.connect() as connection:
        columns = tuple(
            tuple(row)
            for row in (
                await connection.exec_driver_sql(
                    "PRAGMA table_info('consolidation_queue')"
                )
            ).all()
        )
        indexes = tuple(
            tuple(row)
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='consolidation_queue' "
                    "ORDER BY name"
                )
            ).all()
        )
        rows = tuple(
            tuple(row)
            for row in (
                await connection.exec_driver_sql(
                    "SELECT id, board_id, artifact_type, artifact_id, priority, "
                    "source, status, triggered_by_event, claimed_by_session_id, "
                    "attempts, last_error, work_kind, generation, payload, "
                    "delete_event_id "
                    "FROM consolidation_queue ORDER BY id"
                )
            ).all()
        )
        table_sql = str(
            (
                await connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='consolidation_queue'"
                )
            ).scalar_one()
        )
    return columns, indexes, rows, ((table_sql,),)


def _expect_integrity_error(path: Path, statement: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(path) as connection:
            connection.execute(statement)


def test_ts_c6c7aa78_migration_backfills_replays_and_enforces_kind_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-queue.db"
    _create_legacy_queue(database_path)

    async def drive():
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        first_result = await _migrate_add_consolidation_work_kinds()
        first_snapshot = await _snapshot(runtime.engine)
        second_result = await _migrate_add_consolidation_work_kinds()
        second_snapshot = await _snapshot(runtime.engine)
        await runtime.close()
        return first_result, first_snapshot, second_result, second_snapshot

    first_result, first_snapshot, second_result, second_snapshot = asyncio.run(drive())

    assert first_result is None
    assert second_result == "skipped"
    assert second_snapshot == first_snapshot

    columns, indexes, rows, table_sql = first_snapshot
    column_names = {str(row[1]) for row in columns}
    assert {
        "work_kind",
        "generation",
        "payload",
        "delete_event_id",
        "claim_token",
    }.issubset(column_names)
    assert {str(row[-4]) for row in rows} == {"consolidate"}
    assert {int(row[-3]) for row in rows} == {0}
    assert {row[-1] for row in rows} == {None}
    assert {row[0] for row in rows} == {"q-pending", "q-claimed", "q-done"}
    assert next(row for row in rows if row[0] == "q-claimed")[8:11] == (
        "session-1",
        2,
        "previous failure",
    )

    index_sql = {str(row[0]): str(row[1] or "") for row in indexes}
    assert {
        "uq_queue_consolidate_board_artifact",
        "uq_queue_stale_reconcile_generation",
        "uq_queue_stale_sweep_board",
        "ix_queue_drain_work",
        "ix_consolidation_queue_delete_event_id",
    }.issubset(index_sql)
    assert "uq_queue_board_artifact" not in str(table_sql)

    # Legacy consolidate dedupe remains exact.
    _expect_integrity_error(
        database_path,
        "INSERT INTO consolidation_queue "
        "(id,board_id,artifact_type,artifact_id,priority,source,status) VALUES "
        "('duplicate','board-1','card','card-1','high','test','pending')",
    )

    # A claimed stale generation may coexist with both the consolidate row and
    # a later immutable generation; only an exact generation replay dedupes.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO consolidation_queue "
            "(id,board_id,artifact_type,artifact_id,priority,source,status,"
            "work_kind,generation,payload) VALUES "
            "('reconcile-g1','board-1','spec','spec-1','high','delete','claimed',"
            "'stale_reconcile',1,'{\"source_refs\":[\"spec:spec-1\"]}')"
        )
        connection.execute(
            "INSERT INTO consolidation_queue "
            "(id,board_id,artifact_type,artifact_id,priority,source,status,"
            "work_kind,generation) VALUES "
            "('reconcile-g2','board-1','spec','spec-1','high','delete','pending',"
            "'stale_reconcile',2)"
        )

    _expect_integrity_error(
        database_path,
        "INSERT INTO consolidation_queue "
        "(id,board_id,artifact_type,artifact_id,priority,source,status,"
        "work_kind,generation) VALUES "
        "('reconcile-g1-copy','board-1','spec','spec-1','high','delete','pending',"
        "'stale_reconcile',1)",
    )

    # Sweep identity is board-scoped regardless of payload/cursor.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO consolidation_queue "
            "(id,board_id,artifact_type,artifact_id,priority,source,status,"
            "work_kind,generation,payload) VALUES "
            "('sweep-1','board-1','board','board-1','low','tick','pending',"
            "'stale_sweep',0,'{\"cursor\":\"\",\"budget\":100,\"attempt\":0}')"
        )
    _expect_integrity_error(
        database_path,
        "INSERT INTO consolidation_queue "
        "(id,board_id,artifact_type,artifact_id,priority,source,status,"
        "work_kind,generation) VALUES "
        "('sweep-2','board-1','board','other','low','tick','pending',"
        "'stale_sweep',0)",
    )

    _expect_integrity_error(
        database_path,
        "INSERT INTO consolidation_queue "
        "(id,board_id,artifact_type,artifact_id,priority,source,status,work_kind) "
        "VALUES ('invalid-kind','board-1','card','other','high','test','pending',"
        "'unknown')",
    )
