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

    assert counts == {
        "inserted": 2,
        "reset_to_pending": 0,
        "reordered_pending": 0,
        "fenced_claimed": 0,
        "deferred_unrelated": 0,
        "preserved_live_intent": 0,
        "left_alone": 0,
    }
    rows = _queue_rows(db_path)
    assert {
        (
            row["artifact_type"],
            row["artifact_id"],
            row["status"],
            row["priority"],
            row["source"],
        )
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

    assert counts == {
        "inserted": 0,
        "reset_to_pending": 1,
        "reordered_pending": 1,
        "fenced_claimed": 0,
        "deferred_unrelated": 0,
        "preserved_live_intent": 1,
        "left_alone": 0,
    }
    rows = {row["artifact_id"]: row for row in _queue_rows(db_path)}
    assert rows["story-active"]["status"] == "pending"
    assert rows["story-active"]["attempts"] == 0
    assert rows["story-active"]["last_error"] is None
    assert rows["story-active"]["claimed_by_session_id"] is None
    assert rows["story-active"]["worker_id"] is None
    assert rows["story-active"]["source"] == "rebuild:run-2"

    reset = rows["spec-terminal"]
    assert reset["status"] == "pending"
    assert reset["attempts"] == 0
    assert reset["last_error"] is None
    assert reset["claimed_by_session_id"] is None
    assert reset["worker_id"] is None
    assert reset["priority"] == "high"
    assert reset["source"] == "rebuild:run-2"


def test_rebuild_adopts_live_and_stale_claims_without_losing_live_retry(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consolidation_queue "
            "(id, board_id, artifact_type, artifact_id, priority, source, status, "
            "attempts, payload, claimed_by_session_id, worker_id) VALUES "
            "('live-row', 'board-1', 'story', 'story-live', 'high', "
            "'event:story.updated', 'pending', 0, '{\"revision\":2}', NULL, NULL), "
            "('old-rebuild', 'board-1', 'spec', 'spec-old', 'high', "
            "'rebuild:old-manifest', 'claimed', 3, NULL, 'stale-session', "
            "'stale-worker')"
        )
        conn.commit()

    adapter = CommunityBoardRebuildIngestionAdapter(db_path=db_path)
    counts = adapter.enqueue_sources(
        board_id="board-1",
        run_id="new-manifest",
        sources=[
            {"artifact_type": "story", "id": "story-live"},
            {"artifact_type": "spec", "id": "spec-old"},
        ],
    )

    assert counts["preserved_live_intent"] == 1
    assert counts["reordered_pending"] == 1
    assert counts["fenced_claimed"] == 1
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        live = conn.execute(
            "SELECT * FROM consolidation_queue WHERE id='live-row'"
        ).fetchone()
        old = conn.execute(
            "SELECT * FROM consolidation_queue WHERE id='old-rebuild'"
        ).fetchone()
    assert live is not None and old is not None
    assert live["source"] == "rebuild:new-manifest"
    marker = __import__("json").loads(live["payload"])["_rebuild_deferred_live"]
    assert marker == {
        "source": "event:story.updated",
        "triggered_by_event": None,
        "payload": {"revision": 2},
    }
    assert old["source"] == "rebuild:new-manifest"
    assert old["status"] == "pending"
    assert old["claimed_by_session_id"] is None
    assert old["worker_id"] is None

    compensation = adapter.compensate_pending_sources(
        board_id="board-1",
        run_id="new-manifest",
    )
    assert compensation["live_intents_restored"] == 1
    assert compensation["active_remaining"] == 0
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        live = conn.execute(
            "SELECT * FROM consolidation_queue WHERE id='live-row'"
        ).fetchone()
        old = conn.execute(
            "SELECT * FROM consolidation_queue WHERE id='old-rebuild'"
        ).fetchone()
    assert live is not None and old is not None
    assert live["status"] == "pending"
    assert live["source"] == "event:story.updated"
    assert __import__("json").loads(live["payload"]) == {"revision": 2}
    assert old["status"] == "failed"


def test_empty_manifest_releases_preclaimed_board_work(tmp_path: Path) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consolidation_queue "
            "(id, board_id, artifact_type, artifact_id, priority, source, status, "
            "attempts, claimed_by_session_id, claim_token, claimed_at, worker_id, "
            "claim_timeout_at) VALUES "
            "('preclaimed-live', 'board-1', 'story', 'story-live', 'high', "
            "'event:story.updated', 'claimed', 2, 'session-old', 'token-old', "
            "datetime('now'), 'worker-old', datetime('now', '+5 minutes'))"
        )
        conn.commit()

    counts = CommunityBoardRebuildIngestionAdapter(db_path=db_path).enqueue_sources(
        board_id="board-1",
        run_id="empty-manifest",
        sources=(),
    )

    assert counts["deferred_unrelated"] == 1
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM consolidation_queue WHERE id='preclaimed-live'"
        ).fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["source"] == "event:story.updated"
    assert row["attempts"] == 2
    assert row["claimed_by_session_id"] is None
    assert row["claim_token"] is None
    assert row["claimed_at"] is None
    assert row["worker_id"] is None
    assert row["claim_timeout_at"] is None


def test_queue_observation_scopes_depth_to_active_rows_and_board(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO consolidation_queue "
            "(id, board_id, artifact_type, artifact_id, priority, source, status, "
            "attempts, last_error) VALUES (?, ?, 'story', ?, 'high', 'test', ?, 0, ?)",
            (
                ("board-1-pending", "board-1", "story-1", "pending", None),
                ("board-1-claimed", "board-1", "story-2", "claimed", "retryable"),
                (
                    "board-1-done",
                    "board-1",
                    "story-3",
                    "done",
                    "graph_memory_pressure: ignored terminal row",
                ),
                (
                    "board-2-pending",
                    "board-2",
                    "story-4",
                    "pending",
                    "graph_memory_pressure: ignored other board",
                ),
            ),
        )
        conn.commit()

    adapter = CommunityBoardRebuildIngestionAdapter(db_path=db_path)

    assert adapter.queue_observation("board-1") == (2, None)
    assert adapter.queue_depth("board-1") == 2
    assert type(adapter.queue_depth("board-1")) is int


def test_queue_observation_reports_active_graph_memory_pressure(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consolidation_queue "
            "(id, board_id, artifact_type, artifact_id, priority, source, status, "
            "attempts, last_error) VALUES "
            "('blocked', 'board-1', 'story', 'story-1', 'high', 'test', "
            "'pending', 0, 'graph_memory_pressure')"
        )
        conn.commit()

    adapter = CommunityBoardRebuildIngestionAdapter(db_path=db_path)

    assert adapter.queue_observation("board-1") == (
        1,
        "graph_memory_pressure",
    )

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE consolidation_queue SET "
            "last_error='  Graph_Memory_Pressure: allocator exhausted  ' "
            "WHERE id='blocked'"
        )
        conn.commit()

    assert adapter.queue_observation("board-1") == (
        1,
        "graph_memory_pressure",
    )

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE consolidation_queue SET last_error='transient graph error' "
            "WHERE id='blocked'"
        )
        conn.commit()

    assert adapter.queue_observation("board-1") == (1, None)


def test_rebuild_observation_blocks_new_dead_letter_even_at_zero_depth(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE consolidation_dead_letter ("
            "id TEXT PRIMARY KEY, board_id TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO consolidation_dead_letter(id, board_id) "
            "VALUES ('baseline', 'board-1')"
        )
        conn.commit()
    adapter = CommunityBoardRebuildIngestionAdapter(db_path=db_path)
    baseline = adapter.dead_letter_ids("board-1")
    assert baseline == ("baseline",)

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consolidation_dead_letter(id, board_id) "
            "VALUES ('new-from-rebuild', 'board-1')"
        )
        conn.commit()

    assert adapter.queue_observation(
        "board-1",
        run_id="manifest-1",
        baseline_dead_letter_ids=baseline,
    ) == (0, "rebuild_new_dead_letter")


def test_queue_observation_does_not_abort_for_claimed_retry_in_progress(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consolidation_queue "
            "(id, board_id, artifact_type, artifact_id, priority, source, status, "
            "attempts, last_error) VALUES "
            "('retrying', 'board-1', 'story', 'story-1', 'high', 'test', "
            "'claimed', 1, 'graph_memory_pressure: previous attempt')"
        )
        conn.commit()

    adapter = CommunityBoardRebuildIngestionAdapter(db_path=db_path)

    # The row still contributes to depth, but a live claim is making progress;
    # only a pending backoff row is a deterministic blocker.
    assert adapter.queue_observation("board-1") == (1, None)


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
            "claim_token TEXT, "
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
