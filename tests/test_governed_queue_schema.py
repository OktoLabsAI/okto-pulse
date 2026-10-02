"""TS14 — native governed queue identity and restart contract."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from test_skb3_semantic_guideline_persistence import _sqlite_engine


async def _create_native_queue(engine):
    await initialize_current_schema(engine, current_schema_contract())
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES ('board-1', 'Board', 'owner', 'local')"
        )
        await connection.exec_driver_sql("""
            INSERT INTO consolidation_queue (
                id, board_id, artifact_type, artifact_id, priority, source,
                status, triggered_by_event, claimed_by_session_id, attempts, last_error
            ) VALUES
                ('q-pending', 'board-1', 'card', 'card-1', 'high',
                 'state_transition', 'pending', 'card.moved', NULL, 0, NULL),
                ('q-claimed', 'board-1', 'spec', 'spec-1', 'low',
                 'state_transition', 'claimed', 'spec.moved', 'session-1', 2, 'previous failure'),
                ('q-done', 'board-1', 'ideation', 'ideation-1', 'high',
                 'state_transition', 'done', NULL, NULL, 3, NULL)
        """)


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


def test_ts_c6c7aa78_native_queue_preserves_restart_and_kind_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "native-queue.db"

    async def drive():
        engine = _sqlite_engine(database_path)
        await _create_native_queue(engine)
        first = await _snapshot(engine)
        await engine.dispose()
        restarted = _sqlite_engine(database_path)
        await initialize_current_schema(restarted, current_schema_contract())
        second = await _snapshot(restarted)
        await restarted.dispose()
        return first, second

    first_snapshot, second_snapshot = asyncio.run(drive())
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

    # Native consolidate identity remains exact.
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
            '\'stale_sweep\',0,\'{"cursor":"","budget":100,"attempt":0}\')'
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
