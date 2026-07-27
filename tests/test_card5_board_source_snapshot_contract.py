"""Card 5: Community source snapshots prove completeness explicitly."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from okto_pulse.community.adapters import board_source_reader as reader_module
from okto_pulse.community.adapters.board_source_reader import (
    ARTIFACT_QUERIES,
    CommunityBoardSourceReader,
)
from okto_pulse.core.application.rebuild_ports import BoardSourceSnapshot
from okto_pulse.core.kg.board_source_store import (
    AMENDMENT_CONTENT_COLUMNS,
    CARD_CONTENT_COLUMNS,
)
from okto_pulse.core.kg.interfaces.board_source_reader import (
    SourceReadFailure,
    SourceUnavailableError,
)


def _source_database(
    path: Path,
    *,
    omit_table: str | None = None,
    board_exists: bool = True,
    realm_id: str | None = "realm-card5",
    include_realm_column: bool = True,
    omit_column: tuple[str, str] | None = None,
) -> Path:
    """Create the smallest structurally complete, empty source database."""

    with sqlite3.connect(path) as connection:
        board_columns = {"id", "settings"}
        if include_realm_column:
            board_columns.add("realm_id")
        if omit_column is not None and omit_column[0] == "boards":
            board_columns.remove(omit_column[1])
        board_declarations = [
            "id TEXT PRIMARY KEY" if column == "id" else f'"{column}" TEXT'
            for column in sorted(board_columns)
        ]
        connection.execute(f"CREATE TABLE boards ({', '.join(board_declarations)})")
        if board_exists and "id" in board_columns:
            if include_realm_column:
                connection.execute(
                    "INSERT INTO boards (id, settings, realm_id) VALUES (?, ?, ?)",
                    ("board-card5", "{}", realm_id),
                )
            else:
                connection.execute(
                    "INSERT INTO boards (id, settings) VALUES (?, ?)",
                    ("board-card5", "{}"),
                )

        required_columns = {
            table: {
                "id",
                "board_id",
                "created_at",
                status_col,
                *content_cols,
            }
            for _, table, status_col, content_cols in ARTIFACT_QUERIES
        }
        required_columns["cards"] = {
            "id",
            "board_id",
            "created_at",
            "status",
            *CARD_CONTENT_COLUMNS,
        }
        required_columns["amendment_hotfix_revisions"] = {
            "id",
            "board_id",
            "created_at",
            "status",
            *AMENDMENT_CONTENT_COLUMNS,
        }
        for table, columns in sorted(required_columns.items()):
            if table == omit_table:
                continue
            if omit_column is not None and table == omit_column[0]:
                columns.remove(omit_column[1])
            declarations = ["id TEXT PRIMARY KEY"]
            declarations.extend(
                f'"{column}" TEXT' for column in sorted(columns - {"id"})
            )
            connection.execute(f"CREATE TABLE {table} ({', '.join(declarations)})")
        connection.commit()
    return path


def test_missing_database_returns_an_explicit_incomplete_snapshot(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.sqlite3"

    snapshot = CommunityBoardSourceReader(missing).fetch("board-card5")

    assert isinstance(snapshot, BoardSourceSnapshot)
    assert snapshot.rows == ()
    with pytest.raises(SourceUnavailableError):
        list(snapshot)
    assert snapshot.complete is False
    assert snapshot.cause == "db_missing"
    assert not missing.exists(), "the read path must not create a missing database"


def test_missing_required_table_returns_incomplete_instead_of_partial_rows(
    tmp_path: Path,
) -> None:
    database = _source_database(
        tmp_path / "missing-specs.sqlite3",
        omit_table="specs",
    )

    snapshot = CommunityBoardSourceReader(database).fetch("board-card5")

    assert snapshot == BoardSourceSnapshot(
        rows=(),
        complete=False,
        cause="table_missing",
    )


@pytest.mark.parametrize(
    ("table", "column"),
    [
        pytest.param("boards", "id", id="board-identity"),
        pytest.param("stories", "status", id="artifact-status"),
        pytest.param("specs", "integration_requirements", id="artifact-content"),
        pytest.param("sprints", "created_at", id="artifact-common"),
        pytest.param("cards", "board_id", id="card-ownership"),
        pytest.param("cards", "card_type", id="card-content"),
        pytest.param(
            "amendment_hotfix_revisions",
            "lineage_state",
            id="amendment-content",
        ),
    ],
)
def test_empty_table_missing_a_required_column_makes_snapshot_incomplete(
    tmp_path: Path,
    table: str,
    column: str,
) -> None:
    database = _source_database(
        tmp_path / f"mutilated-{table}.sqlite3",
        omit_column=(table, column),
    )

    snapshot = CommunityBoardSourceReader(database).fetch("board-card5")

    assert snapshot == BoardSourceSnapshot(
        rows=(),
        complete=False,
        cause="realm_incomplete",
    )


def test_missing_board_makes_the_requested_realm_incomplete(tmp_path: Path) -> None:
    database = _source_database(
        tmp_path / "board-missing.sqlite3",
        board_exists=False,
    )

    snapshot = CommunityBoardSourceReader(database).fetch("board-card5")

    assert snapshot.rows == ()
    assert snapshot.complete is False
    assert snapshot.cause == "realm_incomplete"


def test_legacy_board_without_realm_column_can_be_complete_and_empty(
    tmp_path: Path,
) -> None:
    database = _source_database(
        tmp_path / "legacy-complete-empty.sqlite3",
        include_realm_column=False,
    )

    snapshot = CommunityBoardSourceReader(database).fetch("board-card5")

    assert snapshot.rows == ()
    assert snapshot.complete is True
    assert snapshot.cause is None


def test_proven_empty_realm_is_a_complete_empty_snapshot(tmp_path: Path) -> None:
    database = _source_database(tmp_path / "complete-empty.sqlite3")

    snapshot = CommunityBoardSourceReader(database).fetch("board-card5")

    assert snapshot.rows == ()
    assert snapshot.complete is True
    assert snapshot.cause is None


def test_database_open_errors_keep_the_typed_unavailable_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "exists.sqlite3"
    database.touch()

    def _cannot_open(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("injected open failure")

    monkeypatch.setattr(reader_module.sqlite3, "connect", _cannot_open)

    with pytest.raises(SourceUnavailableError) as caught:
        CommunityBoardSourceReader(database).fetch("board-card5")

    assert caught.value.code == "source_unavailable"
    assert caught.value.cause_type == "OperationalError"


def test_database_read_errors_keep_the_typed_failure_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _source_database(tmp_path / "read-failure.sqlite3")

    def _cannot_read(
        _self: CommunityBoardSourceReader,
        _connection: sqlite3.Connection,
        _board_id: str,
    ) -> BoardSourceSnapshot:
        raise sqlite3.DatabaseError("injected read failure")

    monkeypatch.setattr(CommunityBoardSourceReader, "_fetch_conn", _cannot_read)

    with pytest.raises(SourceReadFailure) as caught:
        CommunityBoardSourceReader(database).fetch("board-card5")

    assert caught.value.code == "read_error"
    assert caught.value.cause_type == "DatabaseError"
