from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
)
from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects
from okto_pulse.core.application.rebuild_processor import (
    CompensationAction,
    CompensationCommand,
    RebuildCheckpoint,
    RebuildCommand,
    RebuildState,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
from okto_pulse.core.kg.rebuild_service import RebuildStepInput


class DictArtifactStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.quarantines: list[dict] = []

    def write_json_atomic(self, key: RebuildAuditKey, payload) -> None:  # noqa: ANN001
        self.rows[key.to_ref()] = dict(payload)

    def read_json(self, key: RebuildAuditKey):  # noqa: ANN201
        value = self.rows.get(key.to_ref())
        return dict(value) if value is not None else None

    def exists(self, key: RebuildAuditKey) -> bool:
        return key.to_ref() in self.rows

    def delete_json(self, key: RebuildAuditKey) -> bool:
        return self.rows.pop(key.to_ref(), None) is not None

    def list_json(self, prefix: RebuildAuditKey):  # noqa: ANN201
        marker = prefix.to_ref()
        return [dict(value) for key, value in self.rows.items() if key.startswith(marker)]

    def replace_json(self, key: RebuildAuditKey, transform):  # noqa: ANN001, ANN201
        value = transform(self.read_json(key))
        self.write_json_atomic(key, value)
        return value

    def list_quarantine_manifests(self, **_kwargs):  # noqa: ANN201
        return list(self.quarantines)


def test_f06_production_composition_injects_durable_artifact_store(
    monkeypatch, tmp_path: Path
) -> None:
    from okto_pulse.community.adapters.composition import (
        _apply_rebuild_audit_storage,
        _apply_rebuild_ingestion,
    )

    monkeypatch.setenv("OKTO_PULSE_REBUILD_BASE_DIR", str(tmp_path))
    registry = SimpleNamespace()
    _apply_rebuild_audit_storage(registry)
    _apply_rebuild_ingestion(registry)

    assert registry.rebuild_ingestion_port.artifact_store is (
        registry.rebuild_audit_artifact_store
    )


def _queue_db(tmp_path: Path) -> Path:
    path = tmp_path / "pulse.db"
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE consolidation_queue ("
            "id TEXT PRIMARY KEY, board_id TEXT NOT NULL, "
            "artifact_type TEXT NOT NULL, artifact_id TEXT NOT NULL, "
            "priority TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, "
            "triggered_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
            "last_error TEXT, claimed_by_session_id TEXT, claimed_at TEXT, "
            "worker_id TEXT, claim_timeout_at TEXT, next_retry_at TEXT, "
            "UNIQUE(board_id, artifact_type, artifact_id))"
        )
    return path


def _command() -> RebuildCommand:
    return RebuildCommand(
        run_id="f06:manifest-1",
        board_id="board-1",
        manifest_ref="manifest-1",
        operation="rebuild",
        actor_id="operator",
        reason="test",
        source_rows=({"artifact_type": "story", "id": "story-1"},),
        candidate_generation_id="gen-2",
    )


def test_f06_effect_receipt_and_checkpoint_replay_survive_adapter_recreation(
    monkeypatch, tmp_path: Path
) -> None:
    from okto_pulse.core.kg import canonical_cognitive_preservation as cognitive

    calls = {"snapshot": 0}

    def snapshot(board_id: str):
        calls["snapshot"] += 1
        return cognitive.CognitiveSnapshot(board_id=board_id, readable=True)

    monkeypatch.setattr(cognitive, "snapshot_canonical_cognitive", snapshot)
    store = DictArtifactStore()
    first_owner = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path), artifact_store=store
    )
    command = _command()
    first = CommunityRebuildEffects(first_owner, artifact_store=store)
    receipt = first.snapshot(command, effect_key=f"{command.run_id}:snapshot")
    now = datetime.now(timezone.utc)
    checkpoint = RebuildCheckpoint(
        command=command,
        state=RebuildState.SNAPSHOTTED,
        started_at=now,
        last_progress_at=now,
        receipts={receipt.effect_key: receipt},
    )
    first.save_checkpoint(checkpoint)

    second_owner = CommunityBoardRebuildIngestionAdapter(
        db_path=first_owner.db_path, artifact_store=store
    )
    second_owner._rebuild_run_boards[command.run_id] = command.board_id
    second = CommunityRebuildEffects(second_owner, artifact_store=store)
    replayed = second.snapshot(command, effect_key=receipt.effect_key)
    loaded = second.load_checkpoint(command.run_id)

    assert replayed == receipt
    assert calls["snapshot"] == 1
    assert loaded is not None
    assert loaded.command == command
    assert loaded.state is RebuildState.SNAPSHOTTED


def test_f06_every_concrete_effect_replays_without_duplicate_side_effect(
    monkeypatch, tmp_path: Path
) -> None:
    from okto_pulse.core.kg import canonical_cognitive_preservation as cognitive
    from okto_pulse.core.services import application_kg

    calls = {"snapshot": 0, "quarantine": 0, "enqueue": 0, "restore": 0}
    restored_nodes: list[dict] = []

    def snapshot(board_id: str):
        calls["snapshot"] += 1
        return cognitive.CognitiveSnapshot(
            board_id=board_id,
            readable=True,
            nodes=[{"node_type": "Learning", "id": "learning-1", "attrs": {}}],
        )

    def restore(_board_id: str, value):  # noqa: ANN001
        calls["restore"] += 1
        restored_nodes.extend(value.nodes)
        return cognitive.RestoreResult(restored_nodes=len(value.nodes))

    def quarantine(self, *, board_id, reason):  # noqa: ANN001
        del self, board_id, reason
        calls["quarantine"] += 1
        return SimpleNamespace(affected_storage_refs=(), quarantine_ref=None)

    def enqueue(self, *, board_id, run_id, sources):  # noqa: ANN001
        del self, board_id, run_id, sources
        calls["enqueue"] += 1
        return {"inserted": 1, "reset_to_pending": 0, "left_alone": 0}

    monkeypatch.setattr(cognitive, "snapshot_canonical_cognitive", snapshot)
    monkeypatch.setattr(cognitive, "restore_canonical_cognitive", restore)
    monkeypatch.setattr(application_kg, "signal_consolidation_worker", lambda: True)
    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "prepare_board_graph_storage_report",
        quarantine,
    )
    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "enqueue_sources",
        enqueue,
    )

    store = DictArtifactStore()
    db_path = _queue_db(tmp_path)
    command = _command()
    for effect_name in ("snapshot", "quarantine", "enqueue", "restore", "promote"):
        effect_key = f"{command.run_id}:{effect_name}"
        first_owner = CommunityBoardRebuildIngestionAdapter(
            db_path=db_path, artifact_store=store
        )
        first = CommunityRebuildEffects(first_owner, artifact_store=store)
        receipt = getattr(first, effect_name)(command, effect_key=effect_key)

        replay_owner = CommunityBoardRebuildIngestionAdapter(
            db_path=db_path, artifact_store=store
        )
        replay = CommunityRebuildEffects(replay_owner, artifact_store=store)
        assert getattr(replay, effect_name)(command, effect_key=effect_key) == receipt

    assert calls == {"snapshot": 1, "quarantine": 1, "enqueue": 1, "restore": 1}
    assert restored_nodes == [
        {"node_type": "Learning", "id": "learning-1", "attrs": {}}
    ]
    persisted_effects = [
        row["effect"] for row in store.rows.values() if "effect" in row
    ]
    assert sorted(persisted_effects) == [
        "enqueue",
        "promote",
        "quarantine",
        "restore",
        "snapshot",
    ]


def test_f06_compensation_preserves_claims_and_stops_pending_rows(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute(
            "INSERT INTO consolidation_queue "
            "(id,board_id,artifact_type,artifact_id,priority,source,status,attempts) "
            "VALUES ('pending','board-1','story','s1','high','rebuild:manifest-1','pending',0)"
        )
        connection.execute(
            "INSERT INTO consolidation_queue "
            "(id,board_id,artifact_type,artifact_id,priority,source,status,attempts,claimed_by_session_id) "
            "VALUES ('claimed','board-1','story','s2','high','rebuild:manifest-1','claimed',1,'session-1')"
        )
    store = DictArtifactStore()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    )
    command = _command()
    now = datetime.now(timezone.utc)
    owner._rebuild_checkpoint_cache[command.run_id] = RebuildCheckpoint(
        command=command,
        state=RebuildState.COMPENSATING,
        started_at=now,
        last_progress_at=now,
    )
    receipt = CommunityRebuildEffects(owner).compensate(
        CompensationCommand(
            run_id=command.run_id,
            board_id=command.board_id,
            failed_state=RebuildState.DRAINING,
            actions=(
                CompensationAction.CANCEL_ENQUEUED_SOURCES,
                CompensationAction.DISCARD_CANDIDATE_GENERATION,
            ),
            receipt_keys=(),
        ),
        effect_key=f"{command.run_id}:compensate",
    )
    with sqlite3.connect(str(db_path)) as connection:
        rows = dict(
            connection.execute(
                "SELECT id,status FROM consolidation_queue ORDER BY id"
            ).fetchall()
        )

    assert receipt.ok is True
    assert receipt.details["candidate_discard"] == {
        "status": "not_persisted_by_effect_adapter",
        "candidate_generation_id": command.candidate_generation_id,
    }
    assert rows == {"claimed": "claimed", "pending": "failed"}


def test_f06_build_step_uses_core_processor_and_typed_effects(
    monkeypatch, tmp_path: Path
) -> None:
    from okto_pulse.core.kg import canonical_cognitive_preservation as cognitive
    from okto_pulse.core.services import application_kg

    monkeypatch.setattr(
        cognitive,
        "snapshot_canonical_cognitive",
        lambda board_id: cognitive.CognitiveSnapshot(board_id, readable=True),
    )
    monkeypatch.setattr(
        cognitive,
        "restore_canonical_cognitive",
        lambda _board_id, _snapshot: cognitive.RestoreResult(),
    )
    monkeypatch.setattr(application_kg, "signal_consolidation_worker", lambda: True)
    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "prepare_board_graph_storage_report",
        lambda self, *, board_id, reason: SimpleNamespace(
            affected_storage_refs=(), quarantine_ref=None
        ),
    )
    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "queue_depth",
        lambda self, board_id: 0,
    )

    store = DictArtifactStore()
    adapter = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=store,
        drain_timeout_seconds=0.05,
        drain_hard_timeout_seconds=0.1,
        drain_poll_interval_seconds=0.001,
    )
    source = {"artifact_type": "story", "id": "story-1"}
    step = adapter.build_step_adapter(lambda _request: (source,))
    result = step(
        RebuildStepInput(
            board_id="board-1",
            manifest_ref="manifest-1",
            source_set_hash="hash-1",
            actor_id="operator",
            operation="rebuild",
            owner_token="token-1",
            candidate_kg_generation_id="gen-2",
        )
    )

    assert result.ok is True
    assert result.drilldown["ingestion_mode"] == "community_rebuild_effects"
    assert result.drilldown["rebuild_processor"] == {
        "state": "completed",
        "code": "completed",
        "promotion_allowed": True,
        "compensation_actions": [],
    }
    assert result.counts["enqueue_inserted"] == 1
    assert any("f06-checkpoint" in key for key in store.rows)
    audit_payloads = [
        row for row in store.rows.values() if row.get("effect") == "audit"
    ]
    assert len(audit_payloads) == 1
    assert audit_payloads[0]["details"] == {
        "state": "completed",
        "code": "completed",
        "promotion_allowed": True,
        "compensation_actions": [],
        "detail": None,
    }


def test_f06_salvage_pending_blocks_before_quarantine(
    monkeypatch, tmp_path: Path
) -> None:
    called = {"quarantine": 0}

    def quarantine(self, *, board_id, reason):  # noqa: ANN001
        called["quarantine"] += 1
        return ()

    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "prepare_board_graph_storage",
        quarantine,
    )
    adapter = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=DictArtifactStore(),
        salvage_pending_provider=lambda _board_id: True,
        drain_timeout_seconds=0.05,
        drain_hard_timeout_seconds=0.1,
        drain_poll_interval_seconds=0.001,
    )
    result = adapter.build_step_adapter(lambda _request: ())(
        RebuildStepInput(
            board_id="board-1",
            manifest_ref="manifest-salvage",
            source_set_hash="hash",
            actor_id="operator",
            operation="rebuild",
            owner_token="token",
        )
    )
    assert result.ok is False
    assert result.detail is not None and result.detail.startswith("salvage_pending")
    assert called["quarantine"] == 0
