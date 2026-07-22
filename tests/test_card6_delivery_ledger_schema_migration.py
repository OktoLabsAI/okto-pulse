"""Card 6 — GD delivery-ledger schema and outbox-key migration."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_global_discovery_delivery_contract,
    _migrate_global_discovery_recovery_control_plane,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    configure_community_database,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base


_BOARD_ID = "11111111-1111-1111-1111-111111111111"
_ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"
_DELIVERY_KEY = f"gd_parity:{_BOARD_ID}:spec:{_ARTIFACT_ID}:7"
_ATTEMPT_KEY = f"{_DELIVERY_KEY}:attempt:0"


def test_card7_delivery_maintenance_control_schema_migrates_and_reads_back(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "delivery-redrive-control.db"

    async def drive():
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        async with runtime.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        migration_result = await _migrate_global_discovery_delivery_contract()
        async with runtime.engine.begin() as connection:
            columns = {
                str(row[1])
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA table_info("
                        "'global_discovery_delivery_redrive_control')"
                    )
                ).all()
            }
            table_sql = str(
                (
                    await connection.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name="
                        "'global_discovery_delivery_redrive_control'"
                    )
                ).scalar_one()
            )
            watchdog_columns = {
                str(row[1])
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA table_info("
                        "'global_discovery_delivery_watchdog_control')"
                    )
                ).all()
            }
            watchdog_table_sql = str(
                (
                    await connection.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name="
                        "'global_discovery_delivery_watchdog_control'"
                    )
                ).scalar_one()
            )
            watchdog_foreign_keys = tuple(
                tuple(row)
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA foreign_key_list("
                        "'global_discovery_delivery_watchdog_control')"
                    )
                ).all()
            )
            await connection.exec_driver_sql(
                "INSERT INTO global_discovery_delivery_redrive_control "
                "(id,cursor_board_id,cursor_oldest_at,cursor_delivery_key,"
                "checkpoint_version) VALUES "
                "('_global','board-b','2026-07-21 12:00:00',"
                "'gd_parity:cursor',7)"
            )
            row = tuple(
                (
                    await connection.exec_driver_sql(
                        "SELECT id,cursor_board_id,cursor_oldest_at,"
                        "cursor_delivery_key,checkpoint_version "
                        "FROM global_discovery_delivery_redrive_control"
                    )
                ).one()
            )
            await connection.exec_driver_sql(
                "INSERT INTO boards(id,name,owner_id) VALUES "
                "('watchdog-board','Watchdog board','tester')"
            )
            await connection.exec_driver_sql(
                "INSERT INTO global_discovery_delivery_watchdog_control "
                "(board_id,cursor_updated_at,cursor_delivery_key,"
                "checkpoint_version) VALUES "
                "('watchdog-board','2026-07-21 12:01:00',"
                "'gd_parity:watchdog-cursor',9)"
            )
            watchdog_row = tuple(
                (
                    await connection.exec_driver_sql(
                        "SELECT board_id,cursor_updated_at,"
                        "cursor_delivery_key,checkpoint_version "
                        "FROM global_discovery_delivery_watchdog_control"
                    )
                ).one()
            )
        with pytest.raises(IntegrityError):
            async with runtime.engine.begin() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO global_discovery_delivery_redrive_control "
                    "(id,checkpoint_version) VALUES ('not-global',0)"
                )
        with pytest.raises(IntegrityError):
            async with runtime.engine.begin() as connection:
                await connection.exec_driver_sql(
                    "UPDATE global_discovery_delivery_redrive_control "
                    "SET checkpoint_version=-1 WHERE id='_global'"
                )
        with pytest.raises(IntegrityError):
            async with runtime.engine.begin() as connection:
                await connection.exec_driver_sql(
                    "UPDATE global_discovery_delivery_watchdog_control "
                    "SET checkpoint_version=-1 "
                    "WHERE board_id='watchdog-board'"
                )
        with pytest.raises(IntegrityError):
            async with runtime.engine.begin() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO global_discovery_delivery_watchdog_control "
                    "(board_id,checkpoint_version) VALUES ('missing-board',0)"
                )
        async with runtime.engine.begin() as connection:
            await connection.exec_driver_sql(
                "DELETE FROM boards WHERE id='watchdog-board'"
            )
            watchdog_rows_after_cascade = int(
                (
                    await connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM "
                        "global_discovery_delivery_watchdog_control"
                    )
                ).scalar_one()
            )
        await runtime.close()
        return (
            migration_result,
            columns,
            table_sql,
            row,
            watchdog_columns,
            watchdog_table_sql,
            watchdog_foreign_keys,
            watchdog_row,
            watchdog_rows_after_cascade,
        )

    (
        migration_result,
        columns,
        table_sql,
        row,
        watchdog_columns,
        watchdog_table_sql,
        watchdog_foreign_keys,
        watchdog_row,
        watchdog_rows_after_cascade,
    ) = asyncio.run(drive())
    assert migration_result == "skipped"
    assert columns == {
        "id",
        "cursor_board_id",
        "cursor_oldest_at",
        "cursor_delivery_key",
        "checkpoint_version",
        "updated_at",
    }
    normalized = "".join(table_sql.lower().split())
    assert "check(id='_global')" in normalized
    assert "check(checkpoint_version>=0)" in normalized
    assert row == (
        "_global",
        "board-b",
        "2026-07-21 12:00:00",
        "gd_parity:cursor",
        7,
    )
    assert watchdog_columns == {
        "board_id",
        "cursor_updated_at",
        "cursor_delivery_key",
        "checkpoint_version",
        "updated_at",
    }
    normalized_watchdog = "".join(watchdog_table_sql.lower().split())
    assert "check(checkpoint_version>=0)" in normalized_watchdog
    assert any(
        str(foreign_key[2]) == "boards"
        and str(foreign_key[3]) == "board_id"
        and str(foreign_key[4]) == "id"
        and str(foreign_key[6]).upper() == "CASCADE"
        for foreign_key in watchdog_foreign_keys
    )
    assert watchdog_row == (
        "watchdog-board",
        "2026-07-21 12:01:00",
        "gd_parity:watchdog-cursor",
        9,
    )
    assert watchdog_rows_after_cascade == 0


async def _replace_outbox_with_legacy_contract(engine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql("DROP TABLE global_update_outbox")
        await connection.exec_driver_sql(
            """
            CREATE TABLE global_update_outbox (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                event_id VARCHAR(36) NOT NULL UNIQUE,
                board_id VARCHAR(36) NOT NULL,
                session_id VARCHAR(36) NOT NULL,
                event_type VARCHAR(50) NOT NULL,
                payload JSON NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                processed_at DATETIME,
                retry_count INTEGER NOT NULL,
                last_error TEXT
            )
            """
        )
        await connection.exec_driver_sql(
            "CREATE INDEX ix_global_update_outbox_board_id "
            "ON global_update_outbox(board_id)"
        )
        await connection.exec_driver_sql(
            "CREATE INDEX ix_global_update_outbox_processed_at "
            "ON global_update_outbox(processed_at)"
        )
        await connection.exec_driver_sql(
            """
            INSERT INTO global_update_outbox(
                id, event_id, board_id, session_id, event_type, payload,
                processed_at, retry_count, last_error
            ) VALUES
                ('row-pending', 'event-pending', 'board-a', 'session-a',
                 'consolidation_committed', '{"nodes_added":0}', NULL, 0, NULL),
                ('row-processed', 'event-processed', 'board-b', 'session-b',
                 'consolidation_committed', '{"nodes_added":0}',
                 '2026-07-21 12:00:00', 2, NULL),
                ('row-dlq', 'event-dlq', 'board-c', 'session-c',
                 'consolidation_committed', '{"nodes_added":0}', NULL, -1,
                 'terminal failure')
            """
        )


async def _snapshot(engine) -> tuple[object, ...]:
    async with engine.connect() as connection:
        outbox_columns = tuple(
            tuple(row)
            for row in (
                await connection.exec_driver_sql(
                    "PRAGMA table_info('global_update_outbox')"
                )
            ).all()
        )
        outbox_rows = tuple(
            tuple(row)
            for row in (
                await connection.exec_driver_sql(
                    "SELECT id, event_id, board_id, session_id, event_type, "
                    "payload, processed_at, retry_count, last_error "
                    "FROM global_update_outbox ORDER BY id"
                )
            ).all()
        )
        ledger_columns = tuple(
            tuple(row)
            for row in (
                await connection.exec_driver_sql(
                    "PRAGMA table_info('global_discovery_delivery_ledger')"
                )
            ).all()
        )
        ledger_indexes = tuple(
            tuple(row)
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='index' "
                    "AND tbl_name='global_discovery_delivery_ledger' "
                    "ORDER BY name"
                )
            ).all()
        )
        ledger_sql = str(
            (
                await connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='global_discovery_delivery_ledger'"
                )
            ).scalar_one()
        )
    return outbox_columns, outbox_rows, ledger_columns, ledger_indexes, ledger_sql


def test_card6_delivery_schema_migrates_without_data_loss_and_replays(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "delivery-contract.db"

    async def drive():
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        await _replace_outbox_with_legacy_contract(runtime.engine)
        first_result = await _migrate_global_discovery_delivery_contract()
        async with runtime.engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO global_update_outbox "
                "(id,event_id,board_id,session_id,event_type,payload,retry_count) "
                "VALUES ('row-long-key', ?, 'board-d', 'session-d', "
                "'consolidation_committed', '{\"nodes_added\":0}', 0)",
                (_ATTEMPT_KEY,),
            )
        first_snapshot = await _snapshot(runtime.engine)
        second_result = await _migrate_global_discovery_delivery_contract()
        second_snapshot = await _snapshot(runtime.engine)
        await runtime.close()
        return first_result, first_snapshot, second_result, second_snapshot

    first_result, first_snapshot, second_result, second_snapshot = asyncio.run(
        drive()
    )

    assert first_result is None
    assert second_result == "skipped"
    assert second_snapshot == first_snapshot

    outbox_columns, outbox_rows, ledger_columns, ledger_indexes, ledger_sql = (
        first_snapshot
    )
    event_id_column = next(row for row in outbox_columns if row[1] == "event_id")
    assert event_id_column[2].upper() == "VARCHAR(255)"
    assert len(_ATTEMPT_KEY) > 36
    assert next(row for row in outbox_rows if row[0] == "row-long-key")[1] == (
        _ATTEMPT_KEY
    )
    assert {row[0] for row in outbox_rows} == {
        "row-pending",
        "row-processed",
        "row-dlq",
        "row-long-key",
    }
    assert next(row for row in outbox_rows if row[0] == "row-processed")[-3:] == (
        "2026-07-21 12:00:00",
        2,
        None,
    )
    assert next(row for row in outbox_rows if row[0] == "row-dlq")[-2:] == (
        -1,
        "terminal failure",
    )

    assert {str(row[1]) for row in ledger_columns} == {
        "delivery_key",
        "board_id",
        "artifact_type",
        "artifact_id",
        "generation",
        "delete_event_id",
        "state",
        "attempt",
        "attempt_event_key",
        "last_error",
        "next_retry_at",
        "created_at",
        "updated_at",
        "delivered_at",
    }
    index_names = {str(row[0]) for row in ledger_indexes}
    assert {
        "ix_gd_delivery_ledger_state_retry",
        "ix_gd_delivery_ledger_board_state",
    }.issubset(index_names)
    normalized_ledger_sql = "".join(ledger_sql.lower().split())
    assert "check(generation>=1)" in normalized_ledger_sql
    assert "check(attempt>=0)" in normalized_ledger_sql
    assert "'outbox_persisted'" in normalized_ledger_sql
    assert "attempt_event_keyisnotnull" in normalized_ledger_sql


def test_card6_copy_failure_rolls_back_and_retry_converges_without_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "delivery-copy-retry.db"

    async def drive():
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        await _replace_outbox_with_legacy_contract(runtime.engine)
        assert await _migrate_global_discovery_recovery_control_plane() is None
        before = await _snapshot(runtime.engine)

        async def rebuild_state():
            async with runtime.engine.connect() as connection:
                tables = tuple(
                    (
                        await connection.exec_driver_sql(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name LIKE 'global_update_outbox%' ORDER BY name"
                        )
                    ).scalars()
                )
                triggers = tuple(
                    tuple(row)
                    for row in (
                        await connection.exec_driver_sql(
                            "SELECT name, tbl_name FROM sqlite_master "
                            "WHERE type='trigger' "
                            "AND name LIKE "
                            "'trg_global_discovery_source_revision_"
                            "global_update_outbox_%' ORDER BY name"
                        )
                    ).all()
                )
            return tables, triggers

        before_state = await rebuild_state()
        injected = False

        def fail_copy_once(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            nonlocal injected
            if (
                not injected
                and 'INSERT INTO "global_update_outbox"' in statement
                and 'FROM "global_update_outbox_delivery_key_legacy"'
                in statement
            ):
                injected = True
                raise sqlite3.OperationalError("card6_transient_copy_failure")

        event.listen(
            runtime.engine.sync_engine,
            "before_cursor_execute",
            fail_copy_once,
        )
        try:
            with pytest.raises(
                sqlite3.OperationalError,
                match="card6_transient_copy_failure",
            ):
                await _migrate_global_discovery_delivery_contract()
        finally:
            event.remove(
                runtime.engine.sync_engine,
                "before_cursor_execute",
                fail_copy_once,
            )

        after_failure = await _snapshot(runtime.engine)
        failure_state = await rebuild_state()
        retry_result = await _migrate_global_discovery_delivery_contract()
        after_retry = await _snapshot(runtime.engine)
        retry_state = await rebuild_state()
        recovery_result = await _migrate_global_discovery_recovery_control_plane()
        recovered_state = await rebuild_state()
        replay_result = await _migrate_global_discovery_delivery_contract()
        await runtime.close()
        return (
            before,
            before_state,
            after_failure,
            failure_state,
            retry_result,
            after_retry,
            retry_state,
            recovery_result,
            recovered_state,
            replay_result,
        )

    (
        before,
        before_state,
        after_failure,
        failure_state,
        retry_result,
        after_retry,
        retry_state,
        recovery_result,
        recovered_state,
        replay_result,
    ) = asyncio.run(drive())

    # The forced physical transaction restores the pre-upgrade table,
    # source-revision triggers, schema declaration, and every row.
    assert after_failure == before
    assert failure_state == before_state
    assert failure_state[0] == ("global_update_outbox",)
    assert len(failure_state[1]) == 3

    assert retry_result is None
    assert retry_state[0] == ("global_update_outbox",)
    assert retry_state[1] == ()
    assert before[1] == after_retry[1]
    event_id_column = next(row for row in after_retry[0] if row[1] == "event_id")
    assert event_id_column[2].upper() == "VARCHAR(255)"

    # The immediately-following recovery step restores the governed trigger
    # manifest; both migrations are terminally idempotent after recovery.
    assert recovery_result is None
    assert len(recovered_state[1]) == 3
    assert replay_result == "skipped"


def test_card6_resumes_legacy_split_table_state_without_loss(tmp_path: Path) -> None:
    database_path = tmp_path / "delivery-resume-backup.db"

    async def drive():
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        await _replace_outbox_with_legacy_contract(runtime.engine)
        legacy = await _snapshot(runtime.engine)

        # Reproduce the durable state left by the old non-transactional DDL
        # implementation when it failed immediately before/during row copy.
        async with runtime.engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE global_update_outbox RENAME TO "
                "global_update_outbox_delivery_key_legacy"
            )
            await connection.exec_driver_sql(
                "DROP INDEX ix_global_update_outbox_board_id"
            )
            await connection.exec_driver_sql(
                "DROP INDEX ix_global_update_outbox_processed_at"
            )
            await connection.run_sync(
                lambda sync_connection: Base.metadata.tables[
                    "global_update_outbox"
                ].create(sync_connection, checkfirst=False)
            )

        result = await _migrate_global_discovery_delivery_contract()
        converged = await _snapshot(runtime.engine)
        async with runtime.engine.connect() as connection:
            table_names = tuple(
                (
                    await connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'global_update_outbox%' ORDER BY name"
                    )
                ).scalars()
            )
        replay = await _migrate_global_discovery_delivery_contract()
        await runtime.close()
        return legacy, result, converged, table_names, replay

    legacy, result, converged, table_names, replay = asyncio.run(drive())

    assert result is None
    assert replay == "skipped"
    assert table_names == ("global_update_outbox",)
    assert converged[1] == legacy[1]
    event_id_column = next(row for row in converged[0] if row[1] == "event_id")
    assert event_id_column[2].upper() == "VARCHAR(255)"


@pytest.mark.parametrize(
    ("drift", "expected_section"),
    [
        ("outbox_columns_pk_default", "columns"),
        ("outbox_unique", "unique_constraints"),
        ("outbox_index", "indexes"),
        ("ledger_columns_default", "columns"),
        ("ledger_relational", "unique_constraints"),
        ("ledger_index", "indexes"),
    ],
)
def test_card6_migration_rejects_complete_physical_contract_drift(
    tmp_path: Path,
    drift: str,
    expected_section: str,
) -> None:
    database_path = tmp_path / f"delivery-drift-{drift}.db"

    async def drive() -> str:
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        table_name = (
            "global_update_outbox"
            if drift.startswith("outbox_")
            else "global_discovery_delivery_ledger"
        )
        try:
            async with runtime.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                table_sql = str(
                    (
                        await connection.exec_driver_sql(
                            "SELECT sql FROM sqlite_master WHERE type='table' "
                            "AND name=?",
                            (table_name,),
                        )
                    ).scalar_one()
                )
                indexes = {
                    str(row[0]): str(row[1])
                    for row in (
                        await connection.exec_driver_sql(
                            "SELECT name, sql FROM sqlite_master "
                            "WHERE type='index' AND tbl_name=? "
                            "AND sql IS NOT NULL",
                            (table_name,),
                        )
                    ).all()
                }
                original_table_sql = table_sql
                original_indexes = dict(indexes)

                if drift == "outbox_columns_pk_default":
                    table_sql = table_sql.replace(
                        "id VARCHAR(36) NOT NULL",
                        "id VARCHAR(36)",
                    ).replace(
                        "board_id VARCHAR(36) NOT NULL, \n\tsession_id "
                        "VARCHAR(36) NOT NULL",
                        "session_id VARCHAR(36) NOT NULL, \n\tboard_id "
                        "INTEGER NOT NULL",
                    ).replace(
                        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL",
                        "created_at DATETIME NOT NULL",
                    ).replace(
                        "PRIMARY KEY (id), \n\tUNIQUE (event_id)",
                        "UNIQUE (event_id)",
                    )
                elif drift == "outbox_unique":
                    table_sql = table_sql.replace(
                        "PRIMARY KEY (id), \n\tUNIQUE (event_id)",
                        "PRIMARY KEY (id)",
                    )
                elif drift == "outbox_index":
                    index_name = "ix_global_update_outbox_board_id"
                    indexes[index_name] = indexes[index_name].replace(
                        "CREATE INDEX",
                        "CREATE UNIQUE INDEX",
                        1,
                    )
                elif drift == "ledger_columns_default":
                    table_sql = table_sql.replace(
                        "artifact_type VARCHAR(50) NOT NULL",
                        "artifact_type INTEGER NOT NULL",
                    ).replace(
                        "state VARCHAR(32) NOT NULL",
                        "state VARCHAR(32)",
                    ).replace(
                        "attempt INTEGER DEFAULT '0' NOT NULL",
                        "attempt INTEGER DEFAULT '1' NOT NULL",
                    )
                elif drift == "ledger_relational":
                    table_sql = table_sql.replace(
                        "CONSTRAINT uq_gd_delivery_ledger_artifact_generation "
                        "UNIQUE (board_id, artifact_type, artifact_id, generation)",
                        "CONSTRAINT uq_gd_delivery_ledger_artifact_generation_wrong "
                        "UNIQUE (board_id, artifact_type, generation, artifact_id)",
                    ).replace(
                        "CONSTRAINT ck_gd_delivery_ledger_attempt "
                        "CHECK (attempt >= 0)",
                        "CONSTRAINT ck_gd_delivery_ledger_attempt "
                        "CHECK (attempt >= -1)",
                    ).replace(
                        "REFERENCES boards (id) ON DELETE CASCADE",
                        "REFERENCES boards (name) ON DELETE CASCADE",
                    )
                elif drift == "ledger_index":
                    index_name = "ix_gd_delivery_ledger_state_retry"
                    indexes[index_name] = indexes[index_name].replace(
                        "CREATE INDEX",
                        "CREATE UNIQUE INDEX",
                        1,
                    ).replace(
                        "(state, next_retry_at, updated_at, delivery_key)",
                        "(next_retry_at, state, updated_at, delivery_key)",
                    )
                else:  # pragma: no cover - closed parametrization
                    raise AssertionError(drift)

                assert (
                    table_sql != original_table_sql
                    or indexes != original_indexes
                )
                await connection.exec_driver_sql(f'DROP TABLE "{table_name}"')
                await connection.exec_driver_sql(table_sql)
                for index_sql in indexes.values():
                    await connection.exec_driver_sql(index_sql)

            with pytest.raises(RuntimeError) as error:
                await _migrate_global_discovery_delivery_contract()
            return str(error.value)
        finally:
            await runtime.close()

    message = asyncio.run(drive())
    assert "physical contract drift" in message
    assert expected_section in message


def test_card6_delivery_ledger_constraints_are_fail_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "delivery-constraints.db"

    async def create_schema() -> None:
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        async with runtime.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        assert await _migrate_global_discovery_delivery_contract() == "skipped"
        await runtime.close()

    asyncio.run(create_schema())

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO boards(id,name,owner_id) VALUES (?,?,?)",
            (_BOARD_ID, "Board", "owner"),
        )
        connection.execute(
            "INSERT INTO global_discovery_delivery_ledger "
            "(delivery_key,board_id,artifact_type,artifact_id,generation,"
            "delete_event_id,state,attempt,attempt_event_key) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                _DELIVERY_KEY,
                _BOARD_ID,
                "spec",
                _ARTIFACT_ID,
                7,
                "delete-7",
                "outbox_persisted",
                0,
                _ATTEMPT_KEY,
            ),
        )

    invalid_rows = (
        (
            "gd_parity:invalid-null-key",
            "delete-null-key",
            "outbox_persisted",
            0,
            None,
        ),
        (
            "gd_parity:invalid-attempt",
            "delete-negative-attempt",
            "delivery_debt",
            -1,
            None,
        ),
        (
            "gd_parity:invalid-state",
            "delete-invalid-state",
            "unknown",
            0,
            None,
        ),
    )
    for delivery_key, delete_event_id, state, attempt, attempt_key in invalid_rows:
        with pytest.raises(sqlite3.IntegrityError):
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "INSERT INTO global_discovery_delivery_ledger "
                    "(delivery_key,board_id,artifact_type,artifact_id,generation,"
                    "delete_event_id,state,attempt,attempt_event_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        delivery_key,
                        _BOARD_ID,
                        "card",
                        delivery_key,
                        1,
                        delete_event_id,
                        state,
                        attempt,
                        attempt_key,
                    ),
                )
