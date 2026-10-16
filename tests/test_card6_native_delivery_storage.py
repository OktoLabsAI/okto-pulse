"""Native Global Discovery delivery ledger and recovery control storage."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.current_relational_schema import (
    StorageFormatError, current_schema_contract, initialize_current_schema,
)
from test_current_relational_schema import snapshot
from okto_pulse.community.adapters.sqlalchemy_database import (
    configure_community_database,
)


_BOARD_ID = "11111111-1111-1111-1111-111111111111"
_ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"
_DELIVERY_KEY = f"gd_parity:{_BOARD_ID}:spec:{_ARTIFACT_ID}:7"
_ATTEMPT_KEY = f"{_DELIVERY_KEY}:attempt:0"


def test_card7_native_delivery_maintenance_control_roundtrip(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "delivery-redrive-control.db"

    async def drive():
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        await initialize_current_schema(runtime.engine, current_schema_contract())
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
                "INSERT INTO boards(id,name,owner_id,realm_id) VALUES "
                "('watchdog-board','Watchdog board','tester','local')"
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
        await runtime.engine.dispose()
        await initialize_current_schema(runtime.engine, current_schema_contract())
        async with runtime.engine.connect() as connection:
            assert tuple((await connection.exec_driver_sql(
                "SELECT id,cursor_board_id,cursor_oldest_at,cursor_delivery_key,"
                "checkpoint_version FROM global_discovery_delivery_redrive_control"
            )).one()) == row
            assert tuple((await connection.exec_driver_sql(
                "SELECT board_id,cursor_updated_at,cursor_delivery_key,checkpoint_version "
                "FROM global_discovery_delivery_watchdog_control"
            )).one()) == watchdog_row
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
        columns,
        table_sql,
        row,
        watchdog_columns,
        watchdog_table_sql,
        watchdog_foreign_keys,
        watchdog_row,
        watchdog_rows_after_cascade,
    ) = asyncio.run(drive())
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












@pytest.mark.parametrize(
    "drift",
    [
        "outbox_columns_pk_default",
        "outbox_unique",
        "outbox_index",
        "ledger_columns_default",
        "ledger_relational",
        "ledger_index",
    ],
)
def test_card6_current_schema_refuses_physical_contract_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    database_path = tmp_path / f"delivery-drift-{drift}.db"

    async def drive() -> None:
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        table_name = (
            "global_update_outbox"
            if drift.startswith("outbox_")
            else "global_discovery_delivery_ledger"
        )
        try:
            await initialize_current_schema(runtime.engine, current_schema_contract())
            async with runtime.engine.begin() as connection:
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

            await runtime.engine.dispose()
            before = snapshot(database_path)
            with pytest.raises(StorageFormatError):
                await initialize_current_schema(runtime.engine, current_schema_contract())
            assert snapshot(database_path) == before
        finally:
            await runtime.close()

    asyncio.run(drive())


def test_card6_delivery_ledger_constraints_are_fail_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "delivery-constraints.db"

    async def create_schema() -> None:
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        await initialize_current_schema(runtime.engine, current_schema_contract())
        await runtime.close()

    asyncio.run(create_schema())

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO boards(id,name,owner_id,realm_id) VALUES (?,?,?,?)",
            (_BOARD_ID, "Board", "owner", "local"),
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
