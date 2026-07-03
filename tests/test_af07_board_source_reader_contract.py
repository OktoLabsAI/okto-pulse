"""AF-07 Community reader consumes core-owned source contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from okto_pulse.community.adapters.board_source_reader import (
    ARTIFACT_QUERIES,
    CommunityBoardSourceReader,
)
from okto_pulse.core.kg.board_source_store import (
    IDEATION_CONTENT_COLUMNS,
    REFINEMENT_CONTENT_COLUMNS,
    SPEC_CONTENT_COLUMNS_V2,
    SPRINT_CONTENT_COLUMNS,
    STORY_CONTENT_COLUMNS,
    _canonical_content_hash,
)


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


def _read_story(db_path: Path) -> dict[str, object]:
    rows = CommunityBoardSourceReader(db_path).fetch("b1")
    return next(row for row in rows if row["artifact_type"] == "story")


def _story_db(tmp_path: Path, *, ttl_days: int) -> Path:
    db_path = tmp_path / f"story_ttl_{ttl_days}.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE boards (id TEXT, settings TEXT)")
        conn.execute(
            "CREATE TABLE stories ("
            "id TEXT, board_id TEXT, status TEXT, created_at TEXT, updated_at TEXT, "
            "title TEXT, description TEXT, actor TEXT, goal TEXT, benefit TEXT, "
            "topic_id TEXT, labels TEXT)"
        )
        conn.execute(
            "INSERT INTO boards VALUES (?, ?)",
            ("b1", json.dumps({"kg_working_ttl_days": ttl_days})),
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
