"""C8: governed Q&A mutations participate in the global source fence."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.board_source_reader import (
    _REQUIRED_SOURCE_COLUMNS,
    _REQUIRED_SOURCE_TABLES,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
)


_QA_BINDINGS = (
    ("ideation_qa_items", "ideation_id", "ideation-c8"),
    ("refinement_qa_items", "refinement_id", "refinement-c8"),
    ("spec_qa_items", "spec_id", "spec-c8"),
)

_NORMATIVE_QA_COLUMNS = frozenset(
    {
        "id",
        "question",
        "question_type",
        "choices",
        "allow_free_text",
        "answer",
        "selected",
        "answered_at",
        "revision",
        "lifecycle",
        "tombstoned",
    }
)


def _initialize_schema(database_path: Path) -> None:
    async def initialize() -> None:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        register_community_relational_schema_lifecycle()
        await database_module.init_db()
        await database_module.get_engine().dispose()

    asyncio.run(initialize())


def _read_fence(connection: sqlite3.Connection) -> tuple[int, str]:
    row = connection.execute(
        "SELECT revision, mutation_nonce "
        "FROM global_discovery_source_revision WHERE scope_id = '_global'"
    ).fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


def test_qa_tables_and_normative_columns_are_in_the_closed_source_census() -> None:
    assert GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION == (
        "gdsr-trigger-manifest-v5"
    )
    for table_name, parent_column, _parent_id in _QA_BINDINGS:
        assert table_name in GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES
        assert table_name in _REQUIRED_SOURCE_TABLES
        assert _REQUIRED_SOURCE_COLUMNS[table_name] == (
            _NORMATIVE_QA_COLUMNS | {parent_column}
        )


def test_each_qa_insert_update_delete_rotates_revision_and_nonce(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "gdsr-qa-mutations.sqlite3"
    _initialize_schema(database_path)

    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            "INSERT INTO boards (id, name, owner_id) VALUES (?, ?, ?)",
            ("board-c8", "C8 Q&A fence", "agent-c8"),
        )
        connection.execute(
            "INSERT INTO ideations "
            "(id, board_id, title, status, version, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "ideation-c8",
                "board-c8",
                "Ideation C8",
                "draft",
                1,
                "agent-c8",
            ),
        )
        connection.execute(
            "INSERT INTO refinements "
            "(id, ideation_id, board_id, title, status, version, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "refinement-c8",
                "ideation-c8",
                "board-c8",
                "Refinement C8",
                "draft",
                1,
                "agent-c8",
            ),
        )
        connection.execute(
            "INSERT INTO specs "
            "(id, board_id, ideation_id, refinement_id, title, status, "
            "version, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "spec-c8",
                "board-c8",
                "ideation-c8",
                "refinement-c8",
                "Spec C8",
                "draft",
                1,
                "agent-c8",
            ),
        )
        connection.commit()

        previous_revision, previous_nonce = _read_fence(connection)
        observed_nonces = {previous_nonce}
        mutation_count = 0

        for table_name, parent_column, parent_id in _QA_BINDINGS:
            qa_id = f"{table_name}-c8"
            connection.execute(
                f'INSERT INTO "{table_name}" '
                f'(id, "{parent_column}", question, asked_by) '
                "VALUES (?, ?, ?, ?)",
                (qa_id, parent_id, "What is the contract?", "agent-c8"),
            )
            connection.commit()
            revision, nonce = _read_fence(connection)
            mutation_count += 1
            assert revision == previous_revision + 1
            assert nonce not in observed_nonces
            previous_revision, previous_nonce = revision, nonce
            observed_nonces.add(nonce)

            connection.execute(
                f'UPDATE "{table_name}" '
                "SET answer = ?, answered_at = CURRENT_TIMESTAMP, revision = 2 "
                "WHERE id = ?",
                ("The governed answer.", qa_id),
            )
            connection.commit()
            revision, nonce = _read_fence(connection)
            mutation_count += 1
            assert revision == previous_revision + 1
            assert nonce not in observed_nonces
            previous_revision, previous_nonce = revision, nonce
            observed_nonces.add(nonce)

            connection.execute(
                f'DELETE FROM "{table_name}" WHERE id = ?',
                (qa_id,),
            )
            connection.commit()
            revision, nonce = _read_fence(connection)
            mutation_count += 1
            assert revision == previous_revision + 1
            assert nonce not in observed_nonces
            previous_revision, previous_nonce = revision, nonce
            observed_nonces.add(nonce)

        assert mutation_count == 9
        assert len(observed_nonces) == mutation_count + 1
    finally:
        connection.close()
