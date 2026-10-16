"""Native cognitive revision schema and scoped reader contracts."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from okto_pulse.community.adapters.sqlalchemy_base import Base
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.ext.asyncio import create_async_engine
from okto_pulse.community.adapters.board_source_reader import read_realm_cognitive_source_snapshot
from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION, KGCognitiveSourceRevision,
)
from okto_pulse.core.ports.kg_cognitive_source import canonical_cognitive_source_fingerprint
from okto_pulse.core.kg.rebuild_sources import cognitive_durable_digest_from_rows

def _fingerprint(
    *,
    board_id: str,
    node_id: str,
    payload: dict[str, object],
    evidence_refs: list[str],
) -> str:
    return canonical_cognitive_source_fingerprint(
        board_id=board_id,
        node_id=node_id,
        node_type="Decision",
        generation=0,
        payload=payload,
        evidence_refs=evidence_refs,
    )



def _initialize_schema(database_path: Path) -> None:
    async def initialize():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            await initialize_current_schema(engine, current_schema_contract())
        finally:
            await engine.dispose()
    asyncio.run(initialize())



def test_native_revision_model_has_exact_owned_contract() -> None:
    table = KGCognitiveSourceRevision.__table__
    assert table.name == "kg_cognitive_source_revisions"
    assert tuple(table.columns) == tuple(
        table.columns[name]
        for name in (
            "id",
            "cognitive_source_id",
            "source_revision",
            "record_fingerprint",
            "payload",
            "evidence_refs",
            "source_session_id",
            "committed_at",
        )
    )
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_kg_cognitive_source_revisions_positive_revision": ("source_revision >= 1"),
        "ck_kg_cognitive_source_revisions_fingerprint_length": (
            "length(record_fingerprint) = 64"
        ),
    }
    uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert uniques == {
        "uq_kg_cognitive_source_revisions_source_revision": (
            "cognitive_source_id",
            "source_revision",
        )
    }
    foreign_key = next(iter(table.foreign_key_constraints)).elements[0]
    assert foreign_key.target_fullname == "kg_cognitive_sources.id"
    assert foreign_key.ondelete == "RESTRICT"
    assert foreign_key.onupdate == "RESTRICT"
    assert {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
    } == {
        "idx_kg_cognitive_source_revisions_source_revision": (
            "cognitive_source_id",
            "source_revision",
        )
    }
    assert table.name in Base.metadata.tables
    assert table.name in GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES
    assert GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION == (
        "gdsr-trigger-manifest-v9"
    )



def test_native_guards_and_child_insert_advance_global_fence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cognitive-revision.sqlite3"
    _initialize_schema(database_path)

    _initialize_schema(database_path)  # Current restart never rewrites history.

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        expected_guards = {name: table for kind, name, table, _sql in current_schema_contract().objects
            if kind == "trigger" and name.startswith("trg_kg_cognitive_source_immutable_")}
        guard_rows = connection.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_kg_cognitive_source_immutable_%'"
        ).fetchall()
        assert {row["name"]: row["tbl_name"] for row in guard_rows} == expected_guards
        assert not any("fingerprint_epoch" in row["name"] for row in guard_rows)

        before = int(
            connection.execute(
                "SELECT revision FROM global_discovery_source_revision"
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO kg_cognitive_sources "
            "(id, board_id, node_id, node_type, generation, payload, "
            "evidence_refs, source_session_id, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                "source-1",
                "board-1",
                "decision-1",
                "Decision",
                0,
                json.dumps({"title": "v0"}),
                json.dumps(["spec:1"]),
                "session-1",
            ),
        )
        after_base = int(
            connection.execute(
                "SELECT revision FROM global_discovery_source_revision"
            ).fetchone()[0]
        )
        revision_payload = {"title": "v1"}
        revision_evidence = ["spec:2"]
        connection.execute(
            "INSERT INTO kg_cognitive_source_revisions "
            "(id, cognitive_source_id, source_revision, record_fingerprint, "
            "payload, evidence_refs, source_session_id, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                "revision-1",
                "source-1",
                1,
                _fingerprint(
                    board_id="board-1",
                    node_id="decision-1",
                    payload=revision_payload,
                    evidence_refs=revision_evidence,
                ),
                json.dumps(revision_payload),
                json.dumps(revision_evidence),
                "session-2",
            ),
        )
        after_child = int(
            connection.execute(
                "SELECT revision FROM global_discovery_source_revision"
            ).fetchone()[0]
        )
        connection.commit()
        assert after_base == before + 1
        assert after_child == after_base + 1

        for statement in (
            "UPDATE kg_cognitive_sources SET payload = '{}' WHERE id = 'source-1'",
            "DELETE FROM kg_cognitive_sources WHERE id = 'source-1'",
            "UPDATE kg_cognitive_source_revisions SET payload = '{}' "
            "WHERE id = 'revision-1'",
            "DELETE FROM kg_cognitive_source_revisions WHERE id = 'revision-1'",
            "UPDATE kg_cognitive_source_revisions SET record_fingerprint = '" + "0" * 64 + "' WHERE id = 'revision-1'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement)
            connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO kg_cognitive_source_revisions "
                "(id, cognitive_source_id, source_revision, record_fingerprint, "
                "payload, evidence_refs, committed_at) "
                "VALUES ('bad-revision', 'source-1', 0, ?, '{}', '[]', "
                "CURRENT_TIMESTAMP)",
                ("x" * 64,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO kg_cognitive_source_revisions "
                "(id, cognitive_source_id, source_revision, record_fingerprint, "
                "payload, evidence_refs, committed_at) "
                "VALUES ('bad-fk', 'missing', 1, ?, '{}', '[]', "
                "CURRENT_TIMESTAMP)",
                ("x" * 64,),
            )
        connection.rollback()
    finally:
        connection.close()



def _create_reader_fixture(
    database_path: Path,
    *,
    include_revision_table: bool,
) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE boards (
            id TEXT PRIMARY KEY,
            realm_id TEXT NOT NULL
        );
        CREATE TABLE kg_cognitive_sources (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            generation INTEGER NOT NULL,
            payload JSON NOT NULL,
            evidence_refs JSON NOT NULL,
            source_session_id TEXT,
            committed_at TEXT NOT NULL
        );
        INSERT INTO boards (id, realm_id) VALUES ('board-reader', 'realm-a');
        INSERT INTO boards (id, realm_id) VALUES ('board-other', 'realm-b');
        """
    )
    if include_revision_table:
        connection.executescript(
            """
            CREATE TABLE kg_cognitive_source_revisions (
                id TEXT PRIMARY KEY,
                cognitive_source_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                record_fingerprint TEXT,
                payload JSON NOT NULL,
                evidence_refs JSON NOT NULL,
                source_session_id TEXT,
                committed_at TEXT NOT NULL,
                UNIQUE (cognitive_source_id, source_revision)
            );
            """
        )
    return connection



def test_reader_returns_only_latest_child_revision_and_preserves_realm_scope(
    tmp_path: Path,
) -> None:
    connection = _create_reader_fixture(
        tmp_path / "latest-reader.sqlite3",
        include_revision_table=True,
    )
    connection.execute(
        "INSERT INTO kg_cognitive_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "source-reader",
            "board-reader",
            "decision-reader",
            "Decision",
            0,
            json.dumps({"title": "v0"}),
            json.dumps(["spec:0"]),
            "session-0",
            "2026-07-22T12:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO kg_cognitive_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "source-other",
            "board-other",
            "decision-other",
            "Decision",
            0,
            json.dumps({"title": "other"}),
            json.dumps([]),
            None,
            "2026-07-22T12:00:00+00:00",
        ),
    )
    fingerprints: dict[int, str] = {}
    for revision in (1, 2):
        payload = {"title": f"v{revision}"}
        evidence = [f"spec:{revision}"]
        fingerprints[revision] = _fingerprint(
            board_id="board-reader",
            node_id="decision-reader",
            payload=payload,
            evidence_refs=evidence,
        )
        connection.execute(
            "INSERT INTO kg_cognitive_source_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"revision-{revision}",
                "source-reader",
                revision,
                fingerprints[revision],
                json.dumps(payload),
                json.dumps(evidence),
                f"session-{revision}",
                f"2026-07-22T12:0{revision}:00+00:00",
            ),
        )
    try:
        rows = read_realm_cognitive_source_snapshot(
            connection,
            realm_id="realm-a",
        )
    finally:
        connection.close()

    assert set(rows) == {"board-reader"}
    assert len(rows["board-reader"]) == 1
    record = rows["board-reader"][0]
    assert record["board_id"] == "board-reader"
    assert record["source_revision"] == 2
    assert json.loads(record["payload"])["title"] == "v2"
    assert json.loads(record["evidence_refs"]) == ["spec:2"]
    assert record["source_session_id"] == "session-2"
    assert record["record_fingerprint"] == fingerprints[2]
    assert cognitive_durable_digest_from_rows(rows["board-reader"])["count"] == 1



@pytest.mark.parametrize("fingerprint", ["f" * 64, None, ""])
@pytest.mark.parametrize("newer_valid_revision", [False, True])
def test_reader_fails_closed_when_any_revision_fingerprint_is_tampered(
    tmp_path: Path, fingerprint, newer_valid_revision,
) -> None:
    connection = _create_reader_fixture(
        tmp_path / "tampered-reader.sqlite3",
        include_revision_table=True,
    )
    connection.execute(
        "INSERT INTO kg_cognitive_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "source-tampered",
            "board-reader",
            "decision-tampered",
            "Decision",
            0,
            json.dumps({"title": "v0"}),
            json.dumps(["spec:0"]),
            "session-0",
            "2026-07-22T12:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO kg_cognitive_source_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "revision-tampered",
            "source-tampered",
            1,
            fingerprint,
            json.dumps({"title": "tampered"}),
            json.dumps(["spec:1"]),
            "session-1",
            "2026-07-22T12:01:00+00:00",
        ),
    )
    if newer_valid_revision:
        payload = {"title": "valid latest"}
        evidence = ["spec:2"]
        connection.execute(
            "INSERT INTO kg_cognitive_source_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("revision-valid", "source-tampered", 2,
             _fingerprint(board_id="board-reader", node_id="decision-tampered",
                          payload=payload, evidence_refs=evidence),
             json.dumps(payload), json.dumps(evidence), "session-2",
             "2026-07-22T12:02:00+00:00"),
        )
    try:
        with pytest.raises(ValueError, match="record_fingerprint"):
            read_realm_cognitive_source_snapshot(
                connection,
                realm_id="realm-a",
            )
    finally:
        connection.close()



def test_reader_refuses_missing_revision_table_without_modifying_storage(tmp_path):
    path = tmp_path / "incompatible-reader.db"
    connection = _create_reader_fixture(path, include_revision_table=False)
    connection.commit()
    before = path.read_bytes()
    try:
        with pytest.raises(sqlite3.OperationalError, match="kg_cognitive_source_revisions"):
            read_realm_cognitive_source_snapshot(connection, realm_id="realm-a")
    finally:
        connection.close()
    assert path.read_bytes() == before
