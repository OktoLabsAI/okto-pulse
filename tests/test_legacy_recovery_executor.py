from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community import kg_recovery_only as recovery
from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
)
from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
    LEGACY_QUEUE_COLUMNS,
    LegacyManualRestoreQueueOnlyIntent,
    canonical_evidence_hash,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)


BOARD_ID = "15877207-c147-4805-96d7-d53a625571df"
MANIFEST_REF = "rebuild_manifest_legacy_executor"
F06_RUN_ID = f"f06:{MANIFEST_REF}"
CANDIDATE_ID = "c1acd7b9-2a50-4f72-a228-633470389c66"
CONTENT_HASH = "a" * 64
PREFLIGHT_HASH = "b" * 64
SOURCE_SET_HASH = "c" * 64
CONFIRMATION_REF = f"conf_fp_{'d' * 64}"
MANIFEST_CREATED_AT = "2026-08-15T02:29:00+00:00"


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


def _source_payload(index: int, *, command: bool = False) -> dict[str, str]:
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
    }
    if command:
        payload["_rebuild_manifest_created_at"] = MANIFEST_CREATED_AT
    return payload


def _verified_manifest(source_count: int = 1) -> SimpleNamespace:
    rows = tuple(
        SimpleNamespace(
            **payload,
            source_artifact_status="",
            to_dict=lambda current=payload: dict(current),
        )
        for payload in (_source_payload(index) for index in range(source_count))
    )
    return SimpleNamespace(
        manifest_ref=MANIFEST_REF,
        board_id=BOARD_ID,
        preflight_hash=PREFLIGHT_HASH,
        source_set_hash=SOURCE_SET_HASH,
        created_at=MANIFEST_CREATED_AT,
        materializable_sources=rows,
        skipped_expired_working=(),
    )


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
            "errors TEXT, dead_lettered_at TEXT)"
        )
        placeholders = ",".join("?" for _ in LEGACY_QUEUE_COLUMNS)
        connection.execute(
            f"INSERT INTO consolidation_queue ({','.join(LEGACY_QUEUE_COLUMNS)}) "
            f"VALUES ({placeholders})",
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
        connection.execute(
            "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            (("first", "one"), ("second", "two")),
        )

    monkeypatch.setattr(recovery, "MAX_LEGACY_PROTECTED_QUEUE_ROWS", 1)
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
    monkeypatch.setattr(recovery, "MAX_RECOVERY_SQLITE_TABLES", 1)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="sqlite_schema_inventory_row_limit_exceeded",
    ):
        recovery._sqlite_schema_fingerprint(db_path)


def _create_legacy_artifacts(
    data_home: Path,
    *,
    source_count: int = 1,
) -> tuple[Path, Path, str]:
    rebuild = data_home / "rebuild"
    quarantine = data_home / "quarantine"
    board = data_home / "boards" / BOARD_ID
    board.mkdir(parents=True)
    (board / "graph.lbug").write_bytes(b"current-restored-graph")

    source_rows = [
        _source_payload(index, command=True) for index in range(source_count)
    ]
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
            details={"affected_files": ["graph.lbug"], "quarantine_ref": "q_original"},
        ),
        enqueue_key: _receipt(
            enqueue_key,
            "enqueue",
            details={
                "inserted": 1,
                "queue_order_version": 4,
                "enqueue_admission_complete": True,
                "baseline_dead_letter_ids": [],
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
                "detail": "legacy lease lost",
            },
        ),
    )
    _write_json(
        rebuild / "manifests" / f"{MANIFEST_REF}.json",
        {
            "manifest_ref": MANIFEST_REF,
            "board_id": BOARD_ID,
            "preflight_hash": PREFLIGHT_HASH,
            "source_set_hash": SOURCE_SET_HASH,
        },
    )
    run_id = "run_legacy_executor"
    report_id = (
        "report_" + hashlib.sha256(f"{BOARD_ID}\x1f{run_id}".encode()).hexdigest()[:32]
    )
    report_path = rebuild / "reports" / f"{report_id}.json"
    _write_json(
        report_path,
        {
            "report_id": report_id,
            "persisted_at": "2026-08-15T02:45:00+00:00",
            "summary": {"board_id": BOARD_ID, "run_id": run_id, "status": "failed"},
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

    original = quarantine / "q_original"
    original.mkdir(parents=True)
    (original / "graph.lbug").write_bytes(b"pre-rebuild-graph")
    _write_json(
        original / "manifest.json",
        {
            "quarantine_id": "q_original",
            "board_id": BOARD_ID,
            "graph_type": "board_graph",
            "reason": f"explicit_rebuild:{MANIFEST_REF}",
            "reason_bucket": "explicit_rebuild",
            "correlation_ids": [MANIFEST_REF],
            "affected_paths_relative": ["graph.lbug"],
            "files_moved": 1,
        },
    )
    manual = quarantine / "q_manual"
    manual.mkdir()
    (manual / "graph.lbug").write_bytes(b"failed-candidate-graph")
    (manual / "graph.lbug.wal").write_bytes(b"failed-candidate-wal")
    _write_json(
        manual / "manifest.json",
        {
            "quarantine_id": "q_manual",
            "board_id": BOARD_ID,
            "graph_type": "board_graph",
            "reason": "restore_backup_swap:q_original",
            "reason_bucket": "operator_manual",
            "correlation_ids": ["q_original"],
            "affected_paths_relative": ["graph.lbug", "graph.lbug.wal"],
            "files_moved": 2,
        },
    )
    _write_json(
        manual / "restore_operation.json",
        {
            "operation": "quarantine_restore",
            "compensation_run_id": None,
            "source_quarantine_id": "q_original",
            "backup_quarantine_id": "q_manual",
            "board_id": BOARD_ID,
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

    class ManifestStore:
        @staticmethod
        def load_verified(*_args, **_kwargs):  # noqa: ANN202
            return _verified_manifest()

    bundle = SimpleNamespace(artifact_store=store, manifest_store=ManifestStore())
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
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: _verified_manifest()
        ),
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
    (quarantine / "q_manual" / "graph.lbug").write_bytes(b"tampered")
    with pytest.raises(
        recovery.RecoveryRefused,
        match="legacy_queue_only_quarantine_evidence_changed",
    ):
        recovery._assert_legacy_queue_only_evidence_current(
            plan.intent,
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
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: _verified_manifest()
        ),
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
            lambda payload: payload["details"].update(enqueue_admission_complete=False),
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
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: _verified_manifest()
        ),
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
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    verified = _verified_manifest()
    original_row = verified.materializable_sources[0]
    forged_payload = original_row.to_dict()
    forged_payload["content_hash"] = "f" * 64
    forged_row = SimpleNamespace(
        **forged_payload,
        source_artifact_status="",
        to_dict=lambda: dict(forged_payload),
    )
    verified.materializable_sources = (forged_row,)
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)
    bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: verified
        ),
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
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: _verified_manifest()
        ),
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
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: _verified_manifest(404)
        ),
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
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: _verified_manifest()
        ),
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
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: _verified_manifest()
        ),
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
    rebuild, quarantine, _checkpoint_relative = _create_legacy_artifacts(data_home)
    monkeypatch.setattr(
        rebuild_audit_storage,
        "default_community_rebuild_base_dir",
        lambda *_args, **_kwargs: data_home,
    )
    store = CommunityFileSystemRebuildAuditArtifactStore(data_home)

    class ManifestStore:
        @staticmethod
        def load_verified(*_args, **_kwargs):  # noqa: ANN202
            return _verified_manifest()

    discovery_bundle = SimpleNamespace(
        artifact_store=store,
        manifest_store=ManifestStore(),
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
