"""AF-07 Community reader consumes core-owned source contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from okto_pulse.community.adapters.board_source_reader import (
    ARTIFACT_QUERIES,
    CommunityBoardSourceReader,
    _REQUIRED_SOURCE_COLUMNS,
)
from okto_pulse.core.kg.interfaces.board_source_reader import SourceUnavailableError
from okto_pulse.core.kg.board_source_store import (
    IDEATION_CONTENT_COLUMNS,
    REFINEMENT_CONTENT_COLUMNS,
    SPEC_CONTENT_COLUMNS_V2,
    SPRINT_CONTENT_COLUMNS,
    STORY_CONTENT_COLUMNS,
    _canonical_content_hash,
)
from okto_pulse.core.ports.relational_runtime import (
    configure_database_runtime,
    reset_database_runtime_for_tests,
)


class _Url:
    def __init__(self, backend: str, database: str | None) -> None:
        self._backend = backend
        self.database = database

    def get_backend_name(self) -> str:
        return self._backend


class _Engine:
    def __init__(self, backend: str, database: str | None) -> None:
        self.url = _Url(backend, database)


@pytest.fixture(autouse=True)
def _reset_runtime():
    reset_database_runtime_for_tests()
    try:
        yield
    finally:
        reset_database_runtime_for_tests()


def test_artifact_queries_use_core_content_contract_objects() -> None:
    queries = {
        artifact_type: (table, status_col, columns)
        for artifact_type, table, status_col, columns in ARTIFACT_QUERIES
    }

    assert queries["story"][:2] == ("stories", "status")
    assert queries["story"][2] is STORY_CONTENT_COLUMNS
    assert queries["ideation"][:2] == ("ideations", "status")
    assert queries["ideation"][2] is IDEATION_CONTENT_COLUMNS
    assert queries["refinement"][:2] == ("refinements", "status")
    assert queries["refinement"][2] is REFINEMENT_CONTENT_COLUMNS
    assert queries["spec"][:2] == ("specs", "status")
    assert queries["spec"][2] is SPEC_CONTENT_COLUMNS_V2
    assert queries["sprint"][:2] == ("sprints", "status")
    assert queries["sprint"][2] is SPRINT_CONTENT_COLUMNS


def test_reader_keeps_adapter_derived_fields_outside_content_hash(
    tmp_path: Path,
) -> None:
    first = _read_story(_story_db(tmp_path, ttl_days=7))
    second = _read_story(_story_db(tmp_path, ttl_days=30))

    assert first["content_hash"] == second["content_hash"]
    assert first["content_hash"] == _canonical_content_hash(
        _story_hash_row(), STORY_CONTENT_COLUMNS
    )
    assert first["working_ttl_days"] == 7
    assert second["working_ttl_days"] == 30
    assert first["source_artifact_status"] == "review"
    assert first["has_minimal_evidence"] is True


def test_reader_respects_explicit_db_path_provider(tmp_path: Path) -> None:
    db_path = _story_db(tmp_path, ttl_days=14)
    rows = CommunityBoardSourceReader(db_path_provider=lambda: db_path).fetch("b1")

    story = next(row for row in rows if row["artifact_type"] == "story")

    assert story["working_ttl_days"] == 14
    assert story["source_artifact_status"] == "review"


def test_reader_resolves_db_path_from_public_runtime(tmp_path: Path) -> None:
    from okto_pulse.community.adapters.sqlalchemy_database import (
        CommunityDatabaseRuntime,
    )

    db_path = _story_db(tmp_path, ttl_days=21)
    configure_database_runtime(
        runtime=CommunityDatabaseRuntime(
            engine=_Engine("sqlite", str(db_path)),  # type: ignore[arg-type]
            session_factory=lambda: object(),  # type: ignore[arg-type]
        ),
    )

    rows = CommunityBoardSourceReader().fetch("b1")

    story = next(row for row in rows if row["artifact_type"] == "story")
    assert story["working_ttl_days"] == 21


def test_reader_fails_closed_when_no_runtime_path_can_be_resolved() -> None:
    with pytest.raises(SourceUnavailableError) as exc:
        CommunityBoardSourceReader().fetch("b1")

    assert exc.value.code == "source_unavailable"
    assert exc.value.cause_type == "CommunityDatabasePathUnavailable"


def _read_story(db_path: Path) -> dict[str, object]:
    rows = CommunityBoardSourceReader(db_path).fetch("b1")
    return next(row for row in rows if row["artifact_type"] == "story")


def _story_db(tmp_path: Path, *, ttl_days: int) -> Path:
    db_path = tmp_path / f"story_ttl_{ttl_days}.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE boards ("
            "id TEXT, name TEXT, description TEXT, realm_id TEXT, settings TEXT)"
        )
        conn.execute(
            "CREATE TABLE stories ("
            "id TEXT, board_id TEXT, status TEXT, created_at TEXT, updated_at TEXT, "
            "title TEXT, description TEXT, actor TEXT, goal TEXT, benefit TEXT, "
            "topic_id TEXT, labels TEXT)"
        )
        # Card 5 makes the source census fail closed: even an empty source
        # family must be represented in the schema before any row is trusted.
        required_columns = {
            table: set(columns)
            for table, columns in _REQUIRED_SOURCE_COLUMNS.items()
            if table not in {"boards", "stories"}
        }
        for table, columns in sorted(required_columns.items()):
            declarations = ", ".join(
                f'"{column}" TEXT' for column in sorted(columns)
            )
            conn.execute(f"CREATE TABLE {table} ({declarations})")
        conn.execute(
            "INSERT INTO boards VALUES (?, ?, ?, ?, ?)",
            (
                "b1",
                "Board one",
                "",
                "realm-af07",
                json.dumps({"kg_working_ttl_days": ttl_days}),
            ),
        )
        conn.execute(
            "INSERT INTO stories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "story-1",
                "b1",
                "review",
                "2026-07-01T00:00:00Z",
                "2026-07-02T00:00:00Z",
                "Story title",
                "Story description",
                "Developer",
                "Keep hashes stable",
                "Deterministic rebuilds",
                "topic-1",
                '["kg", "source"]',
            ),
        )
        conn.commit()
    return db_path


def _story_hash_row() -> dict[str, object]:
    return {
        "title": "Story title",
        "description": "Story description",
        "actor": "Developer",
        "goal": "Keep hashes stable",
        "benefit": "Deterministic rebuilds",
        "topic_id": "topic-1",
        "status": "review",
        "labels": '["kg", "source"]',
    }
