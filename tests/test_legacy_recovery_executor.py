from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import replace
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community import kg_recovery_only as recovery
from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
    read_legacy_source_revision_state,
)
from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
    LEGACY_DEAD_LETTER_COLUMNS,
    LEGACY_QUEUE_COLUMNS,
    LegacyManualRestoreQueueOnlyIntent,
    canonical_evidence_hash,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    global_discovery_source_revision_trigger_manifest,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
)


BOARD_ID = "15877207-c147-4805-96d7-d53a625571df"
MANIFEST_REF = "rebuild_manifest_legacy_executor"
F06_RUN_ID = f"f06:{MANIFEST_REF}"
CANDIDATE_ID = "c1acd7b9-2a50-4f72-a228-633470389c66"
CONTENT_HASH = "a" * 64
PREFLIGHT_HASH = "b" * 64
CONFIRMATION_REF = f"conf_fp_{'d' * 64}"
MANIFEST_CREATED_AT = "2026-08-15T02:29:00+00:00"
REPORT_ID = "report_1b4dd579136d415c9d5225ccc8654201"
ORIGINAL_QUARANTINE_ID = f"q_{'o' * 22}"
MANUAL_QUARANTINE_ID = f"q_{'m' * 22}"
HISTORICAL_DLQ_ID = "08949856-fbed-4d68-8425-2d6f04725045"
HISTORICAL_ORIGINAL_QUEUE_ID = "2626ab17-eda9-4b6b-85c9-af3c38c9650a"
COGNITIVE_SOURCE_ID = "11111111-1111-4111-8111-111111111111"
COGNITIVE_REVISION_ID = "22222222-2222-4222-8222-222222222222"
COGNITIVE_LATE_REVISION_ID = "33333333-3333-4333-8333-333333333333"
COGNITIVE_NODE_ID = "decision_legacy_manifest_fixture"
POST_LEGACY_CHECKPOINT_FIELDS = (
    "writer_handoff_count",
    "writer_reacquire_count",
    "compensation_failed_state",
    "compensation_failure_code",
    "compensation_failure_detail",
    "compensation_actions",
)
STREAMING_LOGICAL_TABLES = tuple(recovery.SQLITE_LOGICAL_STREAMING_POLICIES)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _effect_relative(effect_key: str) -> str:
    digest = hashlib.sha256(effect_key.encode()).hexdigest()[:24]
    return f"audit/f06-effect-{digest}.json"


def _receipt(
    effect_key: str,
    effect: str,
    *,
    details: dict[str, object],
    code: str = "ok",
) -> dict[str, object]:
    return {
        "effect_key": effect_key,
        "effect": effect,
        "ok": True,
        "code": code,
        "details": details,
    }


def _source_payload(index: int) -> dict[str, object]:
    artifact_id = "spec-legacy" if index == 0 else f"spec-legacy-{index:04d}"
    payload = {
        "artifact_type": "spec",
        "id": artifact_id,
        "source_ref": f"spec:{artifact_id}",
        "source_version": str(index + 1),
        "content_hash": (
            CONTENT_HASH
            if index == 0
            else hashlib.sha256(artifact_id.encode()).hexdigest()
        ),
        "created_at": "2026-08-01T00:00:00+00:00",
        "source_artifact_status": "active",
        "graph_layer": "canonical",
        "maturity_status": "canonical_eligible",
        "disposition": "canonical",
        "reason_code": "",
        "expires_at": None,
    }
    return payload


def _cognitive_record(
    *,
    payload: dict[str, object],
    committed_at: str,
    source_revision: int,
) -> dict[str, object]:
    from okto_pulse.core.ports.kg_cognitive_source import (
        canonical_cognitive_source_fingerprint,
    )

    evidence_refs = ("spec:legacy-manifest",)
    fingerprint = canonical_cognitive_source_fingerprint(
        board_id=BOARD_ID,
        node_id=COGNITIVE_NODE_ID,
        node_type="Decision",
        generation=0,
        payload=payload,
        evidence_refs=evidence_refs,
    )
    return {
        "board_id": BOARD_ID,
        "node_id": COGNITIVE_NODE_ID,
        "node_type": "Decision",
        "generation": 0,
        "payload": payload,
        "evidence_refs": evidence_refs,
        "committed_at": committed_at,
        "source_revision": source_revision,
        "record_fingerprint": fingerprint if source_revision else "",
    }


def _historical_cognitive_records() -> tuple[dict[str, object], ...]:
    return (
        _cognitive_record(
            payload={"title": "base"},
            committed_at="2026-08-15T02:27:00.000000",
            source_revision=0,
        ),
        _cognitive_record(
            payload={"title": "historical"},
            committed_at="2026-08-15T02:28:00.000000",
            source_revision=1,
        ),
    )


def _manifest_fixture(source_count: int = 1):  # noqa: ANN202
    from okto_pulse.core.kg.rebuild_sources import (
        RebuildSourceManifest,
        RebuildSourceRow,
        RebuildSourceSet,
        _compose_source_set_hash,
        cognitive_durable_digest_from_rows,
    )

    rows = tuple(
        RebuildSourceRow(**_source_payload(index)) for index in range(source_count)
    )
    cognitive_digest = cognitive_durable_digest_from_rows(
        _historical_cognitive_records()
    )
    source_set = RebuildSourceSet(
        board_id=BOARD_ID,
        sources=rows,
        skipped_cancelled_count=0,
        has_non_deterministic_inputs=False,
        generated_at=MANIFEST_CREATED_AT,
        cognitive_durable_digest=cognitive_digest,
    )
    manifest = RebuildSourceManifest(
        manifest_ref=MANIFEST_REF,
        board_id=BOARD_ID,
        source_set_hash=_compose_source_set_hash(source_set),
        preflight_hash=PREFLIGHT_HASH,
        sources=rows,
        skipped_cancelled_count=0,
        has_non_deterministic_inputs=False,
        created_at=MANIFEST_CREATED_AT,
        manifest_schema_version=3,
    )
    payload = manifest.to_dict()
    payload.pop("payload_digest", None)
    return manifest, payload


def _manifest_store(data_home: Path):  # noqa: ANN202
    from okto_pulse.core.kg.rebuild_sources import KGRebuildSourceManifest

    return KGRebuildSourceManifest(
        artifact_store=CommunityFileSystemRebuildAuditArtifactStore(data_home)
    )


def _install_source_revision_guard(connection: sqlite3.Connection) -> None:
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


def _create_queue_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": "queue-legacy-spec",
        "board_id": BOARD_ID,
        "artifact_type": "spec",
        "artifact_id": "spec-legacy",
        "work_kind": "consolidate",
        "generation": 0,
        "payload": None,
        "delete_event_id": None,
        "priority": "high",
        "source": f"rebuild:{MANIFEST_REF}",
        "status": "claimed",
        "triggered_at": "2026-08-15 02:32:52",
        "triggered_by_event": None,
        "claimed_by_session_id": "legacy-worker",
        "claim_token": "1" * 32,
        "claimed_at": "2026-08-15 02:33:00+00:00",
        "last_error": None,
        "worker_id": "legacy-worker",
        "claim_timeout_at": "2026-08-15 02:38:00+00:00",
        "attempts": 0,
        "next_retry_at": None,
    }
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
        placeholders = ",".join("?" for _ in LEGACY_QUEUE_COLUMNS)
        connection.execute(
            f"INSERT INTO consolidation_queue ({','.join(LEGACY_QUEUE_COLUMNS)}) "
            f"VALUES ({placeholders})",
            tuple(row[column] for column in LEGACY_QUEUE_COLUMNS),
        )
        connection.execute(
            "CREATE TABLE kg_cognitive_sources ("
            "id VARCHAR(36) PRIMARY KEY NOT NULL, board_id VARCHAR(36) NOT NULL, "
            "node_id VARCHAR(64) NOT NULL, node_type VARCHAR(50) NOT NULL, "
            "generation INTEGER NOT NULL DEFAULT 0, payload JSON NOT NULL, "
            "evidence_refs JSON NOT NULL, source_session_id VARCHAR(36), "
            "committed_at DATETIME NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE kg_cognitive_source_revisions ("
            "id VARCHAR(36) PRIMARY KEY NOT NULL, "
            "cognitive_source_id VARCHAR(36) NOT NULL, "
            "source_revision INTEGER NOT NULL, "
            "record_fingerprint VARCHAR(64) NOT NULL, payload JSON NOT NULL, "
            "evidence_refs JSON NOT NULL, source_session_id VARCHAR(36), "
            "committed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        historical = _historical_cognitive_records()
        base = historical[0]
        connection.execute(
            "INSERT INTO kg_cognitive_sources "
            "(id,board_id,node_id,node_type,generation,payload,evidence_refs,"
            "source_session_id,committed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                COGNITIVE_SOURCE_ID,
                BOARD_ID,
                COGNITIVE_NODE_ID,
                "Decision",
                0,
                json.dumps(base["payload"], sort_keys=True, separators=(",", ":")),
                json.dumps(base["evidence_refs"], separators=(",", ":")),
                "kgses_1111111111111111",
                "2026-08-15 02:27:00.000000",
            ),
        )
        late = _cognitive_record(
            payload={"title": "late"},
            committed_at="2026-08-15T02:31:00.000000",
            source_revision=2,
        )
        for revision_id, record, committed_at in (
            (
                COGNITIVE_REVISION_ID,
                historical[1],
                "2026-08-15 02:28:00.000000",
            ),
            (
                COGNITIVE_LATE_REVISION_ID,
                late,
                "2026-08-15 02:31:00.000000",
            ),
        ):
            connection.execute(
                "INSERT INTO kg_cognitive_source_revisions "
                "(id,cognitive_source_id,source_revision,record_fingerprint,"
                "payload,evidence_refs,source_session_id,committed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    COGNITIVE_SOURCE_ID,
                    record["source_revision"],
                    record["record_fingerprint"],
                    json.dumps(
                        record["payload"], sort_keys=True, separators=(",", ":")
                    ),
                    json.dumps(record["evidence_refs"], separators=(",", ":")),
                    "kgses_2222222222222222",
                    committed_at,
                ),
            )
        _install_source_revision_guard(connection)


def _insert_second_legacy_target(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        values = connection.execute(
            "SELECT " + ",".join(LEGACY_QUEUE_COLUMNS) + " "
            "FROM consolidation_queue WHERE id='queue-legacy-spec'"
        ).fetchone()
        assert values is not None
        row = dict(zip(LEGACY_QUEUE_COLUMNS, values, strict=True))
        row.update(
            {
                "id": "queue-legacy-pending-0001",
                "artifact_id": "spec-legacy-0001",
                "status": "pending",
                "claimed_by_session_id": None,
                "claim_token": None,
                "claimed_at": None,
                "worker_id": None,
                "claim_timeout_at": None,
            }
        )
        connection.execute(
            "INSERT INTO consolidation_queue ("
            + ",".join(LEGACY_QUEUE_COLUMNS)
            + ") VALUES ("
            + ",".join("?" for _ in LEGACY_QUEUE_COLUMNS)
            + ")",
            tuple(row[column] for column in LEGACY_QUEUE_COLUMNS),
        )


def _replace_queue_row(path: Path, row: dict[str, object]) -> None:
    columns = tuple(column for column in LEGACY_QUEUE_COLUMNS if column != "id")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE consolidation_queue SET "
            + ",".join(f'"{column}"=?' for column in columns)
            + " WHERE id=?",
            (*(row[column] for column in columns), row["id"]),
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


def _raw_source_revision_state(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute(
                "SELECT scope_id,fence_version,trigger_manifest_version,"
                "incarnation_id,revision,mutation_nonce,updated_at "
                "FROM global_discovery_source_revision"
            ).fetchone()
        )


def _insert_historical_target_dlq_peer(path: Path) -> None:
    errors = json.dumps(
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
            for attempt in range(1, 3)
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    row = {
        "id": HISTORICAL_DLQ_ID,
        "board_id": BOARD_ID,
        "artifact_type": "spec",
        "artifact_id": "spec-legacy",
        "original_queue_id": HISTORICAL_ORIGINAL_QUEUE_ID,
        "attempts": 2,
        "errors": errors,
        "dead_lettered_at": "2026-08-13T02:15:52+00:00",
        "created_at": "2026-08-13T02:15:52+00:00",
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO consolidation_dead_letter "
            f"({','.join(LEGACY_DEAD_LETTER_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in LEGACY_DEAD_LETTER_COLUMNS)})",
            tuple(row[column] for column in LEGACY_DEAD_LETTER_COLUMNS),
        )


def test_legacy_queue_evidence_uses_one_wal_snapshot_for_revision_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import board_rebuild_ingestion

    db_path = tmp_path / "pulse.db"
    _create_queue_database(db_path)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    original = board_rebuild_ingestion.read_legacy_source_revision_state
    interleaved: list[bool] = []

    def read_after_concurrent_commit(connection):  # noqa: ANN001, ANN202
        with sqlite3.connect(db_path, timeout=5.0) as writer:
            writer.execute(
                "UPDATE global_discovery_source_revision SET "
                "revision=revision+1,mutation_nonce=?",
                ("4" * 64,),
            )
        interleaved.append(True)
        return original(connection)

    monkeypatch.setattr(
        board_rebuild_ingestion,
        "read_legacy_source_revision_state",
        read_after_concurrent_commit,
    )
    evidence = recovery._legacy_queue_current_evidence(
        db_path,
        board_id=BOARD_ID,
        source=f"rebuild:{MANIFEST_REF}",
        source_rows=(_source_payload(0),),
        checkpoint_started_at="2026-08-15T02:32:51+00:00",
    )

    assert interleaved == [True]
    assert evidence[-1]["baseline"]["revision"] == 100
    assert _raw_source_revision_state(db_path)[4] == 101


def test_legacy_outer_sqlite_snapshots_refuse_unbounded_global_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "pulse.db"
    _create_queue_database(db_path)
    with sqlite3.connect(db_path) as connection:
        row = list(
            connection.execute(
                f"SELECT {','.join(LEGACY_QUEUE_COLUMNS)} "
                "FROM consolidation_queue LIMIT 1"
            ).fetchone()
        )
        row[0] = "queue-other-board"
        row[1] = "0d7c469e-9db1-4d26-a9ea-c80a7deaa770"
        connection.execute(
            f"INSERT INTO consolidation_queue ({','.join(LEGACY_QUEUE_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in LEGACY_QUEUE_COLUMNS)})",
            tuple(row),
        )
        connection.executemany(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            (("first", "one"), ("second", "two")),
        )

    with monkeypatch.context() as row_limit:
        row_limit.setattr(recovery, "MAX_LEGACY_PROTECTED_QUEUE_ROWS", 1)
        with pytest.raises(
            recovery.RecoveryRefused,
            match="sqlite_snapshot_row_limit_exceeded",
        ):
            recovery._full_queue_snapshot(db_path)
        with pytest.raises(
            recovery.RecoveryRefused,
            match="sqlite_logical_table_app_settings_row_limit_exceeded",
        ):
            recovery._sqlite_logical_fingerprints(
                db_path,
                exclude_tables=frozenset(
                    {"consolidation_queue", "consolidation_dead_letter"}
                ),
            )
    with monkeypatch.context() as object_limit:
        object_limit.setattr(recovery, "MAX_RECOVERY_SQLITE_SCHEMA_OBJECTS", 1)
        with pytest.raises(
            recovery.RecoveryRefused,
            match="sqlite_schema_inventory_row_limit_exceeded",
        ):
            recovery._sqlite_schema_fingerprint(db_path)
    with monkeypatch.context() as byte_limit:
        byte_limit.setattr(recovery, "MAX_LEGACY_PROTECTED_QUEUE_BYTES", 1)
        with pytest.raises(
            recovery.RecoveryRefused,
            match="sqlite_schema_inventory_byte_limit_exceeded",
        ):
            recovery._sqlite_schema_fingerprint(db_path)


def test_legacy_logical_fingerprint_has_nominal_cognitive_byte_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "pulse.db"
    _create_queue_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE kg_cognitive_source_revisions SET payload=? WHERE id=?",
            (json.dumps({"title": "x" * 4096}), COGNITIVE_LATE_REVISION_ID),
        )

    excluded = frozenset({"consolidation_queue", "consolidation_dead_letter"})
    with monkeypatch.context() as bounded:
        bounded.setattr(recovery, "MAX_LEGACY_PROTECTED_QUEUE_BYTES", 1024)
        policies = dict(recovery.SQLITE_LOGICAL_STREAMING_POLICIES)
        policies["kg_cognitive_source_revisions"] = replace(
            policies["kg_cognitive_source_revisions"],
            max_bytes=16_384,
        )
        bounded.setattr(recovery, "SQLITE_LOGICAL_STREAMING_POLICIES", policies)
        fingerprints = recovery._sqlite_logical_fingerprints(
            db_path,
            exclude_tables=excluded,
        )
        assert "kg_cognitive_source_revisions" in fingerprints

        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE unrelated_large (payload TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO unrelated_large (payload) VALUES (?)",
                ("x" * 4096,),
            )
        with pytest.raises(
            recovery.RecoveryRefused,
            match="sqlite_logical_table_unrelated_large_byte_limit_exceeded",
        ):
            recovery._sqlite_logical_fingerprints(
                db_path,
                exclude_tables=excluded,
            )


def _streaming_policy_create_sql(
    table_name: str,
    policy: recovery.SQLiteLogicalStreamingPolicy,
    *,
    primary_key: tuple[str, ...] | None = None,
) -> str:
    selected_primary_key = policy.primary_key if primary_key is None else primary_key
    assert len(selected_primary_key) == 1
    definitions: list[str] = []
    for name, declared_type, not_null, default, _pk, _hidden in policy.schema:
        definition = f'"{name}" {declared_type}'
        if not_null:
            definition += " NOT NULL"
        if default is not None:
            definition += f" DEFAULT {default}"
        if name in selected_primary_key:
            definition += " PRIMARY KEY"
        definitions.append(definition)
    return f'CREATE TABLE "{table_name}" ({", ".join(definitions)})'


def _streaming_policy_row(
    policy: recovery.SQLiteLogicalStreamingPolicy,
    index: int,
) -> tuple[object, ...]:
    values: list[object] = []
    for name, declared_type, _not_null, _default, pk, _hidden in policy.schema:
        if pk:
            value: object = f"{index:064x}"
        elif name == "board_id" or name == "anchor_board_id":
            value = BOARD_ID if index % 2 else "5dcb7b75-466f-4d1e-8893-3899a7cfacf0"
        elif declared_type in {"INTEGER", "BOOLEAN"}:
            value = index + 1
        elif declared_type == "FLOAT":
            value = float(index) + 0.25
        elif declared_type == "JSON":
            value = json.dumps(
                {"index": index, "column": name},
                sort_keys=True,
                separators=(",", ":"),
            )
        elif declared_type in {"DATETIME", "TIMESTAMP"}:
            value = f"2026-08-{index + 1:02d} 00:00:00.000000"
        elif name.endswith("digest") or name in {
            "record_fingerprint",
            "excerpt_hash",
        }:
            value = hashlib.sha256(f"{name}:{index}".encode()).hexdigest()
        else:
            value = f"{name}-{index}"
        values.append(value)
    return tuple(values)


def _quoted_columns(columns: tuple[str, ...]) -> str:
    return ",".join('"' + column + '"' for column in columns)


def _create_streaming_policy_database(
    path: Path,
    table_name: str,
    *,
    row_order: tuple[int, ...] = (1, 2),
    primary_key: tuple[str, ...] | None = None,
) -> tuple[tuple[object, ...], ...]:
    policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[table_name]
    rows = tuple(_streaming_policy_row(policy, index) for index in row_order)
    columns = tuple(column[0] for column in policy.schema)
    placeholders = ",".join("?" for _ in columns)
    with sqlite3.connect(path) as connection:
        connection.execute(
            _streaming_policy_create_sql(
                table_name,
                policy,
                primary_key=primary_key,
            )
        )
        connection.executemany(
            f'INSERT INTO "{table_name}" '
            f"({_quoted_columns(columns)}) "
            f"VALUES ({placeholders})",
            rows,
        )
    return rows


@pytest.mark.parametrize("table_name", STREAMING_LOGICAL_TABLES)
def test_streaming_logical_fingerprint_is_order_invariant_and_detects_each_row(
    tmp_path: Path,
    table_name: str,
) -> None:
    first_db = tmp_path / f"{table_name}-first.db"
    second_db = tmp_path / f"{table_name}-second.db"
    rows = _create_streaming_policy_database(
        first_db,
        table_name,
        row_order=(2, 1),
    )
    _create_streaming_policy_database(
        second_db,
        table_name,
        row_order=(1, 2),
    )
    baseline = recovery._sqlite_logical_fingerprints(first_db)[table_name]
    assert recovery._sqlite_logical_fingerprints(second_db)[table_name] == baseline
    with sqlite3.connect(second_db) as connection:
        connection.execute("VACUUM")
    assert recovery._sqlite_logical_fingerprints(second_db)[table_name] == baseline

    policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[table_name]
    columns = tuple(column[0] for column in policy.schema)
    mutable_index = next(
        index for index, column in enumerate(policy.schema) if column[4] == 0
    )
    primary_key_index = columns.index(policy.primary_key[0])
    with sqlite3.connect(second_db) as connection:
        connection.execute(
            f'UPDATE "{table_name}" SET "{columns[mutable_index]}"=? '
            f'WHERE "{policy.primary_key[0]}"=?',
            (
                _streaming_policy_row(policy, 99)[mutable_index],
                rows[0][primary_key_index],
            ),
        )
    target_changed = recovery._sqlite_logical_fingerprints(second_db)[table_name]
    assert target_changed != baseline

    with sqlite3.connect(second_db) as connection:
        connection.execute(
            f'UPDATE "{table_name}" SET "{columns[mutable_index]}"=? '
            f'WHERE "{policy.primary_key[0]}"=?',
            (
                _streaming_policy_row(policy, 100)[mutable_index],
                rows[1][primary_key_index],
            ),
        )
    assert recovery._sqlite_logical_fingerprints(second_db)[table_name] not in {
        baseline,
        target_changed,
    }


@pytest.mark.parametrize("table_name", STREAMING_LOGICAL_TABLES)
def test_streaming_logical_fingerprint_refuses_schema_and_primary_key_drift(
    tmp_path: Path,
    table_name: str,
) -> None:
    schema_db = tmp_path / f"{table_name}-schema.db"
    _create_streaming_policy_database(schema_db, table_name, row_order=(1,))
    with sqlite3.connect(schema_db) as connection:
        connection.execute(f'ALTER TABLE "{table_name}" ADD COLUMN unexpected TEXT')
    with pytest.raises(
        recovery.RecoveryRefused,
        match=rf"sqlite_logical_table_{table_name}_schema_invalid",
    ):
        recovery._sqlite_logical_fingerprints(schema_db)

    policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[table_name]
    replacement_primary_key = next(
        column[0]
        for column in policy.schema
        if column[0] not in policy.primary_key and column[2] == 1
    )
    primary_key_db = tmp_path / f"{table_name}-primary-key.db"
    _create_streaming_policy_database(
        primary_key_db,
        table_name,
        row_order=(1,),
        primary_key=(replacement_primary_key,),
    )
    with pytest.raises(
        recovery.RecoveryRefused,
        match=rf"sqlite_logical_table_{table_name}_primary_key_invalid",
    ):
        recovery._sqlite_logical_fingerprints(primary_key_db)


@pytest.mark.parametrize("table_name", STREAMING_LOGICAL_TABLES)
def test_streaming_logical_fingerprint_enforces_exact_row_and_byte_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table_name: str,
) -> None:
    db_path = tmp_path / f"{table_name}.db"
    rows = _create_streaming_policy_database(
        db_path,
        table_name,
        row_order=(1, 2, 3),
    )
    policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[table_name]
    with monkeypatch.context() as exact_rows:
        exact_rows.setattr(
            recovery,
            "SQLITE_LOGICAL_STREAMING_POLICIES",
            {table_name: replace(policy, max_rows=3)},
        )
        assert table_name in recovery._sqlite_logical_fingerprints(db_path)
    with monkeypatch.context() as bounded_rows:
        bounded_rows.setattr(
            recovery,
            "SQLITE_LOGICAL_STREAMING_POLICIES",
            {table_name: replace(policy, max_rows=2)},
        )
        with pytest.raises(
            recovery.RecoveryRefused,
            match=rf"sqlite_logical_table_{table_name}_row_limit_exceeded",
        ):
            recovery._sqlite_logical_fingerprints(db_path)

    exact_byte_count = sum(
        len(
            recovery._canonical_json_bytes(
                [recovery._normalize_sqlite_value(value) for value in row]
            )
        )
        for row in rows
    )
    with monkeypatch.context() as exact_bytes:
        exact_bytes.setattr(
            recovery,
            "SQLITE_LOGICAL_STREAMING_POLICIES",
            {table_name: replace(policy, max_bytes=exact_byte_count)},
        )
        assert table_name in recovery._sqlite_logical_fingerprints(db_path)
    with monkeypatch.context() as bounded_bytes:
        bounded_bytes.setattr(
            recovery,
            "SQLITE_LOGICAL_STREAMING_POLICIES",
            {table_name: replace(policy, max_bytes=exact_byte_count - 1)},
        )
        with pytest.raises(
            recovery.RecoveryRefused,
            match=rf"sqlite_logical_table_{table_name}_byte_limit_exceeded",
        ):
            recovery._sqlite_logical_fingerprints(db_path)


def test_streaming_logical_policy_limits_keep_defaults_and_headroom_explicit() -> None:
    assert recovery.MAX_LEGACY_PROTECTED_QUEUE_ROWS == 16_384
    assert recovery.MAX_LEGACY_PROTECTED_QUEUE_BYTES == 32 * 1024 * 1024
    assert {
        table: (policy.primary_key, policy.max_rows, policy.max_bytes)
        for table, policy in recovery.SQLITE_LOGICAL_STREAMING_POLICIES.items()
    } == {
        "quality_findings": (("id",), 131_072, 128 * 1024 * 1024),
        "domain_events": (("id",), 131_072, 64 * 1024 * 1024),
        "domain_event_handler_executions": (
            ("id",),
            131_072,
            32 * 1024 * 1024,
        ),
        "activity_logs": (("id",), 131_072, 128 * 1024 * 1024),
        "semantic_subject_version_events": (
            ("event_id",),
            131_072,
            128 * 1024 * 1024,
        ),
        "spec_history": (("id",), 65_536, 256 * 1024 * 1024),
        "kg_cognitive_source_revisions": (
            ("id",),
            32_768,
            256 * 1024 * 1024,
        ),
    }


def test_streaming_logical_policy_headroom_matches_canonical_inventory() -> None:
    # Read-only inventory from the 2026-08-15 canonical rehearsal fixture:
    # (row count, canonical row bytes, August rows through day 15).
    observed = {
        "quality_findings": (21_983, 18_193_982, 12_384),
        "domain_events": (16_688, 7_653_834, 11_424),
        "domain_event_handler_executions": (20_524, 3_242_860, 13_371),
        "activity_logs": (10_525, 13_428_387, 6_491),
        "semantic_subject_version_events": (9_182, 5_008_614, 9_182),
        "spec_history": (4_564, 32_495_214, 2_860),
        "kg_cognitive_source_revisions": (5_111, 52_148_992, 1_439),
    }
    for table_name, (row_count, byte_count, august_rows) in observed.items():
        policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[table_name]
        average_row_bytes = byte_count / row_count
        daily_rows = august_rows / 15
        row_headroom_days = (policy.max_rows - row_count) / daily_rows
        byte_headroom_days = (
            (policy.max_bytes - byte_count) / average_row_bytes / daily_rows
        )
        assert min(row_headroom_days, byte_headroom_days) >= 117


def test_streaming_logical_fingerprint_has_stable_framing_and_never_materializes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "golden.db"
    _create_streaming_policy_database(
        db_path,
        "domain_events",
        row_order=(1,),
    )
    monkeypatch.setattr(
        recovery,
        "_fingerprint_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("streaming policy must not materialize rows")
        ),
    )
    assert (
        recovery._sqlite_logical_fingerprints(db_path)["domain_events"]
        == "a0279aa68a25af0da69d1c1412f345d9669d43d395e53e0b539b5ca011928cd8"
    )


def test_streaming_execution_policy_accepts_observed_20524_row_fixture(
    tmp_path: Path,
) -> None:
    table_name = "domain_event_handler_executions"
    policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[table_name]
    columns = tuple(column[0] for column in policy.schema)
    db_path = tmp_path / "current-like.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(_streaming_policy_create_sql(table_name, policy))
        connection.executemany(
            f'INSERT INTO "{table_name}" '
            f"({_quoted_columns(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            (_streaming_policy_row(policy, index) for index in range(20_524)),
        )
    fingerprint = recovery._sqlite_logical_fingerprints(db_path)[table_name]
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)


@pytest.mark.parametrize(
    ("ddl_transform", "error_code"),
    (
        (
            lambda ddl: ddl.replace(
                '"event_type" VARCHAR(100) NOT NULL',
                '"event_type" TEXT NOT NULL',
            ),
            "schema_invalid",
        ),
        (
            lambda ddl: ddl.replace(
                "\"actor_type\" VARCHAR(20) NOT NULL DEFAULT 'user'",
                "\"actor_type\" VARCHAR(20) NOT NULL DEFAULT 'agent'",
            ),
            "schema_invalid",
        ),
        (
            lambda ddl: ddl.replace(
                '"event_type" VARCHAR(100) NOT NULL',
                '"event_type" VARCHAR(100)',
            ),
            "schema_invalid",
        ),
        (
            lambda ddl: (
                ddl[:-1]
                + ', "generated_shadow" TEXT GENERATED ALWAYS AS ("event_type") STORED)'
            ),
            "schema_invalid",
        ),
    ),
)
def test_streaming_logical_fingerprint_refuses_exact_schema_variants(
    tmp_path: Path,
    ddl_transform: Callable[[str], str],
    error_code: str,
) -> None:
    table_name = "domain_events"
    policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[table_name]
    db_path = tmp_path / f"{error_code}.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            ddl_transform(_streaming_policy_create_sql(table_name, policy))
        )
    with pytest.raises(
        recovery.RecoveryRefused,
        match=rf"sqlite_logical_table_{table_name}_{error_code}",
    ):
        recovery._sqlite_logical_fingerprints(db_path)


def test_streaming_logical_fingerprint_refuses_virtual_table_and_invalid_pk_values(
    tmp_path: Path,
) -> None:
    virtual_db = tmp_path / "virtual.db"
    with sqlite3.connect(virtual_db) as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE domain_events USING fts5("
            "id,event_type,board_id,actor_id,actor_type,payload_json,occurred_at)"
        )
    with pytest.raises(
        recovery.RecoveryRefused,
        match="sqlite_logical_table_domain_events_storage_type_invalid",
    ):
        recovery._sqlite_logical_fingerprints(virtual_db)

    policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES["domain_events"]
    columns = tuple(column[0] for column in policy.schema)
    primary_key_index = columns.index("id")
    for label, invalid_value in (("null", None), ("empty", "")):
        db_path = tmp_path / f"{label}.db"
        row = list(_streaming_policy_row(policy, 1))
        row[primary_key_index] = invalid_value
        with sqlite3.connect(db_path) as connection:
            connection.execute(_streaming_policy_create_sql("domain_events", policy))
            connection.execute(
                'INSERT INTO "domain_events" '
                f"({_quoted_columns(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(row),
            )
        with pytest.raises(
            recovery.RecoveryRefused,
            match="sqlite_logical_table_domain_events_primary_key_value_invalid",
        ):
            recovery._sqlite_logical_fingerprints(db_path)


def test_streaming_logical_fingerprint_uses_one_wal_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_name = "domain_events"
    db_path = tmp_path / "wal.db"
    _create_streaming_policy_database(db_path, table_name, row_order=(1,))
    with sqlite3.connect(db_path) as connection:
        assert str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]) == "wal"
    baseline = recovery._sqlite_logical_fingerprints(db_path)[table_name]
    policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[table_name]
    columns = tuple(column[0] for column in policy.schema)
    original = recovery._stream_sqlite_logical_table_fingerprint
    injected = False

    def _inject_after_inventory(connection, *, table_name, policy):  # noqa: ANN001
        nonlocal injected
        if not injected:
            injected = True
            with sqlite3.connect(db_path) as writer:
                writer.execute(
                    f'INSERT INTO "{table_name}" '
                    f"({_quoted_columns(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    _streaming_policy_row(policy, 2),
                )
        return original(connection, table_name=table_name, policy=policy)

    with monkeypatch.context() as concurrent:
        concurrent.setattr(
            recovery,
            "_stream_sqlite_logical_table_fingerprint",
            _inject_after_inventory,
        )
        during_commit = recovery._sqlite_logical_fingerprints(db_path)[table_name]
    assert injected is True
    assert during_commit == baseline
    assert recovery._sqlite_logical_fingerprints(db_path)[table_name] != baseline


def test_streaming_logical_lane_terminal_gate_still_checks_foreign_keys(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "foreign-key.db"
    parent_policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES["domain_events"]
    child_policy = recovery.SQLITE_LOGICAL_STREAMING_POLICIES[
        "domain_event_handler_executions"
    ]
    parent_ddl = _streaming_policy_create_sql("domain_events", parent_policy)
    child_ddl = _streaming_policy_create_sql(
        "domain_event_handler_executions",
        child_policy,
    )
    child_ddl = child_ddl[:-1] + ", FOREIGN KEY(event_id) REFERENCES domain_events(id))"
    with sqlite3.connect(db_path) as connection:
        connection.execute(parent_ddl)
        connection.execute(child_ddl)
        columns = tuple(column[0] for column in child_policy.schema)
        connection.execute(
            'INSERT INTO "domain_event_handler_executions" '
            f"({_quoted_columns(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            _streaming_policy_row(child_policy, 1),
        )
    schema_fingerprint = recovery._sqlite_schema_fingerprint(db_path)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="terminal_sqlite_foreign_key_check_failed",
    ):
        recovery._assert_schema_unchanged(db_path, schema_fingerprint)


def _create_legacy_artifacts(
    data_home: Path,
    *,
    source_count: int = 1,
    current_checkpoint: bool = False,
    historical_data_home: Path | None = None,
) -> tuple[Path, Path, str]:
    historical_home = historical_data_home or data_home
    rebuild = data_home / "rebuild"
    quarantine = data_home / "quarantine"
    board = data_home / "boards" / BOARD_ID
    board.mkdir(parents=True)
    (board / "graph.lbug").write_bytes(b"current-restored-graph")

    source_rows = [_source_payload(index) for index in range(source_count)]
    snapshot_key = f"{F06_RUN_ID}:snapshot"
    quarantine_key = f"{F06_RUN_ID}:quarantine"
    enqueue_key = f"{F06_RUN_ID}:enqueue"
    prefix = {
        snapshot_key: _receipt(
            snapshot_key,
            "snapshot",
            details={"board_id": BOARD_ID, "readable": True, "nodes": [], "edges": []},
        ),
        quarantine_key: _receipt(
            quarantine_key,
            "quarantine",
            details={
                "affected_files": [f"board:{BOARD_ID}:artifact:0"],
                "quarantine_ref": ORIGINAL_QUARANTINE_ID,
            },
        ),
        enqueue_key: _receipt(
            enqueue_key,
            "enqueue",
            details={
                "inserted": source_count,
                "reset_to_pending": 0,
                "left_alone": 0,
            },
        ),
    }
    command = {
        "run_id": F06_RUN_ID,
        "board_id": BOARD_ID,
        "manifest_ref": MANIFEST_REF,
        "operation": "rebuild",
        "actor_id": "legacy-actor",
        "reason": "legacy governed rebuild",
        "source_rows": source_rows,
        "previous_generation_id": None,
        "candidate_generation_id": CANDIDATE_ID,
        "salvage_pending": False,
    }
    checkpoint = {
        "kind": "f06_rebuild_checkpoint",
        "command": command,
        "state": "blocked",
        "started_at": "2026-08-15T02:30:00+00:00",
        "last_progress_at": "2026-08-15T02:40:00+00:00",
        "best_queue_depth": 1,
        "last_sequence": 1,
        "queue_progress_events": 0,
        "queue_grace_applied": False,
        "queue_grace_reason": None,
        "writer_handoff_count": 0,
        "writer_reacquire_count": 0,
        "compensation_failed_state": None,
        "compensation_failure_code": None,
        "compensation_failure_detail": None,
        "compensation_actions": [],
        "receipts": prefix,
    }
    if not current_checkpoint:
        for field in POST_LEGACY_CHECKPOINT_FIELDS:
            checkpoint.pop(field)
    checkpoint_id = (
        "f06-checkpoint-" + hashlib.sha256(F06_RUN_ID.encode()).hexdigest()[:24]
    )
    checkpoint_relative = f"audit/{checkpoint_id}.json"
    _write_json(rebuild / checkpoint_relative, checkpoint)
    for effect_key, payload in prefix.items():
        _write_json(rebuild / _effect_relative(effect_key), payload)
    lease_audit_key = f"{F06_RUN_ID}:audit:lease_lost"
    _write_json(
        rebuild / _effect_relative(lease_audit_key),
        _receipt(
            lease_audit_key,
            "audit",
            details={
                "state": "blocked",
                "code": "lease_lost",
                "promotion_allowed": False,
                "compensation_actions": [],
                "detail": "single-writer lease lost",
            },
        ),
    )
    _manifest, manifest_payload = _manifest_fixture(source_count)
    _write_json(
        rebuild / "manifests" / f"{MANIFEST_REF}.json",
        manifest_payload,
    )
    run_id = "run_legacy_executor"
    report_id = REPORT_ID
    report_path = rebuild / "reports" / f"{report_id}.json"
    _write_json(
        report_path,
        {
            "report_id": report_id,
            "persisted_at": "2026-08-15T02:45:00+00:00",
            "summary": {
                "board_id": BOARD_ID,
                "run_id": run_id,
                "status": "rebuild_failed",
            },
        },
    )
    _write_json(
        rebuild / "audit" / f"{run_id}.json",
        {
            "run_id": run_id,
            "board_id": BOARD_ID,
            "actor_id": "legacy-actor",
            "operation": "rebuild",
            "manifest_ref": MANIFEST_REF,
            "outcome": "rebuild_failed",
            "reason": "lease_lost",
            "event_emitted": True,
            "previous_kg_generation_id": None,
            "current_kg_generation_id": None,
            "promotion_outcome": None,
            "report_id": report_id,
            "report_ref": str(report_path.resolve()),
            "confirmation_ref": CONFIRMATION_REF,
        },
    )
    _write_json(
        rebuild / "audit" / "confirmation" / BOARD_ID / f"audit_{'e' * 32}.json",
        {
            "board_id": BOARD_ID,
            "operation": "rebuild",
            "outcome": "consumed",
            "reason": "confirmed",
            "actor_ref": "legacy-actor",
            "preflight_hash": PREFLIGHT_HASH,
            "generation_ids": {},
            "affected_files": [],
            "confirmation_ref": CONFIRMATION_REF,
        },
    )

    original = quarantine / ORIGINAL_QUARANTINE_ID
    original.mkdir(parents=True)
    (original / "graph.lbug").write_bytes(b"pre-rebuild-graph")
    _write_json(
        original / "manifest.json",
        {
            "quarantine_id": ORIGINAL_QUARANTINE_ID,
            "board_id": BOARD_ID,
            "graph_type": "board_graph",
            "reason": f"explicit_rebuild:{MANIFEST_REF}",
            "reason_bucket": "unknown",
            "correlation_ids": [],
            "affected_paths_relative": ["graph.lbug"],
            "affected_storage_refs": [
                {
                    "namespace": "community_local_graph_v1",
                    "token": base64.urlsafe_b64encode(
                        str(
                            (
                                historical_home / "boards" / BOARD_ID / "graph.lbug"
                            ).resolve()
                        ).encode("utf-8")
                    )
                    .decode("ascii")
                    .rstrip("="),
                }
            ],
            "kg_generation_id": None,
            "files_moved": 1,
            "software_version": "0.3.2",
            "quarantined_at": "2026-08-15T02:32:52+00:00",
            "retention_until": "2026-09-14T02:32:52+00:00",
        },
    )
    manual = quarantine / MANUAL_QUARANTINE_ID
    manual.mkdir()
    (manual / "graph.lbug").write_bytes(b"failed-candidate-graph")
    (manual / "graph.lbug.wal").write_bytes(b"failed-candidate-wal")
    _write_json(
        manual / "manifest.json",
        {
            "quarantine_id": MANUAL_QUARANTINE_ID,
            "board_id": BOARD_ID,
            "graph_type": "board_graph",
            "reason": f"restore_backup_swap:{ORIGINAL_QUARANTINE_ID}",
            "reason_bucket": "operator_manual",
            "correlation_ids": [ORIGINAL_QUARANTINE_ID],
            "affected_paths_relative": ["graph.lbug", "graph.lbug.wal"],
            "kg_generation_id": None,
            "files_moved": 2,
            "software_version": "0.3.2",
            "quarantined_at": "2026-08-15T02:42:30+00:00",
            "retention_until": "2026-09-14T02:42:30+00:00",
        },
    )
    _write_json(
        manual / "restore_operation.json",
        {
            "operation": "quarantine_restore",
            "compensation_run_id": None,
            "source_quarantine_id": ORIGINAL_QUARANTINE_ID,
            "source_quarantine_dir": str(
                (historical_home / "quarantine" / ORIGINAL_QUARANTINE_ID).resolve()
            ),
            "backup_quarantine_id": MANUAL_QUARANTINE_ID,
            "backup_quarantine_dir": str(
                (historical_home / "quarantine" / MANUAL_QUARANTINE_ID).resolve()
            ),
            "board_id": BOARD_ID,
            "board_dir": str((historical_home / "boards" / BOARD_ID).resolve()),
            "phase": "done",
            "started_at": "2026-08-15T02:42:00+00:00",
            "finished_at": "2026-08-15T02:43:00+00:00",
            "moved_to_backup": ["graph.lbug", "graph.lbug.wal"],
            "pending_backup": [],
            "copied_from_snapshot": ["graph.lbug"],
            "pending_copy": [],
            "open_validated": True,
            "error": None,
            "rollback_instruction": None,
        },
    )
    return rebuild, quarantine, checkpoint_relative


def test_legacy_predigest_real_manifest_store_uses_historical_cognitive_cut(
    tmp_path: Path,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import (
        RebuildSourceManifestIntegrityError,
        cognitive_durable_digest_from_rows,
    )

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, _quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    manifest_path = rebuild / "manifests" / f"{MANIFEST_REF}.json"
    raw_manifest = json.loads(manifest_path.read_bytes())
    cut = recovery._legacy_predigest_cognitive_cut(
        db_path,
        board_id=BOARD_ID,
        manifest_created_at=MANIFEST_CREATED_AT,
    )
    historical_digest = cognitive_durable_digest_from_rows(
        _historical_cognitive_records()
    )
    late = _cognitive_record(
        payload={"title": "late"},
        committed_at="2026-08-15T02:31:00.000000",
        source_revision=2,
    )
    current_digest = cognitive_durable_digest_from_rows(
        (*_historical_cognitive_records(), late)
    )
    ledger_fingerprint = cut.pop("ledger_fingerprint")
    assert recovery._is_sha256(ledger_fingerprint)
    assert cut == {
        "cutoff": MANIFEST_CREATED_AT,
        "base_row_count": 1,
        "revision_row_count": 1,
        "count": 1,
        "digest": historical_digest["digest"],
    }
    assert current_digest != historical_digest

    store = _manifest_store(data_home)
    verified = store.load_verified_legacy_predigest_v3(
        MANIFEST_REF,
        expected_board_id=BOARD_ID,
        expected_preflight_hash=PREFLIGHT_HASH,
        expected_canonical_payload_sha256=recovery._canonical_json_hash(raw_manifest),
        cognitive_digest=historical_digest,
    )
    assert verified.source_set_hash == raw_manifest["source_set_hash"]
    with pytest.raises(RebuildSourceManifestIntegrityError):
        store.load_verified(
            MANIFEST_REF,
            expected_board_id=BOARD_ID,
            expected_preflight_hash=PREFLIGHT_HASH,
            cognitive_digest=current_digest,
        )
    with pytest.raises(RebuildSourceManifestIntegrityError):
        store.load_verified_legacy_predigest_v3(
            MANIFEST_REF,
            expected_board_id=BOARD_ID,
            expected_preflight_hash=PREFLIGHT_HASH,
            expected_canonical_payload_sha256=recovery._canonical_json_hash(
                raw_manifest
            ),
            cognitive_digest=current_digest,
        )


@pytest.mark.parametrize(
    "cutoff",
    (
        None,
        "2026-08-15T02:29:00",
        "2026-08-15T02:29:00Z",
        "2026-08-15T02:29:00.000000+01:00",
    ),
    ids=("missing", "naive", "noncanonical-z", "non-utc"),
)
def test_legacy_predigest_cognitive_cut_refuses_invalid_cutoff(
    tmp_path: Path,
    cutoff: object,
) -> None:
    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    with pytest.raises(recovery.RecoveryRefused, match="manifest_created_at"):
        recovery._legacy_predigest_cognitive_cut(
            db_path,
            board_id=BOARD_ID,
            manifest_created_at=cutoff,
        )


@pytest.mark.parametrize(
    "revision_id",
    (COGNITIVE_REVISION_ID, COGNITIVE_LATE_REVISION_ID),
    ids=("historical", "late"),
)
def test_legacy_predigest_cognitive_cut_refuses_tampered_revision(
    tmp_path: Path,
    revision_id: str,
) -> None:
    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE kg_cognitive_source_revisions SET record_fingerprint=? WHERE id=?",
            ("0" * 64, revision_id),
        )
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_predigest_cognitive_ledger_invalid",
    ):
        recovery._legacy_predigest_cognitive_cut(
            db_path,
            board_id=BOARD_ID,
            manifest_created_at=MANIFEST_CREATED_AT,
        )


def test_legacy_predigest_cognitive_cut_refuses_orphan_revision(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO kg_cognitive_source_revisions "
            "(id,cognitive_source_id,source_revision,record_fingerprint,"
            "payload,evidence_refs,source_session_id,committed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "44444444-4444-4444-8444-444444444444",
                "55555555-5555-4555-8555-555555555555",
                1,
                "0" * 64,
                json.dumps({"title": "orphan"}),
                "[]",
                None,
                "2026-08-15 02:28:30.000000",
            ),
        )
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_predigest_cognitive_revision_parent_missing",
    ):
        recovery._legacy_predigest_cognitive_cut(
            db_path,
            board_id=BOARD_ID,
            manifest_created_at=MANIFEST_CREATED_AT,
        )


def test_legacy_predigest_cognitive_cut_refuses_duplicate_semantic_base(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO kg_cognitive_sources "
            "(id,board_id,node_id,node_type,generation,payload,evidence_refs,"
            "source_session_id,committed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "66666666-6666-4666-8666-666666666666",
                BOARD_ID,
                COGNITIVE_NODE_ID,
                "Learning",
                0,
                json.dumps({"title": "duplicate semantic key"}),
                "[]",
                None,
                "2026-08-15 02:27:30.000000",
            ),
        )
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_predigest_cognitive_semantic_key_duplicate",
    ):
        recovery._legacy_predigest_cognitive_cut(
            db_path,
            board_id=BOARD_ID,
            manifest_created_at=MANIFEST_CREATED_AT,
        )


@pytest.mark.parametrize("tamper", ("ordinal_gap", "reversed_time", "missing_first"))
def test_legacy_predigest_cognitive_cut_refuses_broken_revision_history(
    tmp_path: Path,
    tamper: str,
) -> None:
    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    with sqlite3.connect(db_path) as connection:
        if tamper == "ordinal_gap":
            connection.execute(
                "UPDATE kg_cognitive_source_revisions SET source_revision=3 WHERE id=?",
                (COGNITIVE_LATE_REVISION_ID,),
            )
        elif tamper == "reversed_time":
            connection.execute(
                "UPDATE kg_cognitive_source_revisions SET committed_at=? WHERE id=?",
                ("2026-08-15 02:32:00.000000", COGNITIVE_REVISION_ID),
            )
        else:
            connection.execute(
                "DELETE FROM kg_cognitive_source_revisions WHERE id=?",
                (COGNITIVE_REVISION_ID,),
            )
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_predigest_cognitive_revision_sequence_invalid",
    ):
        recovery._legacy_predigest_cognitive_cut(
            db_path,
            board_id=BOARD_ID,
            manifest_created_at=MANIFEST_CREATED_AT,
        )


def test_legacy_predigest_cognitive_cut_refuses_missing_or_oversize_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    with monkeypatch.context() as bounded:
        bounded.setattr(recovery, "MAX_LEGACY_COGNITIVE_LEDGER_BYTES", 1)
        with pytest.raises(
            recovery.RecoveryRefused,
            match="cognitive_ledger_byte_limit_exceeded",
        ):
            recovery._legacy_predigest_cognitive_cut(
                db_path,
                board_id=BOARD_ID,
                manifest_created_at=MANIFEST_CREATED_AT,
            )
    with monkeypatch.context() as bounded:
        bounded.setattr(recovery, "MAX_LEGACY_COGNITIVE_LEDGER_ROWS", 2)
        with pytest.raises(
            recovery.RecoveryRefused,
            match="cognitive_ledger_row_limit_exceeded",
        ):
            recovery._legacy_predigest_cognitive_cut(
                db_path,
                board_id=BOARD_ID,
                manifest_created_at=MANIFEST_CREATED_AT,
            )
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE kg_cognitive_source_revisions")
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_predigest_cognitive_revision_storage_invalid",
    ):
        recovery._legacy_predigest_cognitive_cut(
            db_path,
            board_id=BOARD_ID,
            manifest_created_at=MANIFEST_CREATED_AT,
        )


@pytest.mark.parametrize("tamper", ("extra", "cutoff"))
def test_legacy_predigest_discovery_refuses_manifest_envelope_or_cut_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    manifest_path = rebuild / "manifests" / f"{MANIFEST_REF}.json"
    manifest = json.loads(manifest_path.read_bytes())
    if tamper == "extra":
        manifest["unexpected"] = "unbound"
    else:
        manifest["created_at"] = "2026-08-15T02:32:00+00:00"
    _write_json(manifest_path, manifest)
    bundle = SimpleNamespace(
        artifact_store=CommunityFileSystemRebuildAuditArtifactStore(data_home),
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_manifest_integrity_invalid",
    ):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            data_home=data_home,
            db_path=db_path,
            rebuild_root=rebuild,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            quarantine_root=quarantine,
            quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
            board_storage_baseline=recovery._snapshot_tree_hashes(
                data_home / "boards" / BOARD_ID
            ),
            board_id=BOARD_ID,
            recovery_actor_id="owner-1",
            recovery_reason="governed legacy recovery",
        )


def test_legacy_checkpoint_candidate_accepts_current_shape_exactly(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "data-home"
    _create_queue_database(data_home / "data" / "pulse.db")
    rebuild, _quarantine, checkpoint_relative = _create_legacy_artifacts(
        data_home,
        current_checkpoint=True,
    )
    baseline = recovery._snapshot_tree_hashes(rebuild)
    raw_checkpoint = json.loads((rebuild / checkpoint_relative).read_bytes())

    candidate = recovery._legacy_checkpoint_candidate(
        rebuild_root=rebuild,
        rebuild_baseline=baseline,
        board_id=BOARD_ID,
        expected_run_id=F06_RUN_ID,
    )

    assert candidate is not None
    assert candidate[0] == checkpoint_relative
    assert candidate[1] == raw_checkpoint


def test_legacy_discovery_selects_active_run_and_normalizes_exact_old_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, checkpoint_relative = _create_legacy_artifacts(
        data_home,
        current_checkpoint=True,
    )
    checkpoint_path = rebuild / checkpoint_relative
    target = json.loads(checkpoint_path.read_bytes())
    for field in POST_LEGACY_CHECKPOINT_FIELDS:
        target.pop(field)
    _write_json(checkpoint_path, target)
    exact_old_bytes = checkpoint_path.read_bytes()

    # The audit namespace is global.  Neither an old checkpoint for another
    # board nor a second old checkpoint for this board may divert selection
    # from the sole WAL-aware active rebuild source.
    for suffix, other_board in (
        ("other_board", "5dcb7b75-466f-4d1e-8893-3899a7cfacf0"),
        ("same_board_old_run", BOARD_ID),
    ):
        other = json.loads(json.dumps(target))
        manifest_ref = f"rebuild_manifest_{suffix}"
        run_id = f"f06:{manifest_ref}"
        other["command"]["board_id"] = other_board
        other["command"]["manifest_ref"] = manifest_ref
        other["command"]["run_id"] = run_id
        relative = (
            "audit/f06-checkpoint-"
            + hashlib.sha256(run_id.encode()).hexdigest()[:24]
            + ".json"
        )
        _write_json(rebuild / relative, other)

    # Source selection precedes inspection of the global intent namespace.
    # A stale malformed nominal marker from another operation must not divert
    # the exact checkpoint selected by the sole active rebuild source.
    stale_intent_key = (
        "f06:rebuild_manifest_stale:"
        "legacy_manually_restored_blocked_after_enqueue_intent"
    )
    _write_json(
        rebuild / _effect_relative(stale_intent_key),
        {
            "effect_key": stale_intent_key,
            "effect": "legacy_manually_restored_blocked_after_enqueue_intent",
            "ok": True,
            "code": "legacy_manual_restore_queue_only_authorized",
            "details": {"invalid": "stale"},
        },
    )

    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_root=quarantine,
        quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
        board_storage_baseline=recovery._snapshot_tree_hashes(
            data_home / "boards" / BOARD_ID
        ),
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )

    assert plan is not None and plan.command.run_id == F06_RUN_ID
    assert plan.checkpoint_baseline["writer_handoff_count"] == 0
    assert plan.checkpoint_baseline["writer_reacquire_count"] == 0
    assert plan.checkpoint_baseline["compensation_failed_state"] is None
    assert plan.checkpoint_baseline["compensation_failure_code"] is None
    assert plan.checkpoint_baseline["compensation_failure_detail"] is None
    assert plan.checkpoint_baseline["compensation_actions"] == []
    assert checkpoint_path.read_bytes() == exact_old_bytes


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        (
            lambda checkpoint: checkpoint.pop("writer_handoff_count"),
            "legacy_queue_only_checkpoint_shape_invalid",
        ),
        (
            lambda checkpoint: (
                [checkpoint.pop(field) for field in POST_LEGACY_CHECKPOINT_FIELDS],
                checkpoint.pop("queue_grace_reason"),
            ),
            "legacy_queue_only_checkpoint_shape_invalid",
        ),
        (
            lambda checkpoint: checkpoint.update(unexpected="value"),
            "legacy_queue_only_checkpoint_shape_invalid",
        ),
        (
            lambda checkpoint: (
                [checkpoint.pop(field) for field in POST_LEGACY_CHECKPOINT_FIELDS],
                checkpoint.update(state="compensating"),
            ),
            "legacy_queue_only_legacy_checkpoint_state_invalid",
        ),
    ),
    ids=("hybrid", "old-missing-base", "extra", "old-nonblocked"),
)
def test_legacy_checkpoint_candidate_refuses_noncanonical_shapes(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], object],
    error: str,
) -> None:
    data_home = tmp_path / "data-home"
    _create_queue_database(data_home / "data" / "pulse.db")
    rebuild, _quarantine, checkpoint_relative = _create_legacy_artifacts(
        data_home,
        current_checkpoint=True,
    )
    checkpoint_path = rebuild / checkpoint_relative
    checkpoint = json.loads(checkpoint_path.read_bytes())
    mutation(checkpoint)
    _write_json(checkpoint_path, checkpoint)

    with pytest.raises(recovery.RecoveryRefused, match=error):
        recovery._legacy_checkpoint_candidate(
            rebuild_root=rebuild,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            board_id=BOARD_ID,
            expected_run_id=F06_RUN_ID,
        )


def test_legacy_run_selection_refuses_multiple_active_rebuild_sources(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, _quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    with sqlite3.connect(db_path) as connection:
        original = list(
            connection.execute(
                f"SELECT {','.join(LEGACY_QUEUE_COLUMNS)} "
                "FROM consolidation_queue LIMIT 1"
            ).fetchone()
        )
        original[0] = "queue-conflicting-run"
        original[3] = "spec-conflicting-run"
        original[9] = "rebuild:rebuild_manifest_conflicting_run"
        original[10] = "pending"
        for index in (12, 13, 14, 15, 16, 17, 18):
            original[index] = None
        connection.execute(
            f"INSERT INTO consolidation_queue ({','.join(LEGACY_QUEUE_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in LEGACY_QUEUE_COLUMNS)})",
            tuple(original),
        )

    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_active_rebuild_source_conflict",
    ):
        recovery._legacy_reconciliation_run_id(
            db_path=db_path,
            rebuild_root=rebuild,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            board_id=BOARD_ID,
        )


def test_legacy_executor_discovers_reconciles_and_rediscovers_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, checkpoint_relative = _create_legacy_artifacts(data_home)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)

    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    rebuild_before = recovery._snapshot_tree_hashes(rebuild)
    quarantine_before = recovery._snapshot_tree_hashes(quarantine)
    board_before = recovery._snapshot_tree_hashes(data_home / "boards" / BOARD_ID)
    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=rebuild_before,
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_before,
        board_storage_baseline=board_before,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    assert plan is not None
    assert plan.terminal is False
    assert len(plan.intent.queue_rows) == 1

    ingestion = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    )
    adapter = ingestion.build_legacy_manual_restore_queue_only_adapter(
        evidence_probe=lambda intent: (
            recovery._assert_legacy_queue_only_evidence_current(
                intent,
                manifest_store=bundle.manifest_store,
                data_home=data_home,
                db_path=db_path,
            )
        )
    )
    result = adapter(
        SimpleNamespace(
            board_id=BOARD_ID,
            intent_id=plan.intent.evidence_digest,
            actor_id="owner-1",
            reason="governed legacy recovery",
            command=plan.command,
            intent_receipt=plan.intent_receipt,
            owner_token="owned-writer-token",
            lease_renew=lambda: True,
            orchestration_renew=lambda: True,
            mutation_guard=lambda: True,
        )
    )
    assert result.code.value == recovery.LEGACY_QUEUE_ONLY_OUTCOME_CODE
    recovery._assert_legacy_queue_only_artifact_transition(
        plan,
        rebuild_root=rebuild,
        rebuild_baseline=rebuild_before,
    )
    assert recovery._snapshot_tree_hashes(quarantine) == quarantine_before
    assert (
        recovery._snapshot_tree_hashes(data_home / "boards" / BOARD_ID) == board_before
    )
    terminal, adoption = recovery._legacy_queue_state_current(
        plan.intent,
        db_path=db_path,
    )
    assert terminal is True
    assert adoption.identities == frozenset({("spec", "spec-legacy")})

    checkpoint_path = rebuild / checkpoint_relative
    exact_terminal_checkpoint = checkpoint_path.read_bytes()
    corrupted_checkpoint = json.loads(exact_terminal_checkpoint)
    corrupted_checkpoint["compensation_failure_code"] = "lease_lost"
    _write_json(checkpoint_path, corrupted_checkpoint)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_terminal_checkpoint_invalid",
    ):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            data_home=data_home,
            db_path=db_path,
            rebuild_root=rebuild,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            quarantine_root=quarantine,
            quarantine_baseline=quarantine_before,
            board_storage_baseline=board_before,
            board_id=BOARD_ID,
            recovery_actor_id="owner-1",
            recovery_reason="ignored on durable replay",
        )
    checkpoint_path.write_bytes(exact_terminal_checkpoint)

    terminal_plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_before,
        board_storage_baseline=board_before,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="ignored on durable replay",
    )
    assert terminal_plan is not None
    assert terminal_plan.terminal is True
    assert terminal_plan.adoption == adoption


def test_legacy_executor_refuses_physical_evidence_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_root=quarantine,
        quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
        board_storage_baseline=recovery._snapshot_tree_hashes(
            data_home / "boards" / BOARD_ID
        ),
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    assert plan is not None
    (quarantine / MANUAL_QUARANTINE_ID / "graph.lbug").write_bytes(b"tampered")
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_quarantine_evidence_changed",
    ):
        recovery._assert_legacy_queue_only_evidence_current(
            plan.intent,
            manifest_store=bundle.manifest_store,
            data_home=data_home,
            db_path=db_path,
        )


def test_legacy_executor_refuses_standalone_mutating_f06_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    promote_key = f"{F06_RUN_ID}:promote"
    _write_json(
        rebuild / _effect_relative(promote_key),
        _receipt(promote_key, "promote", details={"candidate": CANDIDATE_ID}),
    )
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )

    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_effect_artifact_set_invalid",
    ):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            data_home=data_home,
            db_path=db_path,
            rebuild_root=rebuild,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            quarantine_root=quarantine,
            quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
            board_storage_baseline=recovery._snapshot_tree_hashes(
                data_home / "boards" / BOARD_ID
            ),
            board_id=BOARD_ID,
            recovery_actor_id="owner-1",
            recovery_reason="governed legacy recovery",
        )


def test_legacy_executor_binds_copied_report_ref_to_explicit_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "copy-home"
    source_home = tmp_path / "source-home-not-opened"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(
        data_home,
        historical_data_home=source_home,
    )
    audit_path = rebuild / "audit" / "run_legacy_executor.json"
    audit = json.loads(audit_path.read_bytes())
    audit["report_ref"] = str(
        source_home / "rebuild" / "reports" / f"{audit['report_id']}.json"
    )
    _write_json(audit_path, audit)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    kwargs = {
        "data_home": data_home,
        "db_path": db_path,
        "rebuild_root": rebuild,
        "rebuild_baseline": recovery._snapshot_tree_hashes(rebuild),
        "quarantine_root": quarantine,
        "quarantine_baseline": recovery._snapshot_tree_hashes(quarantine),
        "board_storage_baseline": recovery._snapshot_tree_hashes(
            data_home / "boards" / BOARD_ID
        ),
        "board_id": BOARD_ID,
        "recovery_actor_id": "owner-1",
        "recovery_reason": "governed legacy recovery",
    }

    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_report_reference_invalid",
    ):
        recovery._discover_legacy_queue_only_reconciliation(bundle, **kwargs)

    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        historical_data_home=source_home,
        historical_rebuild_root=source_home / "rebuild",
        **kwargs,
    )
    assert plan is not None
    assert plan.intent.payload["terminal_run"]["report_relative"] == (
        f"reports/{audit['report_id']}.json"
    )
    assert not source_home.exists()


@pytest.mark.parametrize(
    ("tamper", "error"),
    (
        (
            "lease_missing_detail",
            "legacy_queue_only_lease_lost_audit_invalid",
        ),
        (
            "lease_extra_detail",
            "legacy_queue_only_lease_lost_audit_invalid",
        ),
        (
            "bogus_storage_ref",
            "legacy_queue_only_original_quarantine_storage_ref_invalid",
        ),
        (
            "wrong_storage_ref",
            "legacy_queue_only_original_quarantine_storage_ref_invalid",
        ),
        (
            "original_manifest_missing_generation",
            "legacy_queue_only_original_quarantine_invalid",
        ),
        (
            "journal_reversed",
            "legacy_queue_only_manual_restore_invalid",
        ),
        (
            "journal_missing_path",
            "legacy_queue_only_manual_restore_invalid",
        ),
        (
            "manual_manifest_files_moved_string",
            "legacy_queue_only_manual_restore_invalid",
        ),
        (
            "manual_retention_before_quarantine",
            "legacy_queue_only_manual_restore_invalid",
        ),
        (
            "non_uuid4_report_id",
            "legacy_queue_only_terminal_run_invalid",
        ),
    ),
)
def test_legacy_executor_refuses_impossible_historical_serializer_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    error: str,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)

    if tamper.startswith("lease_"):
        lease_key = f"{F06_RUN_ID}:audit:lease_lost"
        path = rebuild / _effect_relative(lease_key)
        payload = json.loads(path.read_bytes())
        if tamper == "lease_missing_detail":
            payload["details"].pop("detail")
        else:
            payload["details"]["unexpected"] = "value"
        _write_json(path, payload)
    elif tamper.startswith("original_manifest") or tamper in {
        "bogus_storage_ref",
        "wrong_storage_ref",
    }:
        path = quarantine / ORIGINAL_QUARANTINE_ID / "manifest.json"
        payload = json.loads(path.read_bytes())
        if tamper == "bogus_storage_ref":
            payload["affected_storage_refs"][0]["token"] = "not-a-storage-ref"
        elif tamper == "wrong_storage_ref":
            payload["affected_storage_refs"][0]["token"] = (
                base64.urlsafe_b64encode(
                    str(
                        (data_home / "boards" / BOARD_ID / "other.lbug").resolve()
                    ).encode("utf-8")
                )
                .decode("ascii")
                .rstrip("=")
            )
        else:
            payload.pop("kg_generation_id")
        _write_json(path, payload)
    elif tamper.startswith("journal_"):
        path = quarantine / MANUAL_QUARANTINE_ID / "restore_operation.json"
        payload = json.loads(path.read_bytes())
        if tamper == "journal_reversed":
            payload["moved_to_backup"] = ["graph.lbug.wal", "graph.lbug"]
        else:
            payload.pop("board_dir")
        _write_json(path, payload)
    elif tamper.startswith("manual_"):
        path = quarantine / MANUAL_QUARANTINE_ID / "manifest.json"
        payload = json.loads(path.read_bytes())
        if tamper == "manual_manifest_files_moved_string":
            payload["files_moved"] = "2"
        else:
            payload["retention_until"] = "2026-08-15T02:42:00+00:00"
        _write_json(path, payload)
    else:
        audit_path = rebuild / "audit" / "run_legacy_executor.json"
        audit = json.loads(audit_path.read_bytes())
        old_report = rebuild / "reports" / f"{audit['report_id']}.json"
        report = json.loads(old_report.read_bytes())
        forged_report_id = f"report_{'a' * 32}"
        forged_report = rebuild / "reports" / f"{forged_report_id}.json"
        report["report_id"] = forged_report_id
        audit["report_id"] = forged_report_id
        audit["report_ref"] = str(forged_report.resolve())
        _write_json(forged_report, report)
        _write_json(audit_path, audit)

    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )

    with pytest.raises(recovery.RecoveryRefused, match=error):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            data_home=data_home,
            db_path=db_path,
            rebuild_root=rebuild,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            quarantine_root=quarantine,
            quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
            board_storage_baseline=recovery._snapshot_tree_hashes(
                data_home / "boards" / BOARD_ID
            ),
            board_id=BOARD_ID,
            recovery_actor_id="owner-1",
            recovery_reason="governed legacy recovery",
        )


@pytest.mark.parametrize(
    ("effect", "mutation", "error"),
    (
        (
            "snapshot",
            lambda payload: payload["details"].update(board_id="other-board"),
            "legacy_queue_only_snapshot_receipt_invalid",
        ),
        (
            "enqueue",
            lambda payload: payload["details"].update(unexpected="value"),
            "legacy_queue_only_enqueue_receipt_invalid",
        ),
        (
            "quarantine",
            lambda payload: payload.update(code="not_ok"),
            "legacy_queue_only_quarantine_receipt_invalid",
        ),
    ),
)
def test_legacy_executor_refuses_semantically_invalid_prefix_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
    mutation: Callable[[dict[str, object]], None],
    error: str,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, checkpoint_relative = _create_legacy_artifacts(data_home)
    checkpoint_path = rebuild / checkpoint_relative
    checkpoint = json.loads(checkpoint_path.read_bytes())
    effect_key = f"{F06_RUN_ID}:{effect}"
    receipt = checkpoint["receipts"][effect_key]
    mutation(receipt)
    _write_json(checkpoint_path, checkpoint)
    _write_json(rebuild / _effect_relative(effect_key), receipt)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )

    with pytest.raises(recovery.RecoveryRefused, match=error):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            data_home=data_home,
            db_path=db_path,
            rebuild_root=rebuild,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            quarantine_root=quarantine,
            quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
            board_storage_baseline=recovery._snapshot_tree_hashes(
                data_home / "boards" / BOARD_ID
            ),
            board_id=BOARD_ID,
            recovery_actor_id="owner-1",
            recovery_reason="governed legacy recovery",
        )


def test_legacy_executor_refuses_checkpoint_manifest_projection_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, checkpoint_relative = _create_legacy_artifacts(data_home)
    checkpoint_path = rebuild / checkpoint_relative
    checkpoint = json.loads(checkpoint_path.read_bytes())
    checkpoint["command"]["source_rows"][0]["content_hash"] = "f" * 64
    _write_json(checkpoint_path, checkpoint)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )

    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_manifest_command_mismatch",
    ):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            data_home=data_home,
            db_path=db_path,
            rebuild_root=rebuild,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            quarantine_root=quarantine,
            quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
            board_storage_baseline=recovery._snapshot_tree_hashes(
                data_home / "boards" / BOARD_ID
            ),
            board_id=BOARD_ID,
            recovery_actor_id="owner-1",
            recovery_reason="governed legacy recovery",
        )


@pytest.mark.parametrize(
    ("tamper", "error"),
    (
        ("membership", "legacy_queue_only_membership_command_mismatch"),
        ("terminal_run", "legacy_queue_only_terminal_run_invalid"),
        ("manual_restore", "legacy_queue_only_manual_restore_invalid"),
    ),
)
def test_legacy_executor_reconstructs_semantics_of_existing_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    error: str,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    discovery = dict(
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        quarantine_root=quarantine,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
        board_storage_baseline=recovery._snapshot_tree_hashes(
            data_home / "boards" / BOARD_ID
        ),
        **discovery,
    )
    assert plan is not None
    evidence = plan.intent.to_payload()
    if tamper == "membership":
        evidence["queue"]["memberships"][0]["content_hash"] = "f" * 64
        evidence["queue"]["snapshot_fingerprint"] = canonical_evidence_hash(
            {
                "rows": evidence["queue"]["rows"],
                "memberships": evidence["queue"]["memberships"],
            }
        )
    elif tamper == "terminal_run":
        relative = evidence["terminal_run"]["audit_relative"]
        path = rebuild / relative
        payload = json.loads(path.read_bytes())
        payload["reason"] = "not_lease_lost"
        _write_json(path, payload)
        evidence["terminal_run"]["audit_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    else:
        relative = evidence["manual_restore"]["journal_relative"]
        path = quarantine / relative
        payload = json.loads(path.read_bytes())
        payload["open_validated"] = False
        _write_json(path, payload)
        evidence["manual_restore"]["journal_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    forged = LegacyManualRestoreQueueOnlyIntent.build(evidence)
    _write_json(
        rebuild / str(forged.payload["intent_ref"]),
        recovery._legacy_intent_receipt_payload(forged),
    )

    with pytest.raises(recovery.RecoveryRefused, match=error):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
            quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
            board_storage_baseline=recovery._snapshot_tree_hashes(
                data_home / "boards" / BOARD_ID
            ),
            **discovery,
        )


def test_legacy_executor_refuses_persisted_v3_intent_without_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    discovery = dict(
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        quarantine_root=quarantine,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
        board_storage_baseline=recovery._snapshot_tree_hashes(
            data_home / "boards" / BOARD_ID
        ),
        **discovery,
    )
    assert plan is not None
    v3 = plan.intent.to_payload()
    v3["schema_version"] = "legacy_manual_restore_queue_only.v3"
    del v3["source_revision_guard"]
    for key in ("recovery_run_id", "evidence_digest", "intent_digest"):
        v3.pop(key)
    digest = canonical_evidence_hash(v3)
    v3["recovery_run_id"] = f"legacy_reconcile_{digest[:24]}"
    v3["evidence_digest"] = digest
    v3["intent_digest"] = digest
    persisted = {
        "effect_key": plan.intent_receipt.effect_key,
        "effect": plan.intent_receipt.effect,
        "ok": True,
        "code": plan.intent_receipt.code,
        "details": v3,
    }
    intent_path = rebuild / str(v3["intent_ref"])
    _write_json(intent_path, persisted)
    intent_bytes = intent_path.read_bytes()
    queue_before = recovery._full_queue_snapshot(db_path)
    source_revision_before = _raw_source_revision_state(db_path)
    rebuild_baseline = recovery._snapshot_tree_hashes(rebuild)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_intent_receipt_invalid",
    ):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            rebuild_baseline=rebuild_baseline,
            quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
            board_storage_baseline=recovery._snapshot_tree_hashes(
                data_home / "boards" / BOARD_ID
            ),
            **discovery,
        )

    assert intent_path.read_bytes() == intent_bytes
    assert recovery._snapshot_tree_hashes(rebuild) == rebuild_baseline
    assert recovery._full_queue_snapshot(db_path) == queue_before
    assert _raw_source_revision_state(db_path) == source_revision_before


def test_legacy_executor_accepts_realistic_large_checkpoint_but_bounds_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, checkpoint_relative = _create_legacy_artifacts(
        data_home,
        source_count=404,
    )
    checkpoint_path = rebuild / checkpoint_relative
    assert checkpoint_path.stat().st_size > recovery.REHEARSAL_RECEIPT_MAX_BYTES
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )

    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_root=quarantine,
        quarantine_baseline=recovery._snapshot_tree_hashes(quarantine),
        board_storage_baseline=recovery._snapshot_tree_hashes(
            data_home / "boards" / BOARD_ID
        ),
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    assert plan is not None and len(plan.command.source_rows) == 404

    oversized = rebuild / "audit" / "oversized.json"
    oversized.write_bytes(
        b'{"payload":"' + b"x" * recovery.MAX_GOVERNED_REBUILD_ARTIFACT_BYTES + b'"}'
    )
    baseline = recovery._snapshot_tree_hashes(rebuild)
    with pytest.raises(recovery.RecoveryRefused, match="governed_too_large"):
        recovery._read_baseline_json(
            rebuild,
            baseline,
            "audit/oversized.json",
            code="governed",
        )


def test_legacy_executor_refuses_terminal_checkpoint_artifact_queue_split_brain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    discovery = dict(
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        quarantine_root=quarantine,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    quarantine_baseline = recovery._snapshot_tree_hashes(quarantine)
    board_baseline = recovery._snapshot_tree_hashes(data_home / "boards" / BOARD_ID)
    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_baseline=quarantine_baseline,
        board_storage_baseline=board_baseline,
        **discovery,
    )
    assert plan is not None
    adapter = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    ).build_legacy_manual_restore_queue_only_adapter(
        evidence_probe=lambda intent: (
            recovery._assert_legacy_queue_only_evidence_current(
                intent,
                manifest_store=bundle.manifest_store,
                data_home=data_home,
                db_path=db_path,
            )
        )
    )
    adapter(
        SimpleNamespace(
            board_id=BOARD_ID,
            intent_id=plan.intent.evidence_digest,
            actor_id="owner-1",
            reason="governed legacy recovery",
            command=plan.command,
            intent_receipt=plan.intent_receipt,
            owner_token="writer-token",
            lease_renew=lambda: True,
            orchestration_renew=lambda: True,
            mutation_guard=lambda: True,
        )
    )
    compensation_path = rebuild / recovery._legacy_effect_relative(
        f"{F06_RUN_ID}:compensate"
    )
    audit_path = rebuild / recovery._legacy_effect_relative(
        f"{F06_RUN_ID}:audit:{recovery.LEGACY_QUEUE_ONLY_OUTCOME_CODE}"
    )
    compensation_bytes = compensation_path.read_bytes()
    audit_bytes = audit_path.read_bytes()
    compensation_path.unlink()
    audit_path.unlink()
    invalid_baseline = recovery._snapshot_tree_hashes(rebuild)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_terminal_compensation_artifact_missing",
    ):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            rebuild_baseline=invalid_baseline,
            quarantine_baseline=quarantine_baseline,
            board_storage_baseline=board_baseline,
            **discovery,
        )
    assert recovery._snapshot_tree_hashes(rebuild) == invalid_baseline

    compensation_path.write_bytes(compensation_bytes)
    audit_path.write_bytes(audit_bytes)
    _replace_queue_row(db_path, dict(plan.intent.queue_rows[0]))
    _restore_source_revision_baseline(db_path, plan.intent)
    invalid_baseline = recovery._snapshot_tree_hashes(rebuild)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_compensation_artifact_without_terminal_queue",
    ):
        recovery._discover_legacy_queue_only_reconciliation(
            bundle,
            rebuild_baseline=invalid_baseline,
            quarantine_baseline=quarantine_baseline,
            board_storage_baseline=board_baseline,
            **discovery,
        )
    assert recovery._snapshot_tree_hashes(rebuild) == invalid_baseline


def test_legacy_executor_retries_exact_compensation_failed_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    discovery = dict(
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        quarantine_root=quarantine,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    quarantine_baseline = recovery._snapshot_tree_hashes(quarantine)
    board_baseline = recovery._snapshot_tree_hashes(data_home / "boards" / BOARD_ID)
    initial_baseline = recovery._snapshot_tree_hashes(rebuild)
    plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        rebuild_baseline=initial_baseline,
        quarantine_baseline=quarantine_baseline,
        board_storage_baseline=board_baseline,
        **discovery,
    )
    assert plan is not None

    def fail_before_queue_transaction(_self, **_kwargs):  # noqa: ANN202
        raise RuntimeError("injected_nominal_queue_failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            CommunityBoardRebuildIngestionAdapter,
            "compensate_legacy_manual_restore_queue_only",
            fail_before_queue_transaction,
        )
        failing_owner = CommunityBoardRebuildIngestionAdapter(
            db_path=db_path,
            artifact_store=store,
        )
        failing_adapter = failing_owner.build_legacy_manual_restore_queue_only_adapter(
            evidence_probe=lambda intent: (
                recovery._assert_legacy_queue_only_evidence_current(
                    intent,
                    manifest_store=bundle.manifest_store,
                    data_home=data_home,
                    db_path=db_path,
                )
            )
        )
        failed = failing_adapter(
            SimpleNamespace(
                board_id=BOARD_ID,
                intent_id=plan.intent.evidence_digest,
                actor_id="owner-1",
                reason="governed legacy recovery",
                command=plan.command,
                intent_receipt=plan.intent_receipt,
                owner_token="failed-writer-token",
                lease_renew=lambda: True,
                orchestration_renew=lambda: True,
                mutation_guard=lambda: True,
            )
        )
    assert failed.state.value == "compensation_failed"
    assert failed.code.value == "compensation_failed"
    failure_audit_path = rebuild / recovery._legacy_effect_relative(
        f"{F06_RUN_ID}:audit:compensation_failed"
    )
    assert failure_audit_path.is_file()

    retry_baseline = recovery._snapshot_tree_hashes(rebuild)
    retry_plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        rebuild_baseline=retry_baseline,
        quarantine_baseline=quarantine_baseline,
        board_storage_baseline=board_baseline,
        **discovery,
    )
    assert retry_plan is not None and not retry_plan.terminal
    first_failure_audit = failure_audit_path.read_bytes()

    def fail_differently_before_queue_transaction(_self, **_kwargs):  # noqa: ANN202
        raise RuntimeError("injected_second_nominal_queue_failure")

    with monkeypatch.context() as second_failure_patch:
        second_failure_patch.setattr(
            CommunityBoardRebuildIngestionAdapter,
            "compensate_legacy_manual_restore_queue_only",
            fail_differently_before_queue_transaction,
        )
        second_owner = CommunityBoardRebuildIngestionAdapter(
            db_path=db_path,
            artifact_store=store,
        )
        second_adapter = second_owner.build_legacy_manual_restore_queue_only_adapter(
            evidence_probe=lambda intent: (
                recovery._assert_legacy_queue_only_evidence_current(
                    intent,
                    manifest_store=bundle.manifest_store,
                    data_home=data_home,
                    db_path=db_path,
                )
            )
        )
        second_failed = second_adapter(
            SimpleNamespace(
                board_id=BOARD_ID,
                intent_id=retry_plan.intent.evidence_digest,
                actor_id="owner-1",
                reason="governed legacy recovery",
                command=retry_plan.command,
                intent_receipt=retry_plan.intent_receipt,
                owner_token="second-failed-writer-token",
                lease_renew=lambda: True,
                orchestration_renew=lambda: True,
                mutation_guard=lambda: True,
            )
        )
    assert second_failed.state.value == "compensation_failed"
    assert failure_audit_path.read_bytes() == first_failure_audit

    retry_plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_baseline=quarantine_baseline,
        board_storage_baseline=board_baseline,
        **discovery,
    )
    assert retry_plan is not None and not retry_plan.terminal

    def crash_after_retry_checkpoint(_self, **_kwargs):  # noqa: ANN202
        raise KeyboardInterrupt("crash_after_retry_checkpoint")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            CommunityBoardRebuildIngestionAdapter,
            "compensate_legacy_manual_restore_queue_only",
            crash_after_retry_checkpoint,
        )
        crashing_owner = CommunityBoardRebuildIngestionAdapter(
            db_path=db_path,
            artifact_store=store,
        )
        crashing_adapter = (
            crashing_owner.build_legacy_manual_restore_queue_only_adapter(
                evidence_probe=lambda intent: (
                    recovery._assert_legacy_queue_only_evidence_current(
                        intent,
                        manifest_store=bundle.manifest_store,
                        data_home=data_home,
                        db_path=db_path,
                    )
                )
            )
        )
        with pytest.raises(KeyboardInterrupt, match="crash_after_retry_checkpoint"):
            crashing_adapter(
                SimpleNamespace(
                    board_id=BOARD_ID,
                    intent_id=retry_plan.intent.evidence_digest,
                    actor_id="owner-1",
                    reason="governed legacy recovery",
                    command=retry_plan.command,
                    intent_receipt=retry_plan.intent_receipt,
                    owner_token="crashing-writer-token",
                    lease_renew=lambda: True,
                    orchestration_renew=lambda: True,
                    mutation_guard=lambda: True,
                )
            )
    assert failure_audit_path.read_bytes() == first_failure_audit
    retry_plan = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_baseline=quarantine_baseline,
        board_storage_baseline=board_baseline,
        **discovery,
    )
    assert retry_plan is not None and not retry_plan.terminal
    retry_owner = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    )
    retry_adapter = retry_owner.build_legacy_manual_restore_queue_only_adapter(
        evidence_probe=lambda intent: (
            recovery._assert_legacy_queue_only_evidence_current(
                intent,
                manifest_store=bundle.manifest_store,
                data_home=data_home,
                db_path=db_path,
            )
        )
    )
    completed = retry_adapter(
        SimpleNamespace(
            board_id=BOARD_ID,
            intent_id=retry_plan.intent.evidence_digest,
            actor_id="owner-1",
            reason="governed legacy recovery",
            command=retry_plan.command,
            intent_receipt=retry_plan.intent_receipt,
            owner_token="retry-writer-token",
            lease_renew=lambda: True,
            orchestration_renew=lambda: True,
            mutation_guard=lambda: True,
        )
    )
    assert completed.code.value == recovery.LEGACY_QUEUE_ONLY_OUTCOME_CODE
    recovery._assert_legacy_queue_only_artifact_transition(
        retry_plan,
        rebuild_root=rebuild,
        rebuild_baseline=retry_baseline,
    )
    assert failure_audit_path.is_file()
    terminal = recovery._discover_legacy_queue_only_reconciliation(
        bundle,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_baseline=quarantine_baseline,
        board_storage_baseline=board_baseline,
        **discovery,
    )
    assert terminal is not None and terminal.terminal


@pytest.mark.asyncio
async def test_root_bound_service_retries_copy_like_mid_cas_fence_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from okto_pulse.community.adapters.coordination import (
        build_root_bound_community_write_lock_port,
    )
    from okto_pulse.core.kg import rebuild_service
    from okto_pulse.core.kg.rebuild_service import KGRebuildService
    from okto_pulse.core.kg.single_writer_lock import (
        KGAdministrativeOperationReservation,
        KGSingleWriterLock,
    )

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    _insert_second_legacy_target(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(
        data_home,
        source_count=2,
    )
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    discovery_bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    quarantine_before = recovery._snapshot_tree_hashes(quarantine)
    board_root = data_home / "boards" / BOARD_ID
    board_before = recovery._snapshot_tree_hashes(board_root)
    plan = recovery._discover_legacy_queue_only_reconciliation(
        discovery_bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=recovery._snapshot_tree_hashes(rebuild),
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_before,
        board_storage_baseline=board_before,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    assert plan is not None and not plan.terminal
    assert len(plan.intent.queue_rows) == 2
    queue_before = recovery._full_queue_snapshot(db_path)
    source_revision_before = dict(
        plan.intent.payload["source_revision_guard"]["baseline"]
    )
    original_compensate = CommunityBoardRebuildIngestionAdapter.compensate_legacy_manual_restore_queue_only
    fence_live = True

    def lose_fence_before_second_update(self, **kwargs):  # noqa: ANN001, ANN202
        nonlocal fence_live
        guard_calls = 0
        outer_guard = kwargs["mutation_guard"]

        def fail_closed_guard() -> bool:
            nonlocal guard_calls, fence_live
            guard_calls += 1
            if guard_calls == 4:
                fence_live = False
                return False
            return bool(outer_guard())

        kwargs["mutation_guard"] = fail_closed_guard
        return original_compensate(self, **kwargs)

    with monkeypatch.context() as injected_loss:
        injected_loss.setattr(
            CommunityBoardRebuildIngestionAdapter,
            "compensate_legacy_manual_restore_queue_only",
            lose_fence_before_second_update,
        )
        failed_adapter = CommunityBoardRebuildIngestionAdapter(
            db_path=db_path,
            artifact_store=store,
        ).build_legacy_manual_restore_queue_only_adapter(
            evidence_probe=lambda intent: (
                recovery._assert_legacy_queue_only_evidence_current(
                    intent,
                    manifest_store=discovery_bundle.manifest_store,
                    data_home=data_home,
                    db_path=db_path,
                )
            )
        )
        failed = failed_adapter(
            SimpleNamespace(
                board_id=BOARD_ID,
                intent_id=plan.intent.evidence_digest,
                actor_id="owner-1",
                reason="governed legacy recovery",
                command=plan.command,
                intent_receipt=plan.intent_receipt,
                owner_token="lost-writer-token",
                lease_renew=lambda: fence_live,
                orchestration_renew=lambda: fence_live,
                mutation_guard=lambda: True,
            )
        )
    assert failed.state.value == "compensation_failed"
    assert recovery._full_queue_snapshot(db_path) == queue_before
    with sqlite3.connect(db_path) as connection:
        assert read_legacy_source_revision_state(connection) == source_revision_before
    checkpoint = json.loads(
        (rebuild / plan.checkpoint_relative).read_text(encoding="utf-8")
    )
    failed_receipt = checkpoint["receipts"][f"{F06_RUN_ID}:compensate"]
    second_row_id = sorted(str(row["id"]) for row in plan.intent.queue_rows)[1]
    assert failed_receipt["code"] == (
        "RuntimeError:legacy_queue_only_mutation_guard_lost:"
        f"before_update:{second_row_id}"
    )
    assert not (
        rebuild / recovery._legacy_effect_relative(f"{F06_RUN_ID}:compensate")
    ).exists()

    retry_baseline = recovery._snapshot_tree_hashes(rebuild)
    retry_plan = recovery._discover_legacy_queue_only_reconciliation(
        discovery_bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=retry_baseline,
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_before,
        board_storage_baseline=board_before,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="ignored after durable intent",
    )
    assert retry_plan is not None and not retry_plan.terminal

    probe_errors: list[str] = []

    def slow_evidence_probe(intent):  # noqa: ANN001, ANN202
        time.sleep(0.12)
        try:
            return recovery._assert_legacy_queue_only_evidence_current(
                intent,
                manifest_store=discovery_bundle.manifest_store,
                data_home=data_home,
                db_path=db_path,
            )
        except BaseException as exc:
            probe_errors.append(f"{type(exc).__name__}:{exc}")
            raise

    nominal_adapter = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    ).build_legacy_manual_restore_queue_only_adapter(evidence_probe=slow_evidence_probe)
    port = build_root_bound_community_write_lock_port(data_home)
    renew_observations: list[tuple[str, bool, str]] = []
    real_port_renew = port.renew_single_writer_sync

    def observed_port_renew(**kwargs):  # noqa: ANN003, ANN202
        renewed = real_port_renew(**kwargs)
        renew_observations.append(
            (
                str(kwargs["artifact_id"]),
                renewed,
                threading.current_thread().name,
            )
        )
        return renewed

    monkeypatch.setattr(port, "renew_single_writer_sync", observed_port_renew)
    writer = KGSingleWriterLock(write_lock_port=port)
    reservation = KGAdministrativeOperationReservation(write_lock_port=port)

    class Poison:
        def __getattr__(self, name: str):
            raise AssertionError(f"queue-only lane touched forbidden dependency:{name}")

    service = KGRebuildService(
        base_dir=data_home,
        single_writer_lock=writer,
        safe_write_lifecycle=Poison(),
        quarantine_service=Poison(),
        confirmation_store=Poison(),
        manifest_store=Poison(),
        source_enumerator=Poison(),
        generation_repository=Poison(),
        promotion_guard=Poison(),
        report_store=Poison(),
        terminal_state_guard=Poison(),
        event_emitter=Poison(),
        orphan_scan_provider=Poison(),
        operation_reservation=reservation,
        artifact_store=store,
        legacy_manual_restore_queue_only_adapter=nominal_adapter,
        lock_ttl_seconds=30,
        lease_heartbeat_interval_seconds=0.2,
    )

    class Workers:
        active_families = ()
        families = ()

        @staticmethod
        def start_count(_family: object) -> int:
            return 0

    monkeypatch.setattr(
        recovery,
        "_snapshot_closed_board_storage",
        lambda **kwargs: recovery._snapshot_tree_hashes(kwargs["board_storage_root"]),
    )
    result = await recovery._run_legacy_queue_only_lane(
        retry_plan,
        bundle=SimpleNamespace(
            service=service,
            single_writer_lock=writer,
            operation_reservation=reservation,
        ),
        composition=SimpleNamespace(worker_registry=Workers()),
        db_path=db_path,
        schema_fingerprint=recovery._sqlite_schema_fingerprint(db_path),
        rebuild_root=rebuild,
        rebuild_baseline=retry_baseline,
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_before,
        board_storage_root=board_root,
        board_storage_baseline=board_before,
        actor_id="owner-1",
        cancel_event=threading.Event(),
        lifetime_probe=lambda: True,
        timeout_seconds=10.0,
    )

    assert result["_recovery_phase"] == "reconciled"
    assert probe_errors == []
    terminal, _adoption = recovery._legacy_queue_state_current(
        retry_plan.intent,
        db_path=db_path,
    )
    assert terminal is True
    with sqlite3.connect(db_path) as connection:
        source_revision_after = read_legacy_source_revision_state(connection)
    assert source_revision_after["revision"] == (
        int(source_revision_before["revision"]) + len(retry_plan.intent.queue_rows)
    )
    assert (
        source_revision_after["mutation_nonce"]
        != (source_revision_before["mutation_nonce"])
    )
    assert writer.inspect(board_id=BOARD_ID) is None
    assert reservation.inspect(board_id=BOARD_ID) is None
    assert renew_observations and all(
        result for _artifact, result, _thread in renew_observations
    )
    assert recovery._snapshot_tree_hashes(quarantine) == quarantine_before
    assert recovery._snapshot_tree_hashes(board_root) == board_before


@pytest.mark.asyncio
async def test_legacy_lane_validates_source_guard_before_logical_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    discovery_bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    rebuild_baseline = recovery._snapshot_tree_hashes(rebuild)
    quarantine_baseline = recovery._snapshot_tree_hashes(quarantine)
    board_root = data_home / "boards" / BOARD_ID
    board_baseline = recovery._snapshot_tree_hashes(board_root)
    plan = recovery._discover_legacy_queue_only_reconciliation(
        discovery_bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=rebuild_baseline,
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_baseline,
        board_storage_baseline=board_baseline,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    assert plan is not None and not plan.terminal
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE global_discovery_source_revision SET "
            "revision=revision+1,mutation_nonce=?",
            ("4" * 64,),
        )
    queue_before = recovery._full_queue_snapshot(db_path)
    source_revision_before = _raw_source_revision_state(db_path)
    logical_calls: list[bool] = []
    service_calls: list[bool] = []

    def unexpected_logical(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        logical_calls.append(True)
        raise AssertionError("logical exclusion ran before source guard")

    class Service:
        @staticmethod
        def legacy_manual_restore_queue_only(**_kwargs):  # noqa: ANN202
            service_calls.append(True)
            raise AssertionError("service ran before source guard")

    monkeypatch.setattr(recovery, "_sqlite_logical_fingerprints", unexpected_logical)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_source_revision_transition_invalid",
    ):
        await recovery._run_legacy_queue_only_lane(
            plan,
            bundle=SimpleNamespace(service=Service()),
            composition=SimpleNamespace(),
            db_path=db_path,
            schema_fingerprint="unused",
            rebuild_root=rebuild,
            rebuild_baseline=rebuild_baseline,
            quarantine_root=quarantine,
            quarantine_baseline=quarantine_baseline,
            board_storage_root=board_root,
            board_storage_baseline=board_baseline,
            actor_id="owner-1",
            cancel_event=threading.Event(),
            lifetime_probe=lambda: True,
            timeout_seconds=1.0,
        )

    assert logical_calls == []
    assert service_calls == []
    assert recovery._full_queue_snapshot(db_path) == queue_before
    assert _raw_source_revision_state(db_path) == source_revision_before


@pytest.mark.parametrize("mutate_schema", (False, True))
@pytest.mark.asyncio
async def test_legacy_executor_lane_uses_real_core_service_and_releases_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate_schema: bool,
) -> None:
    from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
    from okto_pulse.community.adapters import rebuild_audit_storage
    from okto_pulse.core.kg import rebuild_service
    from okto_pulse.core.kg.rebuild_service import KGRebuildService
    from okto_pulse.core.kg.single_writer_lock import (
        KGAdministrativeOperationReservation,
        KGSingleWriterLock,
    )

    data_home = tmp_path / "data-home"
    db_path = data_home / "data" / "pulse.db"
    _create_queue_database(db_path)
    _insert_historical_target_dlq_peer(db_path)
    dlq_before = recovery._dlq_snapshot(db_path)
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    monkeypatch.setattr(
        rebuild_audit_storage,
        "default_community_rebuild_base_dir",
        lambda *_args, **_kwargs: data_home,
    )
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)

    discovery_bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=_manifest_store(data_home),
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: None,
    )
    rebuild_before = recovery._snapshot_tree_hashes(rebuild)
    quarantine_before = recovery._snapshot_tree_hashes(quarantine)
    board_root = data_home / "boards" / BOARD_ID
    board_before = recovery._snapshot_tree_hashes(board_root)
    plan = recovery._discover_legacy_queue_only_reconciliation(
        discovery_bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=rebuild_before,
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_before,
        board_storage_baseline=board_before,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="governed legacy recovery",
    )
    assert plan is not None and not plan.terminal
    assert [
        peer["id"] for peer in plan.intent.payload["dead_letter_guard"]["peers"]
    ] == [HISTORICAL_DLQ_ID]

    # Crash after the exact SQLite CAS commits but before the compensation
    # receipt is durable.  This leaves the nominal intent + COMPENSATING
    # checkpoint and terminal queue rows for a fresh process to rediscover.
    crashing_ingestion = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    )
    crashing_adapter = (
        crashing_ingestion.build_legacy_manual_restore_queue_only_adapter(
            evidence_probe=lambda intent: (
                recovery._assert_legacy_queue_only_evidence_current(
                    intent,
                    manifest_store=discovery_bundle.manifest_store,
                    data_home=data_home,
                    db_path=db_path,
                )
            )
        )
    )
    compensation_effect_id = (
        "f06-effect-"
        + hashlib.sha256(f"{F06_RUN_ID}:compensate".encode()).hexdigest()[:24]
    )
    original_replace_json = store.replace_json

    class CrashAfterQueueCommit(BaseException):
        pass

    def crash_before_compensation_receipt(key, transform):  # noqa: ANN001, ANN202
        if key.artifact_id == compensation_effect_id:
            raise CrashAfterQueueCommit
        return original_replace_json(key, transform)

    monkeypatch.setattr(store, "replace_json", crash_before_compensation_receipt)
    with pytest.raises(CrashAfterQueueCommit):
        crashing_adapter(
            SimpleNamespace(
                board_id=BOARD_ID,
                intent_id=plan.intent.evidence_digest,
                actor_id="owner-1",
                reason="governed legacy recovery",
                command=plan.command,
                intent_receipt=plan.intent_receipt,
                owner_token="crash-writer-token",
                lease_renew=lambda: True,
                orchestration_renew=lambda: True,
                mutation_guard=lambda: True,
            )
        )
    monkeypatch.setattr(store, "replace_json", original_replace_json)
    queue_terminal, _crash_adoption = recovery._legacy_queue_state_current(
        plan.intent,
        db_path=db_path,
    )
    assert queue_terminal is True
    source_revision_baseline = dict(
        plan.intent.payload["source_revision_guard"]["baseline"]
    )
    with sqlite3.connect(db_path) as connection:
        source_revision_after_crash = read_legacy_source_revision_state(connection)
    assert source_revision_after_crash["revision"] == (
        int(source_revision_baseline["revision"]) + len(plan.intent.queue_rows)
    )
    assert (
        source_revision_after_crash["mutation_nonce"]
        != (source_revision_baseline["mutation_nonce"])
    )
    assert not (rebuild / "audit" / f"{compensation_effect_id}.json").exists()

    rebuild_before = recovery._snapshot_tree_hashes(rebuild)
    plan = recovery._discover_legacy_queue_only_reconciliation(
        discovery_bundle,
        data_home=data_home,
        db_path=db_path,
        rebuild_root=rebuild,
        rebuild_baseline=rebuild_before,
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_before,
        board_storage_baseline=board_before,
        board_id=BOARD_ID,
        recovery_actor_id="owner-1",
        recovery_reason="ignored on durable replay",
    )
    assert plan is not None and not plan.terminal
    assert plan.intent.payload["recovery_reason"] == "governed legacy recovery"

    # A newly-composed adapter/service (no in-memory checkpoint/effect cache)
    # must run the idempotent CAS against already-terminal rows, persist the
    # missing receipt/checkpoint/audit, and converge without touching the graph.
    ingestion = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    )
    nominal_adapter = ingestion.build_legacy_manual_restore_queue_only_adapter(
        evidence_probe=lambda intent: (
            recovery._assert_legacy_queue_only_evidence_current(
                intent,
                manifest_store=discovery_bundle.manifest_store,
                data_home=data_home,
                db_path=db_path,
            )
        )
    )
    if mutate_schema:

        def adapter(request):  # noqa: ANN001, ANN202
            outcome = nominal_adapter(request)
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "CREATE INDEX injected_unrelated_index "
                    "ON consolidation_dead_letter(board_id)"
                )
            return outcome

    else:
        adapter = nominal_adapter
    port = CommunityLocalWriteLockPort()
    lock_root = data_home / "locks"
    writer = KGSingleWriterLock(base_dir=lock_root, write_lock_port=port)
    reservation = KGAdministrativeOperationReservation(
        base_dir=lock_root,
        write_lock_port=port,
    )

    class Poison:
        def __getattr__(self, name: str):
            raise AssertionError(f"queue-only lane touched forbidden dependency:{name}")

    service = KGRebuildService(
        base_dir=data_home,
        single_writer_lock=writer,
        safe_write_lifecycle=Poison(),
        quarantine_service=Poison(),
        confirmation_store=Poison(),
        manifest_store=Poison(),
        source_enumerator=Poison(),
        generation_repository=Poison(),
        promotion_guard=Poison(),
        report_store=Poison(),
        terminal_state_guard=Poison(),
        event_emitter=Poison(),
        orphan_scan_provider=Poison(),
        operation_reservation=reservation,
        artifact_store=store,
        legacy_manual_restore_queue_only_adapter=adapter,
        lock_ttl_seconds=60,
    )
    monkeypatch.setattr(
        recovery,
        "_snapshot_closed_board_storage",
        lambda **kwargs: recovery._snapshot_tree_hashes(kwargs["board_storage_root"]),
    )

    class Workers:
        active_families = ()
        families = ()

        @staticmethod
        def start_count(_family: object) -> int:
            return 0

    schema_fingerprint = recovery._sqlite_schema_fingerprint(db_path)
    lane = recovery._run_legacy_queue_only_lane(
        plan,
        bundle=SimpleNamespace(
            service=service,
            single_writer_lock=writer,
            operation_reservation=reservation,
        ),
        composition=SimpleNamespace(worker_registry=Workers()),
        db_path=db_path,
        schema_fingerprint=schema_fingerprint,
        rebuild_root=rebuild,
        rebuild_baseline=rebuild_before,
        quarantine_root=quarantine,
        quarantine_baseline=quarantine_before,
        board_storage_root=board_root,
        board_storage_baseline=board_before,
        actor_id="owner-1",
        cancel_event=threading.Event(),
        lifetime_probe=lambda: True,
        timeout_seconds=10.0,
    )
    if mutate_schema:
        with pytest.raises(
            recovery.RecoveryRefused,
            match="relational_schema_changed_during_recovery",
        ):
            await lane
        assert writer.inspect(board_id=BOARD_ID) is None
        assert reservation.inspect(board_id=BOARD_ID) is None
        assert recovery._dlq_snapshot(db_path) == dlq_before
        return
    result = await lane

    assert result == {
        "_recovery_phase": "reconciled",
        "reconciliation_kind": "legacy_manual_restore_queue_only",
        "reconciled_run_id": F06_RUN_ID,
        "legacy_intent_digest": plan.intent.evidence_digest,
        "adopted_identity_count": 1,
    }
    assert writer.inspect(board_id=BOARD_ID) is None
    assert reservation.inspect(board_id=BOARD_ID) is None
    terminal, adoption = recovery._legacy_queue_state_current(
        plan.intent,
        db_path=db_path,
    )
    assert terminal is True
    assert adoption.identities == frozenset({("spec", "spec-legacy")})
    assert recovery._snapshot_tree_hashes(quarantine) == quarantine_before
    assert recovery._snapshot_tree_hashes(board_root) == board_before
    assert recovery._dlq_snapshot(db_path) == dlq_before
    with sqlite3.connect(db_path) as connection:
        assert (
            read_legacy_source_revision_state(connection) == source_revision_after_crash
        )


def test_real_service_bundle_heartbeats_keep_root_without_runtime_context(
    tmp_path: Path,
) -> None:
    import os
    import subprocess
    import sys
    import textwrap

    source_root = Path(recovery.__file__).resolve().parents[2]
    workspace_root = source_root.parent.parent
    core_source_root = workspace_root / "okto_labs_pulse_core" / "src"
    if not core_source_root.is_dir():
        core_source_root = workspace_root / "okto-pulse-core" / "src"
    child_home = tmp_path / "bundle-heartbeat-child"
    script = textwrap.dedent(
        """
        import asyncio
        from pathlib import Path
        import sys
        import threading
        import time

        from okto_pulse.community import kg_recovery_only as recovery

        async def main():
            home = Path(sys.argv[1]).resolve()
            for relative in ('data', 'rebuild', 'quarantine'):
                (home / relative).mkdir(parents=True, exist_ok=True)
            recovery._configure_explicit_environment(home)

            from okto_pulse.community.main import create_community_app
            from okto_pulse.core.composition import runtime_composition_scope
            from okto_pulse.core.kg.rebuild_service import _RebuildLeaseHeartbeat
            from okto_pulse.core.services.application_kg import (
                get_current_provider_registry,
            )

            app = create_community_app()
            composition = app.state.runtime_composition
            transaction = app.state.mcp_cold_start_transaction
            board_id = '11111111-1111-4111-8111-111111111111'
            reservation_token = None
            writer_token = None
            bundle = None
            try:
                with runtime_composition_scope(composition):
                    bundle = recovery._build_service_bundle(kg_base_dir=home)
                    bundle.single_writer_lock.bind_write_lock_port()
                    bundle.operation_reservation.bind_write_lock_port()
                    reservation = bundle.operation_reservation.acquire(
                        board_id=board_id,
                        operation='root-bound-reservation',
                        owner_id='owner-1',
                        ttl_seconds=30,
                        admin_lane=True,
                    )
                    writer = bundle.single_writer_lock.acquire(
                        board_id=board_id,
                        operation='root-bound-writer',
                        owner_id='owner-1',
                        ttl_seconds=30,
                        admin_lane=True,
                    )
                    assert reservation.acquired and reservation.owner_token
                    assert writer.acquired and writer.owner_token
                    reservation_token = reservation.owner_token
                    writer_token = writer.owner_token

                registry_probe = []
                def probe_empty_thread_context():
                    try:
                        get_current_provider_registry()
                    except BaseException:
                        registry_probe.append('absent')
                    else:
                        registry_probe.append('present')
                probe = threading.Thread(target=probe_empty_thread_context)
                probe.start()
                probe.join(timeout=5)
                assert registry_probe == ['absent']

                reservation_heartbeat = _RebuildLeaseHeartbeat(
                    lambda: bundle.operation_reservation.renew(
                        board_id=board_id,
                        owner_token=reservation_token,
                        ttl_seconds=30,
                    ),
                    board_id=board_id,
                    interval_seconds=0.1,
                )
                writer_heartbeat = _RebuildLeaseHeartbeat(
                    lambda: bundle.single_writer_lock.renew(
                        board_id=board_id,
                        owner_token=writer_token,
                        ttl_seconds=30,
                    ),
                    board_id=board_id,
                    interval_seconds=0.1,
                )
                reservation_heartbeat.start()
                writer_heartbeat.start()
                time.sleep(1.2)
                assert reservation_heartbeat.renew_now()
                assert writer_heartbeat.renew_now()
                assert reservation_heartbeat.renew_now() and writer_heartbeat.renew_now()
                writer_heartbeat.stop()
                assert bundle.single_writer_lock.release(
                    board_id=board_id,
                    owner_token=writer_token,
                )
                writer_token = None
                reservation_heartbeat.stop()
                assert bundle.operation_reservation.release(
                    board_id=board_id,
                    owner_token=reservation_token,
                )
                reservation_token = None
                assert bundle.single_writer_lock.inspect(board_id=board_id) is None
                assert bundle.operation_reservation.inspect(board_id=board_id) is None
                lock_root = home / 'locks' / board_id
                assert not (lock_root / '.write.lock').exists()
                assert not (
                    lock_root / '.kg_administrative_operation_reservation_v1.lock'
                ).exists()

                expired_board = '22222222-2222-4222-8222-222222222222'
                expiring = bundle.single_writer_lock.acquire(
                    board_id=expired_board,
                    operation='root-bound-expiry-probe',
                    owner_id='owner-1',
                    ttl_seconds=1,
                    admin_lane=True,
                )
                assert expiring.acquired and expiring.owner_token
                lifecycle = bundle.service.safe_write_lifecycle
                assert lifecycle._owner_probe.is_active_owner(
                    expired_board, expiring.owner_token
                )
                assert not lifecycle._owner_probe.is_active_owner(
                    expired_board, 'foreign-token'
                )
                time.sleep(1.1)
                assert not lifecycle._owner_probe.is_active_owner(
                    expired_board, expiring.owner_token
                )
                steps = []
                lifecycle._step = lambda *_args: steps.append(_args)
                try:
                    lifecycle.apply(
                        board_id=expired_board,
                        graph_type='board_graph',
                        operation='root-bound-expired-owner',
                        owner_token=expiring.owner_token,
                        mutation_ref='expired-owner-proof',
                        required_steps=('checkpoint',),
                    )
                except BaseException as exc:
                    assert type(exc).__name__ == 'SafeWriteLifecycleError'
                else:
                    raise AssertionError('expired owner reached lifecycle step')
                assert steps == []
                assert bundle.single_writer_lock.release(
                    board_id=expired_board,
                    owner_token=expiring.owner_token,
                )
                assert bundle.single_writer_lock.inspect(
                    board_id=expired_board
                ) is None
            finally:
                if bundle is not None and writer_token is not None:
                    bundle.single_writer_lock.release(
                        board_id=board_id,
                        owner_token=writer_token,
                    )
                if bundle is not None and reservation_token is not None:
                    bundle.operation_reservation.release(
                        board_id=board_id,
                        owner_token=reservation_token,
                    )
                with runtime_composition_scope(composition):
                    await recovery._shutdown_composed_runtime(composition, None)
                transaction.rollback()
            print('root_bound_raw_heartbeats_ok')

        asyncio.run(main())
        """
    )
    env = os.environ.copy()
    python_paths = [str(source_root)]
    if core_source_root.is_dir():
        python_paths.append(str(core_source_root))
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(child_home)],
        cwd=source_root.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "root_bound_raw_heartbeats_ok" in completed.stdout
