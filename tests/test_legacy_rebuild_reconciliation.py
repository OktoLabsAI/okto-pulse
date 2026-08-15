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
    read_legacy_source_revision_state,
)
from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
    LEGACY_DEAD_LETTER_COLUMNS,
    LEGACY_QUEUE_ONLY_INTENT_CODE,
    LEGACY_QUEUE_ONLY_INTENT_EFFECT,
    LEGACY_QUEUE_ONLY_KIND,
    LEGACY_QUEUE_ONLY_PREAPPLIED_ACTIONS,
    LEGACY_QUEUE_ONLY_PREFIX_SCHEMA,
    LEGACY_QUEUE_ONLY_REMAINING_ACTIONS,
    LEGACY_QUEUE_ONLY_SCHEMA,
    LEGACY_SOURCE_REVISION_GUARD_SCHEMA,
    LEGACY_SOURCE_REVISION_QUEUE_TRIGGERS,
    LEGACY_SOURCE_REVISION_TABLE_SQL_SHA256,
    LEGACY_SOURCE_REVISION_TABLE_XINFO,
    LegacyManualRestoreQueueOnlyIntent,
    LegacyQueueOnlyIntentError,
    build_legacy_dead_letter_guard,
    build_legacy_source_revision_guard,
    canonical_legacy_source_revision_trigger_sql_sha256,
    canonical_evidence_hash,
    validate_legacy_source_revision_phase,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    global_discovery_source_revision_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
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
ORIGINAL_QUARANTINE_ID = f"q_{'o' * 22}"
MANUAL_QUARANTINE_ID = f"q_{'m' * 22}"
REPORT_ID = "report_1b4dd579136d415c9d5225ccc8654201"
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
DLQ_COLUMNS = LEGACY_DEAD_LETTER_COLUMNS
CHECKPOINT_STARTED_AT = "2026-08-15T02:32:51+00:00"


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


def _historical_errors(attempts: int) -> str:
    return json.dumps(
        [
            {
                "attempt": attempt,
                "occurred_at": f"2026-08-{attempt:02d}T02:15:52+00:00",
                "error_type": "ValueError",
                "message": "legacy invalid payload",
                "traceback": None,
                "recovery_class": "invalid_payload",
                "reason_code": "kg_recovery.invalid_payload",
                "replay_safe": False,
                "correlation_id": f"00000000-0000-4000-8000-{attempt:012d}",
            }
            for attempt in range(1, attempts + 1)
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _historical_peer(
    row: dict[str, object],
    *,
    peer_id: str = "a6d88e39-3ad8-4a55-b6c9-15ab8afcc814",
    original_queue_id: str = "2626ab17-eda9-4b6b-85c9-af3c38c9650a",
    created_at: str = "2026-08-13T02:15:52+00:00",
    dead_lettered_at: str = "2026-08-13T02:15:52+00:00",
) -> dict[str, object]:
    return {
        "id": peer_id,
        "board_id": BOARD_ID,
        "artifact_type": row["artifact_type"],
        "artifact_id": row["artifact_id"],
        "original_queue_id": original_queue_id,
        "attempts": 2,
        "errors": _historical_errors(2),
        "dead_lettered_at": dead_lettered_at,
        "created_at": created_at,
    }


def _insert_dlq(path: Path, row: dict[str, object]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO consolidation_dead_letter ({','.join(DLQ_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in DLQ_COLUMNS)})",
            tuple(row[column] for column in DLQ_COLUMNS),
        )


def _effect_artifact(effect_key: str, digest: str) -> dict[str, str]:
    effect_id = hashlib.sha256(effect_key.encode("utf-8")).hexdigest()[:24]
    return {
        "effect_key": effect_key,
        "relative": f"audit/f06-effect-{effect_id}.json",
        "sha256": digest * 64,
    }


def _install_source_revision_guard(connection: sqlite3.Connection) -> None:
    """Install the exact production GDSR manifest over the bounded fixture."""

    connection.execute(
        "CREATE TABLE global_discovery_source_revision ("
        "scope_id VARCHAR(64) NOT NULL, "
        "fence_version VARCHAR(64) NOT NULL, "
        "trigger_manifest_version VARCHAR(64) NOT NULL, "
        "incarnation_id VARCHAR(64) NOT NULL, "
        "revision BIGINT DEFAULT 0 NOT NULL, "
        "mutation_nonce VARCHAR(64) NOT NULL, "
        "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
        "PRIMARY KEY (scope_id), "
        "CONSTRAINT ck_global_discovery_source_revision_global_scope "
        "CHECK (scope_id = '_global'), "
        "CONSTRAINT ck_global_discovery_source_revision_nonnegative "
        "CHECK (revision >= 0))"
    )
    connection.execute(
        "CREATE UNIQUE INDEX uq_global_discovery_source_revision_scope "
        "ON global_discovery_source_revision (scope_id)"
    )
    for table_name in GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES:
        exists = connection.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if exists is None:
            if table_name == "app_settings":
                connection.execute(
                    "CREATE TABLE app_settings ("
                    "key VARCHAR(64) NOT NULL PRIMARY KEY, "
                    "value VARCHAR(64) NOT NULL)"
                )
            else:
                connection.execute(f'CREATE TABLE "{table_name}" (id TEXT)')
    connection.execute(
        "INSERT INTO global_discovery_source_revision "
        "(scope_id,fence_version,trigger_manifest_version,incarnation_id,"
        "revision,mutation_nonce,updated_at) VALUES (?,?,?,?,?,?,?)",
        (
            GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
            GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
            GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
            "1" * 64,
            100,
            "2" * 64,
            "2026-08-15 11:38:48",
        ),
    )
    for _name, (_table_name, sql) in sorted(
        global_discovery_source_revision_trigger_manifest().items()
    ):
        connection.execute(sql)


def _intent(
    db_path: Path,
    target_rows: list[dict[str, object]],
    *,
    non_target_rows: list[dict[str, object]],
    dlq_rows: list[dict[str, object]],
) -> LegacyManualRestoreQueueOnlyIntent:
    memberships = [_membership(row, index) for index, row in enumerate(target_rows)]
    dead_letter_guard = build_legacy_dead_letter_guard(
        board_id=BOARD_ID,
        checkpoint_started_at=CHECKPOINT_STARTED_AT,
        target_rows=target_rows,
        dlq_columns=DLQ_COLUMNS,
        dlq_rows=[tuple(row[column] for column in DLQ_COLUMNS) for row in dlq_rows],
    )
    with sqlite3.connect(db_path) as connection:
        source_revision_guard = build_legacy_source_revision_guard(
            baseline=read_legacy_source_revision_state(connection),
            expected_transition_count=len(target_rows),
        )
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
            "canonical_payload_sha256": "a" * 64,
            "preflight_hash": "4" * 64,
            "source_set_hash": "5" * 64,
            "created_at": "2026-08-15T02:29:00+00:00",
            "cognitive_cut": {
                "cutoff": "2026-08-15T02:29:00+00:00",
                "base_row_count": 1,
                "revision_row_count": 1,
                "count": 1,
                "digest": "b" * 64,
                "ledger_fingerprint": "c" * 64,
            },
        },
        "terminal_run": {
            "run_id": "run_legacy",
            "audit_relative": "audit/run_legacy.json",
            "audit_sha256": "6" * 64,
            "report_id": REPORT_ID,
            "report_relative": f"reports/{REPORT_ID}.json",
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
            "quarantine_id": ORIGINAL_QUARANTINE_ID,
            "manifest_relative": f"{ORIGINAL_QUARANTINE_ID}/manifest.json",
            "manifest_sha256": "a" * 64,
            "storage_hashes": {f"{ORIGINAL_QUARANTINE_ID}/graph.lbug": "b" * 64},
        },
        "manual_restore": {
            "quarantine_id": MANUAL_QUARANTINE_ID,
            "manifest_relative": f"{MANUAL_QUARANTINE_ID}/manifest.json",
            "manifest_sha256": "c" * 64,
            "journal_relative": f"{MANUAL_QUARANTINE_ID}/restore_operation.json",
            "journal_sha256": "d" * 64,
            "source_quarantine_id": ORIGINAL_QUARANTINE_ID,
            "storage_hashes": {
                f"{MANUAL_QUARANTINE_ID}/graph.lbug": "e" * 64,
                f"{MANUAL_QUARANTINE_ID}/graph.lbug.wal": "f" * 64,
            },
        },
        "board_storage": {
            "hashes": {"graph.lbug": "0" * 64},
            "sha256": canonical_evidence_hash({"graph.lbug": "0" * 64}),
        },
        "prefix_schema": LEGACY_QUEUE_ONLY_PREFIX_SCHEMA,
        "dead_letter_guard": dead_letter_guard,
        "source_revision_guard": source_revision_guard,
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
            "id": "08949856-fbed-4d68-8425-2d6f04725045",
            "board_id": BOARD_ID,
            "artifact_type": "story",
            "artifact_id": "story-unrelated",
            "original_queue_id": "old-unrelated",
            "attempts": 5,
            "errors": "[]",
            "dead_lettered_at": "2026-08-01 00:00:00",
            "created_at": "2026-08-01 00:00:00",
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
            "errors TEXT, dead_lettered_at TEXT, created_at TEXT)"
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
        _install_source_revision_guard(connection)
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


def _source_revision_state(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        return read_legacy_source_revision_state(connection)


def _raw_source_revision_state(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute(
                "SELECT scope_id,fence_version,trigger_manifest_version,"
                "incarnation_id,revision,mutation_nonce,updated_at "
                "FROM global_discovery_source_revision"
            ).fetchone()
        )


def _restore_source_revision_baseline(
    path: Path,
    intent: LegacyManualRestoreQueueOnlyIntent,
) -> None:
    baseline = dict(intent.payload["source_revision_guard"]["baseline"])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE global_discovery_source_revision SET "
            "fence_version=?,trigger_manifest_version=?,incarnation_id=?,"
            "revision=?,mutation_nonce=?,updated_at=? WHERE scope_id=?",
            (
                baseline["fence_version"],
                baseline["trigger_manifest_version"],
                baseline["incarnation_id"],
                baseline["revision"],
                baseline["mutation_nonce"],
                baseline["updated_at"],
                baseline["scope_id"],
            ),
        )


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
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    adapter = CommunityBoardRebuildIngestionAdapter(db_path=path)
    guard_calls: list[int] = []
    revision_before = _source_revision_state(path)

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
    revision_after = _source_revision_state(path)
    assert revision_after["revision"] == int(revision_before["revision"]) + 2
    assert revision_after["mutation_nonce"] != revision_before["mutation_nonce"]
    assert str(revision_after["updated_at"]) >= str(revision_before["updated_at"])
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
            "SELECT * FROM consolidation_dead_letter "
            "WHERE id='08949856-fbed-4d68-8425-2d6f04725045'"
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
    assert _source_revision_state(path) == revision_after


def test_source_revision_guard_v1_binds_exact_contract_and_row_cardinality(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    payload = intent.to_payload()
    guard = dict(payload["source_revision_guard"])

    assert payload["schema_version"] == "legacy_manual_restore_queue_only.v4"
    assert guard["schema_version"] == LEGACY_SOURCE_REVISION_GUARD_SCHEMA
    assert (
        guard["expected_transition_count"]
        == len(target_rows)
        == len(intent.memberships)
    )
    assert guard["revision_table_xinfo"] == [
        {
            key: value
            for key, value in zip(
                ("cid", "name", "type", "notnull", "default", "pk", "hidden"),
                row,
                strict=True,
            )
        }
        for row in LEGACY_SOURCE_REVISION_TABLE_XINFO
    ]
    assert guard["revision_table_sql_sha256"] == (
        LEGACY_SOURCE_REVISION_TABLE_SQL_SHA256
    )
    assert [
        (
            trigger["name"],
            trigger["table"],
            trigger["timing"],
            trigger["operation"],
            trigger["normalized_sql_sha256"],
        )
        for trigger in guard["queue_triggers"]
    ] == list(LEGACY_SOURCE_REVISION_QUEUE_TRIGGERS)
    manifest = global_discovery_source_revision_trigger_manifest()
    assert all(
        canonical_legacy_source_revision_trigger_sql_sha256(manifest[name][1]) == digest
        for name, _table, _timing, _operation, digest in (
            LEGACY_SOURCE_REVISION_QUEUE_TRIGGERS
        )
    )

    mutations = (
        lambda value: value["source_revision_guard"].__setitem__(
            "expected_transition_count", True
        ),
        lambda value: value["source_revision_guard"].__setitem__(
            "expected_transition_count", 1
        ),
        lambda value: value["source_revision_guard"]["revision_table_xinfo"][
            0
        ].__setitem__("cid", False),
        lambda value: value["source_revision_guard"]["revision_table_indexes"][
            0
        ].__setitem__("unique", True),
        lambda value: value["source_revision_guard"]["queue_triggers"][2].__setitem__(
            "operation", "INSERT"
        ),
        lambda value: value["source_revision_guard"].__setitem__(
            "revision_table_sql_sha256", "0" * 64
        ),
    )
    for mutate in mutations:
        tampered = copy.deepcopy(payload)
        mutate(tampered)
        with pytest.raises(
            LegacyQueueOnlyIntentError,
            match="source_revision_guard_invalid",
        ):
            LegacyManualRestoreQueueOnlyIntent.from_payload(tampered)


def test_source_revision_guard_phase_matrix_is_exact(tmp_path: Path) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    guard = dict(intent.payload["source_revision_guard"])
    baseline = dict(guard["baseline"])
    expected_count = len(target_rows)
    assert (
        validate_legacy_source_revision_phase(
            guard,
            current=baseline,
            terminal_count=0,
        )
        == "baseline"
    )
    terminal = dict(baseline)
    terminal["revision"] = int(baseline["revision"]) + expected_count
    terminal["mutation_nonce"] = "3" * 64
    assert (
        validate_legacy_source_revision_phase(
            guard,
            current=terminal,
            terminal_count=expected_count,
        )
        == "terminal"
    )

    invalid: list[tuple[dict[str, object], int]] = []
    invalid.append((dict(baseline), 1))
    drift_at_baseline = dict(baseline)
    drift_at_baseline["revision"] = int(baseline["revision"]) + 1
    invalid.append((drift_at_baseline, 0))
    wrong_revision = dict(terminal)
    wrong_revision["revision"] = int(terminal["revision"]) - 1
    invalid.append((wrong_revision, expected_count))
    same_nonce = dict(terminal)
    same_nonce["mutation_nonce"] = baseline["mutation_nonce"]
    invalid.append((same_nonce, expected_count))
    earlier = dict(terminal)
    earlier["updated_at"] = "2026-08-15 11:38:47"
    invalid.append((earlier, expected_count))
    wrong_incarnation = dict(terminal)
    wrong_incarnation["incarnation_id"] = "4" * 64
    invalid.append((wrong_incarnation, expected_count))
    for current, terminal_count in invalid:
        with pytest.raises(
            LegacyQueueOnlyIntentError,
            match="source_revision_transition_invalid",
        ):
            validate_legacy_source_revision_phase(
                guard,
                current=current,
                terminal_count=terminal_count,
            )


def test_legacy_queue_only_cas_drift_rolls_back_every_target(tmp_path: Path) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE consolidation_queue SET priority='low' WHERE id='queue-claimed'"
        )
    before = _queue_state(path)
    revision_before = _source_revision_state(path)

    with pytest.raises(RuntimeError, match="row_cas_conflict"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before
    assert _source_revision_state(path) == revision_before
    assert all(row[10] in {"pending", "claimed"} for row in before[:2])


def test_legacy_queue_only_guard_loss_before_second_update_rolls_back(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    before = _queue_state(path)
    revision_before = _source_revision_state(path)
    calls = 0

    def guard() -> bool:
        nonlocal calls
        calls += 1
        return calls < 4

    with pytest.raises(
        RuntimeError,
        match="mutation_guard_lost:before_update:queue-pending",
    ):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=guard,
        )

    assert _queue_state(path) == before
    assert _source_revision_state(path) == revision_before


def test_source_revision_reader_accepts_tuple_connection_and_restores_factory(
    tmp_path: Path,
) -> None:
    path, _target_rows, _non_target_rows, _dlq_rows = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.row_factory is None
        state = read_legacy_source_revision_state(connection)
        assert connection.row_factory is None
    assert state == {
        "scope_id": GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
        "fence_version": GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
        "trigger_manifest_version": GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
        "incarnation_id": "1" * 64,
        "revision": 100,
        "mutation_nonce": "2" * 64,
        "updated_at": "2026-08-15 11:38:48",
    }


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_queue_trigger",
        "missing_owned_peer_trigger",
        "string_literal_case_collision",
        "case_variant_attached_trigger",
        "revision_table_extra_column",
        "revision_table_missing_index",
    ),
)
def test_source_revision_catalog_or_schema_drift_refuses_before_cas(
    tmp_path: Path,
    mutation: str,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    manifest = global_discovery_source_revision_trigger_manifest()
    update_name = "trg_global_discovery_source_revision_consolidation_queue_update"
    with sqlite3.connect(path) as connection:
        if mutation == "missing_queue_trigger":
            connection.execute(f'DROP TRIGGER "{update_name}"')
        elif mutation == "missing_owned_peer_trigger":
            connection.execute(
                'DROP TRIGGER "trg_global_discovery_source_revision_boards_insert"'
            )
        elif mutation == "string_literal_case_collision":
            connection.execute(f'DROP TRIGGER "{update_name}"')
            connection.execute(
                manifest[update_name][1].replace("'_global'", "'_GLOBAL'")
            )
        elif mutation == "case_variant_attached_trigger":
            connection.execute(
                "CREATE TRIGGER case_variant_queue_side_effect AFTER UPDATE ON "
                '"Consolidation_Queue" BEGIN SELECT 1; END'
            )
        elif mutation == "revision_table_extra_column":
            connection.execute(
                "ALTER TABLE global_discovery_source_revision "
                "ADD COLUMN unexpected TEXT"
            )
        else:
            connection.execute("DROP INDEX uq_global_discovery_source_revision_scope")
    before_queue = _queue_state(path)
    before_revision = _raw_source_revision_state(path)

    with pytest.raises(RuntimeError, match="source_revision_guard_invalid"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before_queue
    assert _raw_source_revision_state(path) == before_revision


def test_source_revision_rejects_chained_singleton_trigger_before_side_effect(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO app_settings (key,value) VALUES ('mode','safe')"
        )
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER mutate_app_settings_from_revision "
            "AFTER UPDATE ON global_discovery_source_revision BEGIN "
            "UPDATE app_settings SET value='unsafe' WHERE key='mode'; END"
        )
    before_queue = _queue_state(path)
    before_revision = _raw_source_revision_state(path)

    with pytest.raises(RuntimeError, match="source_revision_guard_invalid"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before_queue
    assert _raw_source_revision_state(path) == before_revision
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM app_settings WHERE key='mode'"
        ).fetchone() == ("safe",)


def test_source_revision_singleton_drift_refuses_before_cas(tmp_path: Path) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE global_discovery_source_revision SET "
            "revision=revision+1,mutation_nonce=?",
            ("4" * 64,),
        )
    before_queue = _queue_state(path)
    before_revision = _raw_source_revision_state(path)

    with pytest.raises(RuntimeError, match="source_revision_transition_invalid"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before_queue
    assert _raw_source_revision_state(path) == before_revision


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
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER mutate_other_board_queue "
            "AFTER UPDATE ON consolidation_queue "
            "WHEN NEW.id='queue-pending' BEGIN "
            "UPDATE consolidation_queue SET priority='low' "
            "WHERE id='queue-other-board'; END"
        )
    before = _queue_state(path)

    with pytest.raises(RuntimeError, match="source_revision_guard_invalid"):
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
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
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
    before_queue = _queue_state(path)
    before_dlq = _dlq_state(path)

    with pytest.raises(RuntimeError, match="source_revision_guard_invalid"):
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
            "INSERT INTO app_settings (key, value) VALUES ('mode', 'safe')"
        )
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER mutate_app_settings "
            "AFTER UPDATE ON consolidation_queue "
            "WHEN NEW.id='queue-pending' BEGIN "
            "UPDATE app_settings SET value='unsafe' WHERE key='mode'; END"
        )

    with pytest.raises(RuntimeError, match="source_revision_guard_invalid"):
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
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
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
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=dlq_rows,
    )
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
        path,
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


def test_legacy_queue_only_cas_preserves_guarded_historical_dlq_peer(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    alias = _historical_peer(target_rows[0])
    _insert_dlq(path, alias)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=[*dlq_rows, alias],
    )
    before_dlq = _dlq_state(path)

    result = CommunityBoardRebuildIngestionAdapter(
        db_path=path
    ).compensate_legacy_manual_restore_queue_only(
        intent_payload=intent.to_payload(),
        mutation_guard=lambda: True,
    )

    assert result["active_remaining"] == 0
    assert _dlq_state(path) == before_dlq


def test_fresh_lane_baselines_guarded_dlq_and_blocks_only_new_ids(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    peer = _historical_peer(target_rows[0])
    _insert_dlq(path, peer)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=[*dlq_rows, peer],
    )
    adapter = CommunityBoardRebuildIngestionAdapter(db_path=path)
    adapter.compensate_legacy_manual_restore_queue_only(
        intent_payload=intent.to_payload(),
        mutation_guard=lambda: True,
    )

    baseline = adapter.dead_letter_ids(BOARD_ID)
    assert baseline == tuple(sorted((str(dlq_rows[0]["id"]), str(peer["id"]))))
    assert adapter.queue_observation(
        BOARD_ID,
        run_id="fresh-manifest",
        baseline_dead_letter_ids=baseline,
    ) == (0, None)

    extra = {
        "id": "508f2f43-521d-4b3f-9b64-e99b9f3e7828",
        "board_id": BOARD_ID,
        "artifact_type": "story",
        "artifact_id": "story-extra",
        "original_queue_id": "queue-extra-old",
        "attempts": 1,
        "errors": "[]",
        "dead_lettered_at": "2026-08-15T03:00:00+00:00",
        "created_at": "2026-08-15T03:00:00+00:00",
    }
    _insert_dlq(path, extra)
    assert adapter.queue_observation(
        BOARD_ID,
        run_id="fresh-manifest",
        baseline_dead_letter_ids=baseline,
    ) == (0, "rebuild_new_dead_letter")


def test_legacy_queue_only_intent_refuses_nonhistorical_dlq_peer(
    tmp_path: Path,
) -> None:
    _path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    late = _historical_peer(
        target_rows[0],
        dead_lettered_at="2026-08-15T02:32:51+00:00",
    )

    with pytest.raises(
        LegacyQueueOnlyIntentError,
        match="dead_letter_peer_not_historical",
    ):
        _intent(
            _path,
            target_rows,
            non_target_rows=non_target_rows,
            dlq_rows=[*dlq_rows, late],
        )


@pytest.mark.parametrize(
    "mutation",
    ("missing", "tampered", "extra"),
)
def test_legacy_queue_only_cas_refuses_dead_letter_guard_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    peer = _historical_peer(target_rows[0])
    _insert_dlq(path, peer)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=[*dlq_rows, peer],
    )
    if mutation == "missing":
        with sqlite3.connect(path) as connection:
            connection.execute(
                "DELETE FROM consolidation_dead_letter WHERE id=?",
                (peer["id"],),
            )
    elif mutation == "tampered":
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE consolidation_dead_letter SET errors='[]' WHERE id=?",
                (peer["id"],),
            )
    else:
        extra = {
            "id": "508f2f43-521d-4b3f-9b64-e99b9f3e7828",
            "board_id": BOARD_ID,
            "artifact_type": "story",
            "artifact_id": "story-extra",
            "original_queue_id": "queue-extra-old",
            "attempts": 1,
            "errors": "[]",
            "dead_lettered_at": "2026-08-01T00:00:00+00:00",
            "created_at": "2026-08-01T00:00:00+00:00",
        }
        _insert_dlq(path, extra)
    _restore_source_revision_baseline(path, intent)
    before = _queue_state(path)

    with pytest.raises(RuntimeError, match="dlq_(?:guard_invalid|changed)"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before


def test_legacy_queue_only_cas_refuses_live_original_queue_for_dlq_peer(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    peer = _historical_peer(target_rows[0])
    _insert_dlq(path, peer)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=[*dlq_rows, peer],
    )
    original = _row(
        str(peer["original_queue_id"]),
        "story",
        "story-other-board",
        status="pending",
        source="live-intent",
    )
    original["board_id"] = "0d7c469e-9db1-4d26-a9ea-c80a7deaa770"
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO consolidation_queue ({','.join(QUEUE_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in QUEUE_COLUMNS)})",
            tuple(original[column] for column in QUEUE_COLUMNS),
        )
    _restore_source_revision_baseline(path, intent)
    before = _queue_state(path)

    with pytest.raises(RuntimeError, match="dlq_guard_invalid"):
        CommunityBoardRebuildIngestionAdapter(
            db_path=path
        ).compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before


def test_legacy_queue_only_bounds_full_board_dlq_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import board_rebuild_ingestion

    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    extra = {
        "id": "508f2f43-521d-4b3f-9b64-e99b9f3e7828",
        "board_id": BOARD_ID,
        "artifact_type": "story",
        "artifact_id": "story-extra",
        "original_queue_id": "queue-extra-old",
        "attempts": 1,
        "errors": "[]",
        "dead_lettered_at": "2026-08-01T00:00:00+00:00",
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    _insert_dlq(path, extra)
    intent = _intent(
        path,
        target_rows,
        non_target_rows=non_target_rows,
        dlq_rows=[*dlq_rows, extra],
    )
    before = _queue_state(path)
    monkeypatch.setattr(board_rebuild_ingestion, "_MAX_LEGACY_PROTECTED_ROWS", 1)

    with pytest.raises(RuntimeError, match="before_updates_dlq_row_limit_exceeded"):
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
        path,
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


def test_legacy_queue_only_cas_refuses_impossible_mixed_active_and_terminal(
    tmp_path: Path,
) -> None:
    path, target_rows, non_target_rows, dlq_rows = _database(tmp_path)
    intent = _intent(
        path,
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
    before = _queue_state(path)
    revision_before = _source_revision_state(path)

    with pytest.raises(RuntimeError, match="source_revision_transition_invalid"):
        adapter.compensate_legacy_manual_restore_queue_only(
            intent_payload=intent.to_payload(),
            mutation_guard=lambda: True,
        )

    assert _queue_state(path) == before
    assert _source_revision_state(path) == revision_before


@pytest.mark.parametrize(
    ("path", "replacement", "error"),
    (
        (("checkpoint", "relative"), "audit/f06-checkpoint-x/evil.json", "checkpoint"),
        (("manifest_ref",), "rebuild_manifest_x/evil", "manifest_ref"),
        (("terminal_run", "run_id"), "run_x/evil", "run_id"),
        (("terminal_run", "report_id"), "report_x/evil", "report_id"),
        (("terminal_run", "report_id"), f"report_{'a' * 32}", "terminal_run"),
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
        _db_path,
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
        path,
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
        path,
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
        _db_path,
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
