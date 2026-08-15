from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
)
from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
    LEGACY_QUEUE_ONLY_INTENT_CODE,
    LEGACY_QUEUE_ONLY_INTENT_EFFECT,
    LEGACY_QUEUE_ONLY_KIND,
    LEGACY_QUEUE_ONLY_PREAPPLIED_ACTIONS,
    LEGACY_QUEUE_ONLY_REMAINING_ACTIONS,
    LEGACY_QUEUE_ONLY_SCHEMA,
    LegacyManualRestoreQueueOnlyIntent,
    LegacyQueueOnlyIntentError,
    canonical_evidence_hash,
)
from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects
from okto_pulse.core.application.rebuild_processor import (
    RebuildCheckpoint,
    RebuildCommand,
    RebuildEffectReceipt,
    RebuildOutcomeCode,
    RebuildState,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey


BOARD_ID = "15877207-c147-4805-96d7-d53a625571df"
MANIFEST_REF = "rebuild_manifest_legacy"
SOURCE = f"rebuild:{MANIFEST_REF}"
QUEUE_COLUMNS = (
    "id",
    "board_id",
    "artifact_type",
    "artifact_id",
    "work_kind",
    "generation",
    "payload",
    "delete_event_id",
    "priority",
    "source",
    "status",
    "triggered_at",
    "triggered_by_event",
    "claimed_by_session_id",
    "claim_token",
    "claimed_at",
    "last_error",
    "worker_id",
    "claim_timeout_at",
    "attempts",
    "next_retry_at",
)
DLQ_COLUMNS = (
    "id",
    "board_id",
    "artifact_type",
    "artifact_id",
    "original_queue_id",
    "attempts",
    "errors",
    "dead_lettered_at",
)


def _row(
    row_id: str,
    artifact_type: str,
    artifact_id: str,
    *,
    status: str,
    source: str = SOURCE,
) -> dict[str, object]:
    claimed = status == "claimed"
    return {
        "id": row_id,
        "board_id": BOARD_ID,
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "work_kind": "consolidate",
        "generation": 0,
        "payload": None,
        "delete_event_id": None,
        "priority": "high",
        "source": source,
        "status": status,
        "triggered_at": "2026-08-15 02:32:52",
        "triggered_by_event": None,
        "claimed_by_session_id": "worker-old" if claimed else None,
        "claim_token": "a" * 32 if claimed else None,
        "claimed_at": "2026-08-15 11:38:48.240793" if claimed else None,
        "last_error": None,
        "worker_id": "worker-old" if claimed else None,
        "claim_timeout_at": "2026-08-15 11:43:48.240793" if claimed else None,
        "attempts": 0,
        "next_retry_at": None,
    }


def _membership(row: dict[str, object], index: int) -> dict[str, str]:
    return {
        "row_id": str(row["id"]),
        "artifact_type": str(row["artifact_type"]),
        "artifact_id": str(row["artifact_id"]),
        "run_id": MANIFEST_REF,
        "source_ref": f"{row['artifact_type']}:{row['artifact_id']}",
        "source_version": str(index + 1),
        "content_hash": f"{index + 1:064x}",
    }


def _fingerprint(columns: tuple[str, ...], rows: list[dict[str, object]]) -> str:
    ordered = sorted(rows, key=lambda row: str(row["id"]))
    return canonical_evidence_hash(
        {
            "columns": list(columns),
            "rows": [[row[column] for column in columns] for row in ordered],
        }
    )


def _effect_artifact(effect_key: str, digest: str) -> dict[str, str]:
    effect_id = hashlib.sha256(effect_key.encode("utf-8")).hexdigest()[:24]
    return {
        "effect_key": effect_key,
        "relative": f"audit/f06-effect-{effect_id}.json",
        "sha256": digest * 64,
    }


def _intent(
    target_rows: list[dict[str, object]],
    *,
    non_target_rows: list[dict[str, object]],
    dlq_rows: list[dict[str, object]],
) -> LegacyManualRestoreQueueOnlyIntent:
    memberships = [_membership(row, index) for index, row in enumerate(target_rows)]
    evidence = {
        "schema_version": LEGACY_QUEUE_ONLY_SCHEMA,
        "reconciliation_kind": LEGACY_QUEUE_ONLY_KIND,
        "board_id": BOARD_ID,
        "recovery_actor_id": "owner-1",
        "recovery_reason": "governed offline recovery",
        "legacy_actor_id": "legacy-actor",
        "legacy_reason": f"explicit_rebuild:{MANIFEST_REF}",
        "manifest_ref": MANIFEST_REF,
        "f06_run_id": f"f06:{MANIFEST_REF}",
        "candidate_generation_id": "candidate-legacy",
        "checkpoint": {
            "relative": f"audit/f06-checkpoint-{'d' * 24}.json",
            "sha256": "1" * 64,
            "source_rows_sha256": "2" * 64,
            "state": "blocked",
        },
        "f06_artifacts": {
            "snapshot": _effect_artifact(f"f06:{MANIFEST_REF}:snapshot", "1"),
            "quarantine": _effect_artifact(f"f06:{MANIFEST_REF}:quarantine", "2"),
            "enqueue": _effect_artifact(f"f06:{MANIFEST_REF}:enqueue", "3"),
            "lease_lost_audit": _effect_artifact(
                f"f06:{MANIFEST_REF}:audit:lease_lost", "4"
            ),
        },
        "manifest": {
            "relative": f"manifests/{MANIFEST_REF}.json",
            "sha256": "3" * 64,
            "preflight_hash": "4" * 64,
            "source_set_hash": "5" * 64,
        },
        "terminal_run": {
            "run_id": "run_legacy",
            "audit_relative": "audit/run_legacy.json",
            "audit_sha256": "6" * 64,
            "report_id": "report_legacy",
            "report_relative": "reports/report_legacy.json",
            "report_sha256": "7" * 64,
            "confirmation_ref": f"conf_fp_{'8' * 64}",
            "confirmation_audit_relative": (
                f"audit/confirmation/{BOARD_ID}/audit_{'d' * 32}.json"
            ),
            "confirmation_audit_sha256": "9" * 64,
            "outcome": "rebuild_failed",
            "reason": "lease_lost",
            "event_emitted": True,
            "previous_generation_id": None,
            "current_generation_id": None,
            "promotion_outcome": None,
            "current_generation_pointer_present": False,
            "candidate_decision_present": False,
        },
        "original_quarantine": {
            "quarantine_id": "q_original",
            "manifest_relative": "q_original/manifest.json",
            "manifest_sha256": "a" * 64,
            "storage_hashes": {"q_original/graph.lbug": "b" * 64},
        },
        "manual_restore": {
            "quarantine_id": "q_manual",
            "manifest_relative": "q_manual/manifest.json",
            "manifest_sha256": "c" * 64,
            "journal_relative": "q_manual/restore_operation.json",
            "journal_sha256": "d" * 64,
            "source_quarantine_id": "q_original",
            "storage_hashes": {
                "q_manual/graph.lbug": "e" * 64,
                "q_manual/graph.lbug.wal": "f" * 64,
            },
        },
        "board_storage": {
            "hashes": {"graph.lbug": "0" * 64},
            "sha256": canonical_evidence_hash({"graph.lbug": "0" * 64}),
        },
        "queue": {
            "source": SOURCE,
            "snapshot_fingerprint": canonical_evidence_hash(
                {"rows": target_rows, "memberships": memberships}
            ),
            "rows": target_rows,
            "memberships": memberships,
            "board_non_target_fingerprint": _fingerprint(
                QUEUE_COLUMNS, non_target_rows
            ),
            "dlq_fingerprint": _fingerprint(DLQ_COLUMNS, dlq_rows),
        },
        "preapplied_actions": list(LEGACY_QUEUE_ONLY_PREAPPLIED_ACTIONS),
        "remaining_actions": list(LEGACY_QUEUE_ONLY_REMAINING_ACTIONS),
    }
    return LegacyManualRestoreQueueOnlyIntent.build(evidence)


def _database(tmp_path: Path):  # noqa: ANN202
    path = tmp_path / "pulse.db"
    target_rows = [
        _row("queue-claimed", "spec", "spec-1", status="claimed"),
        _row(
            "queue-pending", "code_investigation_receipt", "receipt-1", status="pending"
        ),
    ]
    non_target_rows = [
        _row(
            "queue-unrelated",
            "spec",
            "spec-unrelated",
            status="pending",
            source="spec_validated",
        )
    ]
    dlq_rows = [
        {
            "id": "dlq-unrelated",
            "board_id": BOARD_ID,
            "artifact_type": "story",
            "artifact_id": "story-unrelated",
            "original_queue_id": "old-unrelated",
            "attempts": 5,
            "errors": "[]",
            "dead_lettered_at": "2026-08-01 00:00:00",
        }
    ]
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE consolidation_queue ("
            "id TEXT PRIMARY KEY, board_id TEXT NOT NULL, "
            "artifact_type TEXT NOT NULL, artifact_id TEXT NOT NULL, "
            "work_kind TEXT NOT NULL, generation INTEGER NOT NULL, payload TEXT, "
            "delete_event_id TEXT, priority TEXT NOT NULL, source TEXT NOT NULL, "
            "status TEXT NOT NULL, triggered_at TEXT, triggered_by_event TEXT, "
            "claimed_by_session_id TEXT, claim_token TEXT, claimed_at TEXT, "
            "last_error TEXT, worker_id TEXT, claim_timeout_at TEXT, "
            "attempts INTEGER NOT NULL, next_retry_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE consolidation_dead_letter ("
            "id TEXT PRIMARY KEY, board_id TEXT NOT NULL, artifact_type TEXT, "
            "artifact_id TEXT, original_queue_id TEXT, attempts INTEGER, "
            "errors TEXT, dead_lettered_at TEXT)"
        )
        placeholders = ",".join("?" for _ in QUEUE_COLUMNS)
        for row in [*target_rows, *non_target_rows]:
            connection.execute(
                f"INSERT INTO consolidation_queue ({','.join(QUEUE_COLUMNS)}) "
                f"VALUES ({placeholders})",
                tuple(row[column] for column in QUEUE_COLUMNS),
            )
        dlq_placeholders = ",".join("?" for _ in DLQ_COLUMNS)
        for row in dlq_rows:
            connection.execute(
                f"INSERT INTO consolidation_dead_letter ({','.join(DLQ_COLUMNS)}) "
                f"VALUES ({dlq_placeholders})",
                tuple(row[column] for column in DLQ_COLUMNS),
            )
    return path, target_rows, non_target_rows, dlq_rows


def _queue_state(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            f"SELECT {','.join(QUEUE_COLUMNS)} FROM consolidation_queue ORDER BY id"
        ).fetchall()


def _dlq_state(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            f"SELECT {','.join(DLQ_COLUMNS)} "
            "FROM consolidation_dead_letter ORDER BY board_id, id"
        ).fetchall()


class _ArtifactStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.writes: list[str] = []

    def read_json(self, key: RebuildAuditKey):  # noqa: ANN201
        value = self.rows.get(key.to_ref())
        return copy.deepcopy(value) if value is not None else None

    def write_json_atomic(
        self,
        key: RebuildAuditKey,
        payload: dict[str, object],
    ) -> None:
        self.writes.append(key.to_ref())
        self.rows[key.to_ref()] = copy.deepcopy(payload)

    def replace_json(self, key: RebuildAuditKey, transform):  # noqa: ANN001, ANN201
        value = transform(self.read_json(key))
        self.write_json_atomic(key, value)
        return copy.deepcopy(value)


def test_legacy_queue_only_cas_writes_exact_v4_tombstones_and_replays(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    adapter = CommunityBoardRebuildIngestionAdapter(db_path=path)
    guard_calls: list[int] = []

    first = adapter.compensate_legacy_manual_restore_queue_only(
        intent_payload=intent.to_payload(),
        mutation_guard=lambda: not guard_calls.append(len(guard_calls) + 1),
    )

    assert first == {
        "reconciliation_kind": LEGACY_QUEUE_ONLY_KIND,
        "evidence_digest": intent.evidence_digest,
        "queue_source": SOURCE,
        "pending_compensated": 1,
        "claimed_compensated": 1,
        "already_compensated": 0,
        "active_remaining": 0,
        "live_intents_restored": 0,
        "total_compensated": 2,
    }
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM consolidation_queue WHERE source=? ORDER BY id",
            (SOURCE,),
        ).fetchall()
        unrelated = connection.execute(
            "SELECT * FROM consolidation_queue WHERE id='queue-unrelated'"
        ).fetchone()
        dlq = connection.execute(
            "SELECT * FROM consolidation_dead_letter WHERE id='dlq-unrelated'"
        ).fetchone()
    assert unrelated is not None and unrelated["status"] == "pending"
    assert dlq is not None and dlq["original_queue_id"] == "old-unrelated"
    memberships = {row["row_id"]: row for row in intent.memberships}
    for row in rows:
        assert row["status"] == "failed"
        assert row["last_error"] == "rebuild_compensated"
        assert all(
            row[key] is None
            for key in (
                "claimed_by_session_id",
                "claim_token",
                "claimed_at",
                "worker_id",
                "claim_timeout_at",
                "next_retry_at",
            )
        )
        membership = memberships[row["id"]]
        assert json.loads(row["payload"]) == {
            "_rebuild_membership": {
                key: membership[key]
                for key in (
                    "run_id",
                    "source_ref",
                    "source_version",
                    "content_hash",
                )
            }
        }

    terminal_state = _queue_state(path)
    replay = adapter.compensate_legacy_manual_restore_queue_only(
        intent_payload=intent.to_payload(),
        mutation_guard=lambda: True,
    )
    assert replay["already_compensated"] == 2
    assert replay["total_compensated"] == 0
    assert _queue_state(path) == terminal_state


def test_legacy_queue_only_cas_drift_rolls_back_every_target(tmp_path: Path) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE consolidation_queue SET priority='low' WHERE id='queue-claimed'"
        )
    before = _queue_state(path)

    with pytest.raises(RuntimeError, match="row_cas_conflict"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before
    assert all(row[10] in {"pending", "claimed"} for row in before[:2])


def test_legacy_queue_only_guard_loss_before_commit_rolls_back(tmp_path: Path) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    before = _queue_state(path)
    calls = 0

    def guard() -> bool:
        nonlocal calls
        calls += 1
        return calls < 5

    with pytest.raises(RuntimeError, match="mutation_guard_lost:before_commit"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=guard,
        )

    assert _queue_state(path) == before


def test_legacy_queue_only_refuses_cross_board_queue_update_trigger(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    other = _row(
        "queue-other-board",
        "spec",
        "spec-other-board",
        status="pending",
        source="spec_validated",
    )
    other["board_id"] = "0d7c469e-9db1-4d26-a9ea-c80a7deaa770"
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO consolidation_queue ({','.join(QUEUE_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in QUEUE_COLUMNS)})",
            tuple(other[column] for column in QUEUE_COLUMNS),
        )
        connection.execute(
            "CREATE TRIGGER mutate_other_board_queue "
            "AFTER UPDATE ON consolidation_queue "
            "WHEN NEW.id='queue-pending' BEGIN "
            "UPDATE consolidation_queue SET priority='low' "
            "WHERE id='queue-other-board'; END"
        )
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    before = _queue_state(path)

    with pytest.raises(RuntimeError, match="queue_trigger_present"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before


def test_legacy_queue_only_refuses_cross_board_dlq_insert_trigger(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER mutate_other_board_dlq "
            "AFTER UPDATE ON consolidation_queue "
            "WHEN NEW.id='queue-pending' BEGIN "
            "INSERT INTO consolidation_dead_letter "
            "(id,board_id,artifact_type,artifact_id,original_queue_id,attempts,"
            "errors,dead_lettered_at) VALUES "
            "('dlq-other-board','0d7c469e-9db1-4d26-a9ea-c80a7deaa770',"
            "'spec','spec-other-board','queue-other-board',1,'[]',"
            "'2026-08-15 12:00:00'); END"
        )
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    before_queue = _queue_state(path)
    before_dlq = _dlq_state(path)

    with pytest.raises(RuntimeError, match="queue_trigger_present"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before_queue
    assert _dlq_state(path) == before_dlq


def test_legacy_queue_only_refuses_trigger_side_effect_in_unrelated_table(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO app_settings (key, value) VALUES ('mode', 'safe')"
        )
        connection.execute(
            "CREATE TRIGGER mutate_app_settings "
            "AFTER UPDATE ON consolidation_queue "
            "WHEN NEW.id='queue-pending' BEGIN "
            "UPDATE app_settings SET value='unsafe' WHERE key='mode'; END"
        )
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )

    with pytest.raises(RuntimeError, match="queue_trigger_present"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM app_settings WHERE key='mode'"
        ).fetchone() == ("safe",)


def test_legacy_queue_only_refuses_virtual_queue_and_preserves_shadow_tables(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE consolidation_queue")
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE consolidation_queue USING fts5("
                + ",".join(QUEUE_COLUMNS)
                + ")"
            )
        except sqlite3.OperationalError as exc:
            pytest.skip(f"SQLite FTS5 unavailable: {exc}")

    def shadow_state() -> dict[str, tuple[tuple[object, ...], ...]]:
        with sqlite3.connect(path) as connection:
            names = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'consolidation_queue%' "
                    "ORDER BY name"
                )
            )
            return {
                name: tuple(
                    tuple(row)
                    for row in connection.execute(
                        'SELECT * FROM "' + name.replace('"', '""') + '"'
                    )
                )
                for name in names
            }

    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    before = shadow_state()

    with pytest.raises(RuntimeError, match="queue_storage_invalid"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert shadow_state() == before


def test_legacy_queue_only_refuses_generated_queue_column_before_mutation(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    projection = ",".join(QUEUE_COLUMNS)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE consolidation_queue RENAME TO old_queue")
        connection.execute(
            "CREATE TABLE consolidation_queue ("
            "id TEXT PRIMARY KEY, board_id TEXT NOT NULL, "
            "artifact_type TEXT NOT NULL, artifact_id TEXT NOT NULL, "
            "work_kind TEXT NOT NULL, generation INTEGER NOT NULL, payload TEXT, "
            "delete_event_id TEXT, priority TEXT NOT NULL, source TEXT NOT NULL, "
            "status TEXT NOT NULL, triggered_at TEXT, triggered_by_event TEXT, "
            "claimed_by_session_id TEXT, claim_token TEXT, claimed_at TEXT, "
            "last_error TEXT, worker_id TEXT, claim_timeout_at TEXT, "
            "attempts INTEGER NOT NULL, next_retry_at TEXT, "
            "shadow TEXT GENERATED ALWAYS AS "
            "(status || coalesce(payload, '')) STORED)"
        )
        connection.execute(
            f"INSERT INTO consolidation_queue ({projection}) "
            f"SELECT {projection} FROM old_queue"
        )
        connection.execute("DROP TABLE old_queue")
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )

    def generated_state() -> list[tuple[object, ...]]:
        with sqlite3.connect(path) as connection:
            return connection.execute(
                "SELECT id, status, payload, shadow "
                "FROM consolidation_queue ORDER BY id"
            ).fetchall()

    before = generated_state()
    with pytest.raises(RuntimeError, match="schema_mismatch"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )
    assert generated_state() == before


@pytest.mark.parametrize(
    ("limit_name", "limit", "error"),
    (
        ("_MAX_LEGACY_PROTECTED_ROWS", 1, "row_limit_exceeded"),
        ("_MAX_LEGACY_PROTECTED_BYTES", 1, "byte_limit_exceeded"),
    ),
)
def test_legacy_queue_only_bounds_protected_partition_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    error: str,
) -> None:
    from okto_pulse.community.adapters import board_rebuild_ingestion

    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    second = _row(
        "queue-unrelated-second",
        "spec",
        "spec-unrelated-second",
        status="pending",
        source="spec_validated",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO consolidation_queue ({','.join(QUEUE_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in QUEUE_COLUMNS)})",
            tuple(second[column] for column in QUEUE_COLUMNS),
        )
    intent = _intent(
        target_rows,
        non_target_rows=[*non_target_rows, second],
        dlq_rows=dlq_rows,
    )
    before = _queue_state(path)
    monkeypatch.setattr(board_rebuild_ingestion, limit_name, limit)

    with pytest.raises(RuntimeError, match=error):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before


def test_legacy_queue_only_cas_rejects_dlq_identity_even_in_baseline(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    alias = {
        "id": "dlq-target-alias",
        "board_id": BOARD_ID,
        "artifact_type": target_rows[0]["artifact_type"],
        "artifact_id": target_rows[0]["artifact_id"],
        "original_queue_id": "different-queue-id",
        "attempts": 1,
        "errors": "[]",
        "dead_lettered_at": "2026-08-15 11:50:00",
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO consolidation_dead_letter ({','.join(DLQ_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in DLQ_COLUMNS)})",
            tuple(alias[column] for column in DLQ_COLUMNS),
        )
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=[*dlq_rows, alias],
    )
    before = _queue_state(path)

    with pytest.raises(RuntimeError, match="dlq_identity_conflict"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before


def test_legacy_queue_only_cas_rejects_peer_identity_even_in_baseline(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    peer = _row(
        "queue-peer",
        str(target_rows[0]["artifact_type"]),
        str(target_rows[0]["artifact_id"]),
        status="pending",
        source="live-peer",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO consolidation_queue ({','.join(QUEUE_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in QUEUE_COLUMNS)})",
            tuple(peer[column] for column in QUEUE_COLUMNS),
        )
    intent = _intent(
        target_rows,
        non_target_rows=[*non_target_rows, peer],
        dlq_rows=dlq_rows,
    )
    before = _queue_state(path)

    with pytest.raises(RuntimeError, match="peer_identity_conflict"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before


def test_legacy_queue_only_cas_converges_from_mixed_active_and_terminal(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    adapter = CommunityBoardRebuildIngestionAdapter(db_path=path)
    adapter.compensate_legacy_manual_restore_queue_only(
        intent_payload=intent.to_payload(),
        mutation_guard=lambda: True,
    )
    restored = target_rows[0]
    with sqlite3.connect(path) as connection:
        assignments = ",".join(
            f'"{column}"=?' for column in QUEUE_COLUMNS if column != "id"
        )
        connection.execute(
            f"UPDATE consolidation_queue SET {assignments} WHERE id=?",
            (
                *(restored[column] for column in QUEUE_COLUMNS if column != "id"),
                restored["id"],
            ),
        )

    result = adapter.compensate_legacy_manual_restore_queue_only(
        intent_payload=intent.to_payload(),
        mutation_guard=lambda: True,
    )

    assert result["already_compensated"] == 1
    assert result["claimed_compensated"] == 1
    assert result["total_compensated"] == 1


@pytest.mark.parametrize(
    ("path", "replacement", "error"),
    (
        (("checkpoint", "relative"), "audit/f06-checkpoint-x/evil.json", "checkpoint"),
        (("manifest_ref",), "rebuild_manifest_x/evil", "manifest_ref"),
        (("terminal_run", "run_id"), "run_x/evil", "run_id"),
        (("terminal_run", "report_id"), "report_x/evil", "report_id"),
        (
            ("terminal_run", "confirmation_audit_relative"),
            f"audit/confirmation/{BOARD_ID}/nested/audit_{'d' * 32}.json",
            "terminal_run",
        ),
        (("original_quarantine", "quarantine_id"), "q_x/evil", "quarantine_id"),
        (("queue", "rows", 0, "generation"), 1, "queue_row"),
        (("queue", "rows", 0, "attempts"), False, "queue_row"),
        (("queue", "rows", 0, "priority"), "low", "queue_row"),
        (("queue", "rows", 0, "triggered_at"), "not-a-time", "triggered_at"),
        (("queue", "rows", 0, "claim_token"), "not-hex", "queue_claim"),
        (("queue", "rows", 0, "worker_id"), "other", "queue_claim"),
        (("queue", "memberships", 0, "source_version"), 1, "membership"),
        (("queue", "memberships", 0, "content_hash"), 1, "membership"),
    ),
)
def test_legacy_queue_only_intent_rejects_unsafe_refs_and_row_shape(
    tmp_path: Path,
    path: tuple[object, ...],
    replacement: object,
    error: str,
) -> None:
    _db_path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    payload = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    ).to_payload()
    tampered = copy.deepcopy(payload)
    cursor: object = tampered
    for key in path[:-1]:
        cursor = cursor[key]  # type: ignore[index]
    cursor[path[-1]] = replacement  # type: ignore[index]

    with pytest.raises(LegacyQueueOnlyIntentError, match=error):
        LegacyManualRestoreQueueOnlyIntent.from_payload(tampered)


def test_legacy_queue_only_adapter_persists_intent_before_exact_queue_cas(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    store = _ArtifactStore()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=path,
        artifact_store=store,
    )
    command = RebuildCommand(
        run_id=intent.f06_run_id,
        board_id=BOARD_ID,
        manifest_ref=MANIFEST_REF,
        operation="rebuild",
        actor_id="legacy-actor",
        reason=f"explicit_rebuild:{MANIFEST_REF}",
        candidate_generation_id="candidate-legacy",
    )
    prefix_receipts = {
        f"{command.run_id}:{effect}": RebuildEffectReceipt(
            effect_key=f"{command.run_id}:{effect}",
            effect=effect,
            ok=True,
        )
        for effect in ("snapshot", "quarantine", "enqueue")
    }
    now = datetime.now(timezone.utc)
    owner._rebuild_checkpoint_cache[command.run_id] = RebuildCheckpoint(
        command=command,
        state=RebuildState.BLOCKED,
        started_at=now,
        last_progress_at=now,
        receipts=MappingProxyType(prefix_receipts),
    )
    intent_receipt = RebuildEffectReceipt(
        effect_key=f"{command.run_id}:{LEGACY_QUEUE_ONLY_INTENT_EFFECT}",
        effect=LEGACY_QUEUE_ONLY_INTENT_EFFECT,
        ok=True,
        code=LEGACY_QUEUE_ONLY_INTENT_CODE,
        details=intent.to_payload(),
    )
    probes: list[str] = []
    adapter = owner.build_legacy_manual_restore_queue_only_adapter(
        evidence_probe=lambda current: not probes.append(current.evidence_digest),
    )

    outcome = adapter(
        SimpleNamespace(
            board_id=BOARD_ID,
            intent_id=intent.evidence_digest,
            actor_id="owner-1",
            reason="governed offline recovery",
            owner_token="writer-token",
            command=command,
            intent_receipt=intent_receipt,
            lease_renew=lambda: True,
            orchestration_renew=lambda: True,
            mutation_guard=lambda: True,
        )
    )

    assert outcome.state is RebuildState.FAILED
    assert outcome.code is RebuildOutcomeCode.LEGACY_MANUAL_RESTORE_QUEUE_RECONCILED
    assert outcome.promotion_allowed is False
    assert [action.value for action in outcome.compensation_actions] == [
        "cancel_enqueued_sources"
    ]
    assert probes == [intent.evidence_digest]
    intent_writes = [
        ref
        for ref in store.writes
        if store.rows[ref].get("effect") == LEGACY_QUEUE_ONLY_INTENT_EFFECT
    ]
    assert len(intent_writes) == 1
    checkpoint_writes = [ref for ref in store.writes if "f06-checkpoint-" in ref]
    assert checkpoint_writes
    assert store.writes.index(intent_writes[0]) < store.writes.index(
        checkpoint_writes[0]
    )
    terminal = next(
        payload
        for payload in store.rows.values()
        if payload.get("code") == "legacy_manual_restore_queue_only_reconciled"
        and payload.get("effect") == "compensate"
    )
    assert terminal["details"] == {
        "actions": ["cancel_enqueued_sources"],
        "reconciliation_kind": LEGACY_QUEUE_ONLY_KIND,
        "intent_digest": intent.evidence_digest,
        "queue": {
            "source": SOURCE,
            "expected_row_count": 2,
            "terminal_fingerprint": intent.terminal_queue_fingerprint,
            "pending_compensated": 1,
            "claimed_compensated": 1,
            "already_compensated": 0,
            "active_remaining": 0,
            "live_intents_restored": 0,
            "total_compensated": 2,
            "evidence_digest": intent.evidence_digest,
        },
    }


def test_legacy_queue_only_terminal_receipt_requires_live_fences_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    store = _ArtifactStore()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=path,
        artifact_store=store,
    )
    command = RebuildCommand(
        run_id=intent.f06_run_id,
        board_id=BOARD_ID,
        manifest_ref=MANIFEST_REF,
        operation="rebuild",
        actor_id="legacy-actor",
        reason="legacy",
    )
    intent_receipt = RebuildEffectReceipt(
        effect_key=f"{command.run_id}:{LEGACY_QUEUE_ONLY_INTENT_EFFECT}",
        effect=LEGACY_QUEUE_ONLY_INTENT_EFFECT,
        ok=True,
        code=LEGACY_QUEUE_ONLY_INTENT_CODE,
        details=intent.to_payload(),
    )
    now = datetime.now(timezone.utc)
    owner._rebuild_checkpoint_cache[command.run_id] = RebuildCheckpoint(
        command=command,
        state=RebuildState.COMPENSATING,
        started_at=now,
        last_progress_at=now,
        receipts=MappingProxyType({intent_receipt.effect_key: intent_receipt}),
    )
    live = True
    original = owner.compensate_legacy_manual_restore_queue_only

    def _commit_then_lose(_self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        nonlocal live
        result = original(**kwargs)
        live = False
        return result

    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "compensate_legacy_manual_restore_queue_only",
        _commit_then_lose,
    )
    effects = CommunityRebuildEffects(owner, artifact_store=store)

    with pytest.raises(RuntimeError, match="terminal_guard_lost:after_queue_commit"):
        effects.compensate(
            SimpleNamespace(
                run_id=command.run_id,
                board_id=BOARD_ID,
                failed_state=RebuildState.ENQUEUED,
                actions=(SimpleNamespace(value="cancel_enqueued_sources"),),
                receipt_keys=(intent_receipt.effect_key,),
                mutation_guard=lambda: live,
            ),
            effect_key=f"{command.run_id}:compensate",
        )

    assert not any(
        payload.get("effect") == "compensate" for payload in store.rows.values()
    )
    with sqlite3.connect(path) as connection:
        statuses = connection.execute(
            "SELECT status FROM consolidation_queue WHERE source=? ORDER BY id",
            (SOURCE,),
        ).fetchall()
    assert statuses == [("failed",), ("failed",)]


@pytest.mark.parametrize(
    ("path", "replacement", "error"),
    (
        (("queue", "rows", 0, "payload"), "{}", "queue_row_invalid"),
        (("queue", "memberships", 0, "content_hash"), "bad", "membership_invalid"),
        (
            ("manual_restore", "source_quarantine_id"),
            "q_other",
            "manual_restore_invalid",
        ),
        (("remaining_actions",), ["restore_quarantine"], "actions_invalid"),
        (("evidence_digest",), "0" * 64, "digest_invalid"),
    ),
)
def test_legacy_queue_only_intent_rejects_tampering(
    tmp_path: Path,
    path: tuple[object, ...],
    replacement: object,
    error: str,
) -> None:
    _db_path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    payload = _intent(
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    ).to_payload()
    tampered = copy.deepcopy(payload)
    cursor: object = tampered
    for key in path[:-1]:
        cursor = cursor[key]  # type: ignore[index]
    cursor[path[-1]] = replacement  # type: ignore[index]

    with pytest.raises(LegacyQueueOnlyIntentError, match=error):
        LegacyManualRestoreQueueOnlyIntent.from_payload(tampered)
