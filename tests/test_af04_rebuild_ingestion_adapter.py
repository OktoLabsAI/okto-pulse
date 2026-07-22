from __future__ import annotations

import sqlite3
from pathlib import Path

from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
)


def test_rebuild_ingestion_enqueue_preserves_adapter_coverage(tmp_path: Path) -> None:
    db_path = _queue_db(tmp_path)
    adapter = CommunityBoardRebuildIngestionAdapter(db_path=db_path)

    counts = adapter.enqueue_sources(
        board_id="board-1",
        run_id="run-1",
        sources=[
            {"artifact_type": "story", "id": "story-new"},
            {"artifact_type": "test", "id": "test-card"},
            {"artifact_type": "decision", "id": "decision-skipped"},
        ],
    )

    assert counts == {"inserted": 2, "reset_to_pending": 0, "left_alone": 0}
    rows = _queue_rows(db_path)
    assert {
        (row["artifact_type"], row["artifact_id"], row["status"], row["priority"], row["source"])
        for row in rows
    } == {
        ("story", "story-new", "pending", "high", "rebuild:run-1"),
        ("card", "test-card", "pending", "high", "rebuild:run-1"),
    }


def test_rebuild_ingestion_leaves_active_rows_and_resets_terminal_rows(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consolidation_queue "
            "(id, board_id, artifact_type, artifact_id, priority, source, status, "
            "triggered_at, attempts, last_error, claimed_by_session_id, worker_id) "
            "VALUES "
            "('pending-row', 'board-1', 'story', 'story-active', 'low', 'old', "
            "'pending', datetime('now'), 4, 'keep', 'session-1', 'worker-1'), "
            "('done-row', 'board-1', 'spec', 'spec-terminal', 'low', 'old', "
            "'done', datetime('now'), 3, 'old-error', 'session-2', 'worker-2')"
        )
        conn.commit()

    adapter = CommunityBoardRebuildIngestionAdapter(db_path=db_path)
    counts = adapter.enqueue_sources(
        board_id="board-1",
        run_id="run-2",
        sources=[
            {"artifact_type": "story", "id": "story-active"},
            {"artifact_type": "spec", "id": "spec-terminal"},
        ],
    )

    assert counts == {"inserted": 0, "reset_to_pending": 1, "left_alone": 1}
    rows = {
        row["artifact_id"]: row
        for row in _queue_rows(db_path)
    }
    assert rows["story-active"]["status"] == "pending"
    assert rows["story-active"]["attempts"] == 4
    assert rows["story-active"]["last_error"] == "keep"
    assert rows["story-active"]["claimed_by_session_id"] == "session-1"
    assert rows["story-active"]["worker_id"] == "worker-1"

    reset = rows["spec-terminal"]
    assert reset["status"] == "pending"
    assert reset["attempts"] == 0
    assert reset["last_error"] is None
    assert reset["claimed_by_session_id"] is None
    assert reset["worker_id"] is None
    assert reset["priority"] == "high"
    assert reset["source"] == "rebuild:run-2"


def _queue_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "pulse.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE consolidation_queue ("
            "id TEXT PRIMARY KEY, "
            "board_id TEXT NOT NULL, "
            "artifact_type TEXT NOT NULL, "
            "artifact_id TEXT NOT NULL, "
            "priority TEXT NOT NULL, "
            "source TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "triggered_at TEXT, "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "last_error TEXT, "
            "claimed_by_session_id TEXT, "
            "claimed_at TEXT, "
            "worker_id TEXT, "
            "claim_timeout_at TEXT, "
            "next_retry_at TEXT, "
            "work_kind TEXT NOT NULL DEFAULT 'consolidate', "
            "generation INTEGER NOT NULL DEFAULT 0, "
            "payload JSON, "
            "CHECK(work_kind IN ('consolidate','stale_reconcile','stale_sweep'))"
            ")"
        )
        conn.execute(
            "CREATE UNIQUE INDEX uq_queue_consolidate_board_artifact "
            "ON consolidation_queue(board_id, artifact_type, artifact_id) "
            "WHERE work_kind='consolidate'"
        )
        conn.commit()
    return db_path


def _queue_rows(db_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT artifact_type, artifact_id, status, priority, source, attempts, "
            "last_error, claimed_by_session_id, worker_id "
            "FROM consolidation_queue ORDER BY artifact_type, artifact_id"
        ).fetchall()
