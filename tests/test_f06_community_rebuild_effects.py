from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
    exact_rebuild_reservation_lineage_id,
)
from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects
from okto_pulse.core.application.rebuild_processor import (
    CompensationAction,
    CompensationCommand,
    RebuildCheckpoint,
    RebuildCommand,
    RebuildEffectReceipt,
    RebuildOutcomeCode,
    RebuildState,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
from okto_pulse.core.kg.rebuild_service import RebuildStepInput
from okto_pulse.core.ports.policy_constraint_projection import (
    PolicyConstraintProjectionResult,
)
from okto_pulse.core.ports.consolidation import (
    build_exact_consolidation_compensation_binding,
)


AUTHORIZED_CONFIRMATION_REF = f"conf_fp_{'a' * 64}"


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
        return [
            dict(value) for key, value in self.rows.items() if key.startswith(marker)
        ]

    def replace_json(self, key: RebuildAuditKey, transform):  # noqa: ANN001, ANN201
        value = transform(self.read_json(key))
        self.write_json_atomic(key, value)
        return value

    def list_quarantine_manifests(self, **_kwargs):  # noqa: ANN201
        return list(self.quarantines)


class CandidateDiscardProbe:
    def __init__(self, candidate_path: Path | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.candidate_path = candidate_path

    def discard_rebuild_candidate(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(dict(kwargs))
        if self.candidate_path is not None:
            self.candidate_path.unlink()
        return {
            "status": "discarded",
            "discarded_files": 1,
            "quarantine_id": "q-candidate",
            "live_absent": True,
        }


def test_f06_production_composition_injects_durable_artifact_store(
    tmp_path: Path,
) -> None:
    from okto_pulse.community.adapters.composition import (
        _apply_rebuild_audit_storage,
        _apply_rebuild_ingestion,
    )

    registry = SimpleNamespace()
    _apply_rebuild_audit_storage(registry, kg_base_dir=str(tmp_path))
    _apply_rebuild_ingestion(registry, lambda: None)

    assert registry.rebuild_ingestion_port.artifact_store is (
        registry.rebuild_audit_artifact_store
    )
    assert callable(registry.rebuild_ingestion_port.policy_constraint_rebuild)
    assert registry.rebuild_audit_artifact_store._base_dir == tmp_path  # noqa: SLF001


def test_quarantine_restore_composition_fences_distinct_data_and_kg_roots(
    tmp_path: Path,
) -> None:
    from okto_pulse.community.adapters.composition import _apply_quarantine_restore

    kg_root = (tmp_path / "kg").resolve()
    data_root = (tmp_path / "data").resolve()
    kg_root.mkdir()
    registry = SimpleNamespace()

    _apply_quarantine_restore(
        registry,
        kg_base_dir=str(kg_root),
        data_dir=str(data_root),
        graph_route_resolver=SimpleNamespace(),
    )

    restore = registry.quarantine_restore
    ladybug = restore._ladybug  # noqa: SLF001
    assert ladybug._resolved_base_dir() == kg_root  # noqa: SLF001
    assert not data_root.exists()
    assert ladybug._serve_lock_directories() == tuple(  # noqa: SLF001
        sorted((data_root, kg_root), key=str)
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
            "work_kind TEXT NOT NULL DEFAULT 'consolidate', "
            "generation INTEGER NOT NULL DEFAULT 0, payload JSON, "
            "CHECK(work_kind IN ('consolidate','stale_reconcile','stale_sweep')))"
        )
        connection.execute(
            "CREATE UNIQUE INDEX uq_queue_consolidate_board_artifact "
            "ON consolidation_queue(board_id, artifact_type, artifact_id) "
            "WHERE work_kind='consolidate'"
        )
    return path


def _command() -> RebuildCommand:
    lineage_id = exact_rebuild_reservation_lineage_id(
        board_id="board-1",
        manifest_ref="manifest-1",
        f06_run_id="f06:manifest-1",
        confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
    )
    return RebuildCommand(
        run_id="f06:manifest-1",
        board_id="board-1",
        manifest_ref="manifest-1",
        operation="rebuild",
        actor_id="operator",
        reason="test",
        source_rows=({"artifact_type": "story", "id": "story-1"},),
        candidate_generation_id="gen-2",
        owner_token="owner-token",
        exact_relational_compensation=True,
        reservation_lineage_id=lineage_id,
    )


def _recovery_request(**overrides) -> RebuildStepInput:  # noqa: ANN003
    values = {
        "board_id": "board-1",
        "manifest_ref": "manifest-1",
        "source_set_hash": "source-set-hash",
        "actor_id": "operator",
        "operation": "rebuild",
        "owner_token": "writer-b",
        "previous_kg_generation_id": None,
        "candidate_kg_generation_id": "gen-2",
        "recovery_failure_code": RebuildOutcomeCode.MANIFEST_DRIFT.value,
        "recovery_failure_detail": "manifest missing during authorized resume",
        "authorized_confirmation_ref": AUTHORIZED_CONFIRMATION_REF,
    }
    values.update(overrides)
    return RebuildStepInput(**values)


def test_f06_recovery_failure_without_checkpoint_never_resolves_sources(
    tmp_path: Path,
) -> None:
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=DictArtifactStore(),
    )

    def _unexpected_source_resolution(_request):  # noqa: ANN001, ANN202
        pytest.fail("compensation-only recovery must not resolve live sources")

    result = owner.build_step_adapter(_unexpected_source_resolution)(
        _recovery_request()
    )

    assert result.ok is False
    assert result.detail == (
        "manifest_drift:recovery_checkpoint_missing_before_mutation"
    )
    assert owner._rebuild_checkpoint_cache == {}  # noqa: SLF001


@pytest.mark.parametrize(
    ("state", "expected_actions"),
    (
        (
            RebuildState.QUARANTINED,
            (
                CompensationAction.RESTORE_QUARANTINE,
                CompensationAction.DISCARD_CANDIDATE_GENERATION,
            ),
        ),
        (
            RebuildState.ENQUEUED,
            (
                CompensationAction.CANCEL_ENQUEUED_SOURCES,
                CompensationAction.COMPENSATE_EXACT_RELATIONAL_COMMITS,
                CompensationAction.RESTORE_QUARANTINE,
                CompensationAction.DISCARD_CANDIDATE_GENERATION,
            ),
        ),
        (
            RebuildState.DRAINING,
            (
                CompensationAction.CANCEL_ENQUEUED_SOURCES,
                CompensationAction.COMPENSATE_EXACT_RELATIONAL_COMMITS,
                CompensationAction.RESTORE_QUARANTINE,
                CompensationAction.DISCARD_CANDIDATE_GENERATION,
            ),
        ),
        (
            RebuildState.COMPLETED,
            (
                CompensationAction.CANCEL_ENQUEUED_SOURCES,
                CompensationAction.COMPENSATE_EXACT_RELATIONAL_COMMITS,
                CompensationAction.DEMOTE_CANDIDATE_GENERATION,
                CompensationAction.RESTORE_QUARANTINE,
                CompensationAction.DISCARD_CANDIDATE_GENERATION,
            ),
        ),
    ),
)
def test_f06_recovery_failure_compensates_checkpoint_without_resolving_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: RebuildState,
    expected_actions: tuple[CompensationAction, ...],
) -> None:
    store = DictArtifactStore()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=store,
    )
    command = _command()
    now = datetime.now(timezone.utc)
    effects = CommunityRebuildEffects(owner, artifact_store=store)
    effects.save_checkpoint(
        RebuildCheckpoint(
            command=command,
            state=state,
            started_at=now,
            last_progress_at=now,
        )
    )
    observed_actions: list[tuple[CompensationAction, ...]] = []

    def _compensate(self, compensation, *, effect_key):  # noqa: ANN001, ANN202
        del self
        observed_actions.append(compensation.actions)
        return RebuildEffectReceipt(
            effect_key,
            "compensate",
            True,
            details={
                "exact_relational_compensation": (
                    build_exact_consolidation_compensation_binding(
                        board_id=command.board_id,
                        source=f"rebuild:{command.manifest_ref}",
                        reservation_lineage_id=str(command.reservation_lineage_id),
                        result=None,
                    )
                )
            },
        )

    def _audit(self, outcome, *, effect_key):  # noqa: ANN001, ANN202
        del self, outcome
        return RebuildEffectReceipt(effect_key, "audit", True)

    monkeypatch.setattr(CommunityRebuildEffects, "compensate", _compensate)
    monkeypatch.setattr(CommunityRebuildEffects, "record_audit", _audit)

    def _unexpected_source_resolution(_request):  # noqa: ANN001, ANN202
        pytest.fail("compensation-only recovery must not resolve live sources")

    result = owner.build_step_adapter(_unexpected_source_resolution)(
        _recovery_request()
    )

    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("manifest_drift:")
    assert observed_actions == [expected_actions]
    loaded = effects.load_checkpoint(command.run_id)
    assert loaded is not None
    assert loaded.state is RebuildState.FAILED


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
        state=RebuildState.COMPENSATING,
        started_at=now,
        last_progress_at=now,
        compensation_failed_state=RebuildState.QUARANTINED,
        compensation_failure_code=RebuildOutcomeCode.ENQUEUE_FAILED,
        compensation_failure_detail="admission failed",
        compensation_actions=(
            CompensationAction.RESTORE_QUARANTINE,
            CompensationAction.DISCARD_CANDIDATE_GENERATION,
        ),
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
    assert loaded.state is RebuildState.COMPENSATING
    assert loaded.compensation_failed_state is RebuildState.QUARANTINED
    assert loaded.compensation_failure_code is RebuildOutcomeCode.ENQUEUE_FAILED
    assert loaded.compensation_failure_detail == "admission failed"
    assert loaded.compensation_actions == (
        CompensationAction.RESTORE_QUARANTINE,
        CompensationAction.DISCARD_CANDIDATE_GENERATION,
    )


def test_f06_enqueue_crash_keeps_prepared_dlq_baseline_on_replay(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from okto_pulse.core.services import application_kg

    class _CrashAfterAdmission(BaseException):
        pass

    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute(
            "CREATE TABLE consolidation_dead_letter ("
            "id TEXT PRIMARY KEY, board_id TEXT NOT NULL)"
        )
        connection.commit()

    store = DictArtifactStore()
    command = _command()
    first_owner = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    )
    first = CommunityRebuildEffects(first_owner, artifact_store=store)
    original_enqueue = CommunityBoardRebuildIngestionAdapter.enqueue_sources
    dlq_inserted = False

    def enqueue_then_dead_letter(self, **kwargs):  # noqa: ANN001, ANN003, ANN201
        nonlocal dlq_inserted
        counts = original_enqueue(self, **kwargs)
        if not dlq_inserted:
            dlq_inserted = True
            with sqlite3.connect(str(db_path)) as connection:
                connection.execute(
                    "INSERT INTO consolidation_dead_letter(id, board_id) "
                    "VALUES ('same-run-dlq', 'board-1')"
                )
                connection.commit()
        return counts

    def crash_signal() -> None:
        raise _CrashAfterAdmission

    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "enqueue_sources",
        enqueue_then_dead_letter,
    )
    monkeypatch.setattr(application_kg, "signal_consolidation_worker", crash_signal)

    effect_key = f"{command.run_id}:enqueue"
    with pytest.raises(_CrashAfterAdmission):
        first.enqueue(command, effect_key=effect_key)

    prepared = first._load_receipt(command, effect_key)  # noqa: SLF001
    assert prepared is not None
    assert prepared.details["enqueue_admission_complete"] is False
    assert prepared.details["baseline_dead_letter_ids"] == []

    replay_owner = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
    )
    replay = CommunityRebuildEffects(replay_owner, artifact_store=store)
    monkeypatch.setattr(application_kg, "signal_consolidation_worker", lambda: None)
    completed = replay.enqueue(command, effect_key=effect_key)

    assert completed.ok is True
    assert completed.details["enqueue_admission_complete"] is True
    assert completed.details["baseline_dead_letter_ids"] == []
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute(
            "DELETE FROM consolidation_queue WHERE board_id='board-1' "
            "AND source='rebuild:manifest-1'"
        )
        connection.commit()
    observation = replay.wait_for_queue_observation(
        command,
        after_sequence=0,
        max_wait_seconds=0,
    )
    assert observation.depth == 0
    assert observation.blocking_reason == "rebuild_new_dead_letter"


def test_f06_enqueue_replay_without_dlq_baseline_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    command = _command()
    store = DictArtifactStore()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=store,
    )
    effects = CommunityRebuildEffects(owner, artifact_store=store)
    effect_key = f"{command.run_id}:enqueue"
    effects._store_receipt(  # noqa: SLF001
        command,
        RebuildEffectReceipt(
            effect_key=effect_key,
            effect="enqueue",
            ok=True,
            details={
                "queue_order_version": 4,
                "enqueue_admission_complete": True,
            },
        ),
    )
    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "enqueue_sources",
        lambda *_args, **_kwargs: pytest.fail("unsafe enqueue replay"),
    )

    receipt = effects.enqueue(command, effect_key=effect_key)

    assert receipt.ok is False
    assert receipt.code == "rebuild_enqueue_baseline_missing_requires_new_manifest"
    assert receipt.details["enqueue_admission_complete"] is False


def test_f06_legacy_resume_without_enqueue_receipt_uses_empty_dlq_cut(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute(
            "CREATE TABLE consolidation_dead_letter ("
            "id TEXT PRIMARY KEY, board_id TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO consolidation_dead_letter(id, board_id) "
            "VALUES ('possibly-same-run', 'board-1')"
        )
        connection.commit()
    command = _command()
    effects = CommunityRebuildEffects(
        CommunityBoardRebuildIngestionAdapter(db_path=db_path),
        artifact_store=DictArtifactStore(),
    )

    prepared = effects.prepare_enqueue_resume_baseline(
        command,
        effect_key=f"{command.run_id}:enqueue",
        prior_receipt=None,
        prior_admission_possible=True,
    )

    assert prepared is not None
    assert prepared.details["baseline_dead_letter_ids"] == []
    assert prepared.details["baseline_recovery"] == (
        "legacy_admission_unknown_fail_closed"
    )


@pytest.mark.parametrize(
    "checkpoint_state", (RebuildState.ENQUEUED, RebuildState.DRAINING)
)
def test_f06_v3_checkpoint_strictly_upgrades_and_replays_v4_enqueue_once(
    monkeypatch,
    tmp_path: Path,
    checkpoint_state: RebuildState,
) -> None:
    from okto_pulse.community.adapters import board_rebuild_ingestion as ingestion
    from okto_pulse.core.services import application_kg

    store = DictArtifactStore()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=store,
        drain_timeout_seconds=0.05,
        drain_hard_timeout_seconds=0.1,
        drain_poll_interval_seconds=0.001,
    )
    denominator = {
        "artifact_type": "code_evidence",
        "id": "evidence-current",
        "source_ref": "code_evidence:evidence-current",
        "source_version": "1",
        "content_hash": "a" * 64,
        "status": "active",
        "source_artifact_status": "active",
        "supersedes_evidence_id": "evidence-history",
    }
    command = replace(_command(), source_rows=(denominator,))
    receipts = {
        f"{command.run_id}:{effect}": RebuildEffectReceipt(
            effect_key=f"{command.run_id}:{effect}",
            effect=effect,
            ok=True,
            details=(
                {
                    "queue_order_version": 3,
                    "baseline_dead_letter_ids": [],
                }
                if effect == "enqueue"
                else {}
            ),
        )
        for effect in ("snapshot", "quarantine", "enqueue")
    }
    now = datetime.now(timezone.utc)
    CommunityRebuildEffects(owner, artifact_store=store).save_checkpoint(
        RebuildCheckpoint(
            command=command,
            state=checkpoint_state,
            started_at=now,
            last_progress_at=now,
            receipts=receipts,
        )
    )
    closure = {
        "artifact_type": "code_evidence",
        "id": "evidence-history",
        "source_ref": "code_evidence:evidence-history",
        "source_version": "1",
        "content_hash": "b" * 64,
        "status": "superseded",
        "source_artifact_status": "superseded",
        "disposition": "skipped_expired_working",
        "_rebuild_manifest_created_at": "2026-08-15T00:00:00+00:00",
        "_rebuild_dependency_closure": "code_evidence_supersedence",
    }
    upgraded_sources = (
        {**denominator, "_rebuild_manifest_created_at": "2026-08-15T00:00:00+00:00"},
        closure,
    )
    monkeypatch.setattr(
        ingestion,
        "_resolve_evidence_dependency_closure",
        lambda **_kwargs: (upgraded_sources, 1),
    )
    enqueue_calls: list[tuple[dict[str, object], ...]] = []

    def enqueue_sources(_self, **kwargs):  # noqa: ANN003, ANN201
        enqueue_calls.append(tuple(dict(row) for row in kwargs["sources"]))
        return {
            "inserted": 2,
            "reset_to_pending": 0,
            "reordered_pending": 0,
            "fenced_claimed": 0,
            "deferred_unrelated": 0,
            "preserved_live_intent": 0,
            "left_alone": 0,
        }

    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "enqueue_sources",
        enqueue_sources,
    )
    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "queue_observation",
        lambda _self, *_args, **_kwargs: (0, None),
    )
    monkeypatch.setattr(application_kg, "signal_consolidation_worker", lambda: True)
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "snapshot",
        lambda *_args, **_kwargs: pytest.fail("snapshot must not replay"),
    )
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "quarantine",
        lambda *_args, **_kwargs: pytest.fail("quarantine must not replay"),
    )
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "restore",
        lambda _self, _command, *, effect_key: RebuildEffectReceipt(
            effect_key=effect_key,
            effect="restore",
            ok=True,
        ),
    )
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "promote",
        lambda _self, _command, *, effect_key: RebuildEffectReceipt(
            effect_key=effect_key,
            effect="promote",
            ok=True,
        ),
    )

    step = owner.build_step_adapter(lambda _request: upgraded_sources)
    request = RebuildStepInput(
        board_id="board-1",
        manifest_ref="manifest-1",
        source_set_hash="hash-1",
        actor_id="operator",
        operation="rebuild",
        owner_token="owner-token-b",
        previous_kg_generation_id="gen-1",
        candidate_kg_generation_id="gen-3",
        authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
    )
    result = step(request)

    assert result.ok is True
    assert enqueue_calls == [upgraded_sources]
    loaded = CommunityRebuildEffects(owner, artifact_store=store).load_checkpoint(
        command.run_id
    )
    assert loaded is not None
    assert loaded.command.source_rows == upgraded_sources
    enqueue_receipt = loaded.receipts[f"{command.run_id}:enqueue"]
    assert enqueue_receipt.details["queue_order_version"] == 4
    replay = step(request)
    assert replay.ok is True
    assert enqueue_calls == [upgraded_sources]


@pytest.mark.parametrize(
    "mutation",
    (
        "status",
        "source_version",
        "source_ref",
        "content_hash",
        "extra_non_closure",
        "unrelated_closure",
        "missing_manifest_cut",
        "different_manifest_cut",
        "post_restore_state",
        "restore_receipt",
        "missing_dlq_baseline",
    ),
)
def test_f06_v4_checkpoint_upgrade_rejects_unbounded_drift(
    mutation: str,
) -> None:
    from okto_pulse.community.adapters.board_rebuild_ingestion import (
        _checkpoint_source_upgrade_allowed,
    )

    denominator = {
        "artifact_type": "code_evidence",
        "id": "evidence-current",
        "source_ref": "code_evidence:evidence-current",
        "source_version": "1",
        "content_hash": "a" * 64,
        "status": "active",
        "source_artifact_status": "active",
        "supersedes_evidence_id": "evidence-history",
    }
    command = replace(_command(), source_rows=(denominator,))
    now = datetime.now(timezone.utc)
    checkpoint = RebuildCheckpoint(
        command=command,
        state=RebuildState.DRAINING,
        started_at=now,
        last_progress_at=now,
        receipts={
            f"{command.run_id}:enqueue": RebuildEffectReceipt(
                effect_key=f"{command.run_id}:enqueue",
                effect="enqueue",
                ok=True,
                details={
                    "queue_order_version": 3,
                    "baseline_dead_letter_ids": [],
                },
            )
        },
    )

    manifest_cut = "2026-08-15T00:00:00+00:00"
    current_denominator = {
        **denominator,
        "_rebuild_manifest_created_at": manifest_cut,
    }
    closure = {
        "artifact_type": "code_evidence",
        "id": "evidence-history",
        "source_ref": "code_evidence:evidence-history",
        "source_version": "1",
        "content_hash": "b" * 64,
        "status": "superseded",
        "source_artifact_status": "superseded",
        "disposition": "skipped_expired_working",
        "_rebuild_manifest_created_at": manifest_cut,
        "_rebuild_dependency_closure": "code_evidence_supersedence",
    }
    sources: tuple[dict[str, object], ...] = (current_denominator, closure)
    candidate_checkpoint = checkpoint
    assert _checkpoint_source_upgrade_allowed(checkpoint, sources)
    if mutation == "status":
        sources = ({**current_denominator, "status": "approved"}, closure)
    elif mutation == "source_version":
        sources = ({**current_denominator, "source_version": "2"}, closure)
    elif mutation == "source_ref":
        sources = (
            {**current_denominator, "source_ref": "code_evidence:other"},
            closure,
        )
    elif mutation == "content_hash":
        sources = ({**current_denominator, "content_hash": "c" * 64}, closure)
    elif mutation == "extra_non_closure":
        sources = (
            current_denominator,
            closure,
            {
                "artifact_type": "spec",
                "id": "spec-extra",
                "source_ref": "spec:spec-extra",
                "source_version": "1",
                "content_hash": "d" * 64,
                "status": "draft",
                "_rebuild_manifest_created_at": manifest_cut,
            },
        )
    elif mutation == "unrelated_closure":
        sources = (
            current_denominator,
            {
                **closure,
                "id": "evidence-unrelated",
                "source_ref": "code_evidence:evidence-unrelated",
            },
        )
    elif mutation == "missing_manifest_cut":
        closure = dict(closure)
        closure.pop("_rebuild_manifest_created_at")
        sources = (current_denominator, closure)
    elif mutation == "different_manifest_cut":
        sources = (
            current_denominator,
            {**closure, "_rebuild_manifest_created_at": "different-cut"},
        )
    elif mutation == "post_restore_state":
        candidate_checkpoint = replace(checkpoint, state=RebuildState.RESTORED)
    elif mutation == "restore_receipt":
        restore_receipt = RebuildEffectReceipt(
            effect_key=f"{command.run_id}:restore",
            effect="restore",
            ok=True,
        )
        candidate_checkpoint = replace(
            checkpoint,
            receipts={
                **checkpoint.receipts,
                restore_receipt.effect_key: restore_receipt,
            },
        )
    elif mutation == "missing_dlq_baseline":
        enqueue_key = f"{command.run_id}:enqueue"
        candidate_checkpoint = replace(
            checkpoint,
            receipts={
                enqueue_key: RebuildEffectReceipt(
                    effect_key=enqueue_key,
                    effect="enqueue",
                    ok=True,
                    details={"queue_order_version": 3},
                )
            },
        )

    before = repr(candidate_checkpoint)
    assert not _checkpoint_source_upgrade_allowed(
        candidate_checkpoint,
        sources,
    )
    assert repr(candidate_checkpoint) == before


def test_f06_v3_upgrade_crash_before_enqueue_replays_v4_without_resnapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The command upgrade is durable before its v4 enqueue side effect."""

    from okto_pulse.community.adapters import board_rebuild_ingestion as ingestion
    from okto_pulse.core.services import application_kg

    class _CrashAfterUpgrade(BaseException):
        pass

    store = DictArtifactStore()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=store,
        drain_timeout_seconds=0.05,
        drain_hard_timeout_seconds=0.1,
        drain_poll_interval_seconds=0.001,
    )
    cut = "2026-08-15T00:00:00+00:00"
    denominator = {
        "artifact_type": "code_evidence",
        "id": "evidence-current",
        "source_ref": "code_evidence:evidence-current",
        "source_version": "1",
        "content_hash": "a" * 64,
        "status": "active",
        "source_artifact_status": "active",
        "supersedes_evidence_id": "evidence-history",
    }
    closure = {
        "artifact_type": "code_evidence",
        "id": "evidence-history",
        "source_ref": "code_evidence:evidence-history",
        "source_version": "1",
        "content_hash": "b" * 64,
        "status": "superseded",
        "source_artifact_status": "superseded",
        "disposition": "skipped_expired_working",
        "_rebuild_manifest_created_at": cut,
        "_rebuild_dependency_closure": "code_evidence_supersedence",
    }
    upgraded_sources = (
        {**denominator, "_rebuild_manifest_created_at": cut},
        closure,
    )
    command = replace(_command(), source_rows=(denominator,))
    now = datetime.now(timezone.utc)
    receipts = {
        f"{command.run_id}:{effect}": RebuildEffectReceipt(
            effect_key=f"{command.run_id}:{effect}",
            effect=effect,
            ok=True,
            details=(
                {
                    "queue_order_version": 3,
                    "baseline_dead_letter_ids": [],
                }
                if effect == "enqueue"
                else {}
            ),
        )
        for effect in ("snapshot", "quarantine", "enqueue")
    }
    effects = CommunityRebuildEffects(owner, artifact_store=store)
    effects.save_checkpoint(
        RebuildCheckpoint(
            command=command,
            state=RebuildState.ENQUEUED,
            started_at=now,
            last_progress_at=now,
            receipts=receipts,
        )
    )
    monkeypatch.setattr(
        ingestion,
        "_resolve_evidence_dependency_closure",
        lambda **_kwargs: (upgraded_sources, 1),
    )
    enqueue_calls: list[tuple[dict[str, object], ...]] = []

    def enqueue_sources(_self, **kwargs):  # noqa: ANN003, ANN201
        enqueue_calls.append(tuple(dict(row) for row in kwargs["sources"]))
        return {
            "inserted": 2,
            "reset_to_pending": 0,
            "reordered_pending": 0,
            "fenced_claimed": 0,
            "deferred_unrelated": 0,
            "preserved_live_intent": 0,
            "left_alone": 0,
        }

    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "enqueue_sources",
        enqueue_sources,
    )
    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "queue_observation",
        lambda _self, *_args, **_kwargs: (0, None),
    )
    monkeypatch.setattr(application_kg, "signal_consolidation_worker", lambda: True)
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "snapshot",
        lambda *_args, **_kwargs: pytest.fail("snapshot must not replay"),
    )
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "quarantine",
        lambda *_args, **_kwargs: pytest.fail("quarantine must not replay"),
    )
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "restore",
        lambda _self, _command, *, effect_key: RebuildEffectReceipt(
            effect_key=effect_key,
            effect="restore",
            ok=True,
        ),
    )
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "promote",
        lambda _self, _command, *, effect_key: RebuildEffectReceipt(
            effect_key=effect_key,
            effect="promote",
            ok=True,
        ),
    )
    original_enqueue = CommunityRebuildEffects.enqueue
    crash = {"pending": True}

    def crash_once(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if crash["pending"]:
            crash["pending"] = False
            raise _CrashAfterUpgrade
        return original_enqueue(self, *args, **kwargs)

    monkeypatch.setattr(CommunityRebuildEffects, "enqueue", crash_once)
    step = owner.build_step_adapter(lambda _request: upgraded_sources)
    request = RebuildStepInput(
        board_id="board-1",
        manifest_ref="manifest-1",
        source_set_hash="hash-1",
        actor_id="operator",
        operation="rebuild",
        owner_token="owner-token-b",
        previous_kg_generation_id="gen-1",
        candidate_kg_generation_id="gen-3",
        authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
    )

    with pytest.raises(_CrashAfterUpgrade):
        step(request)
    after_crash = effects.load_checkpoint(command.run_id)
    assert after_crash is not None
    assert after_crash.command.source_rows == upgraded_sources
    assert (
        after_crash.receipts[f"{command.run_id}:enqueue"].details["queue_order_version"]
        == 3
    )
    assert enqueue_calls == []

    result = step(request)
    assert result.ok is True
    assert enqueue_calls == [upgraded_sources]
    recovered = effects.load_checkpoint(command.run_id)
    assert recovered is not None
    assert (
        recovered.receipts[f"{command.run_id}:enqueue"].details["queue_order_version"]
        == 4
    )


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


def test_f06_compensation_fences_claimed_and_pending_rows_before_discard(
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
    discard = CandidateDiscardProbe()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=db_path,
        artifact_store=store,
        quarantine_restore=discard,
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
        "status": "discarded",
        "discarded_files": 1,
        "quarantine_id": "q-candidate",
        "live_absent": True,
        "candidate_generation_id": command.candidate_generation_id,
    }
    assert discard.calls == [
        {
            "expected_board_id": command.board_id,
            "run_id": command.run_id,
            "owner_token": command.owner_token,
        }
    ]
    assert rows == {"claimed": "failed", "pending": "failed"}
    assert receipt.details["queue"] == {
        "pending_compensated": 1,
        "claimed_compensated": 1,
        "active_remaining": 0,
        "live_intents_restored": 0,
        "total_compensated": 2,
    }


def test_f06_queue_observation_surfaces_graph_memory_pressure(
    tmp_path: Path,
) -> None:
    db_path = _queue_db(tmp_path)
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute(
            "INSERT INTO consolidation_queue "
            "(id,board_id,artifact_type,artifact_id,priority,source,status,"
            "attempts,last_error) "
            "VALUES ('blocked','board-1','story','s1','high',"
            "'rebuild:manifest-1','pending',1,"
            "'graph_memory_pressure:capacity exceeded')"
        )

    store = DictArtifactStore()
    effects = CommunityRebuildEffects(
        CommunityBoardRebuildIngestionAdapter(
            db_path=db_path,
            artifact_store=store,
        ),
        artifact_store=store,
    )
    observation = effects.wait_for_queue_observation(
        _command(),
        after_sequence=7,
        max_wait_seconds=0,
    )

    assert observation.depth == 1
    assert observation.sequence == 8
    assert observation.blocking_reason == "graph_memory_pressure"


def test_f06_quarantine_compensation_uses_governed_restore_capability(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str, str | None]] = []

    class _Restore:
        def apply(self, _quarantine_id: str) -> object:
            raise AssertionError("manual restore must not run inside server")

        def plan(self, quarantine_id: str) -> object:
            return SimpleNamespace(
                quarantine_id=quarantine_id,
                board_id=command.board_id,
                files=(SimpleNamespace(name="graph.lbug"),),
            )

        def apply_rebuild_compensation(
            self,
            quarantine_id: str,
            *,
            expected_board_id: str,
            run_id: str,
            owner_token: str | None,
        ) -> object:
            calls.append((quarantine_id, expected_board_id, run_id, owner_token))
            return SimpleNamespace(
                applied=True,
                open_validated=True,
                quarantine_id=quarantine_id,
                board_id=expected_board_id,
                backup_quarantine_id="q-failed-candidate",
                restored_files=("graph.lbug",),
            )

    store = DictArtifactStore()
    owner = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=store,
    )
    command = _command()
    effects = CommunityRebuildEffects(
        owner,
        artifact_store=store,
        quarantine_restore=_Restore(),
    )
    effects._store_receipt(
        command,
        RebuildEffectReceipt(
            effect_key=f"{command.run_id}:quarantine",
            effect="quarantine",
            ok=True,
            details={
                "affected_files": ["graph.lbug"],
                "quarantine_ref": "q-rebuild-1",
            },
        ),
    )

    now = datetime.now(timezone.utc)
    owner._rebuild_checkpoint_cache[command.run_id] = RebuildCheckpoint(
        command=command,
        state=RebuildState.COMPENSATING,
        started_at=now,
        last_progress_at=now,
    )
    compensated = effects.compensate(
        CompensationCommand(
            run_id=command.run_id,
            board_id=command.board_id,
            failed_state=RebuildState.RESTORED,
            actions=(
                CompensationAction.RESTORE_QUARANTINE,
                CompensationAction.DISCARD_CANDIDATE_GENERATION,
            ),
            receipt_keys=(),
        ),
        effect_key=f"{command.run_id}:compensate",
    )
    assert compensated.ok is True
    assert compensated.details["candidate_discard"] == {
        "status": "discarded_by_atomic_backup_swap",
        "candidate_generation_id": command.candidate_generation_id,
        "candidate_quarantine_id": "q-failed-candidate",
        "live_candidate_absent_before_restore": True,
    }
    replay = effects.compensate(
        CompensationCommand(
            run_id=command.run_id,
            board_id=command.board_id,
            failed_state=RebuildState.RESTORED,
            actions=(
                CompensationAction.RESTORE_QUARANTINE,
                CompensationAction.DISCARD_CANDIDATE_GENERATION,
            ),
            receipt_keys=(),
        ),
        effect_key=f"{command.run_id}:compensate",
    )
    assert replay == compensated
    assert calls == [
        (
            "q-rebuild-1",
            command.board_id,
            command.run_id,
            command.owner_token,
        )
    ]


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
        "queue_observation",
        lambda self, board_id, **_kwargs: (0, None),
    )

    store = DictArtifactStore()
    policy_calls: list[str] = []

    def rebuild_policy_constraints(
        board_id: str,
    ) -> PolicyConstraintProjectionResult:
        policy_calls.append(board_id)
        return PolicyConstraintProjectionResult(
            board_id=board_id,
            operation="rebuild",
            event_id=None,
            activated_count=2,
            ended_count=1,
            active_count=2,
            unadopted_active_count=0,
            node_ids=(
                "guideline-revision:r1:rule:a",
                "guideline-revision:r1:rule:b",
            ),
            replayed=False,
        )

    adapter = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=store,
        drain_timeout_seconds=0.05,
        drain_hard_timeout_seconds=0.1,
        drain_poll_interval_seconds=0.001,
        policy_constraint_rebuild=rebuild_policy_constraints,
    )
    source = {
        "artifact_type": "story",
        "id": "story-1",
        "source_ref": "story:story-1",
        "source_version": "3",
        "content_hash": "current-v3-hash",
        "_rebuild_manifest_created_at": "2026-08-15T00:00:00+00:00",
        "_rebuild_rebaseline_evidence_id": ("run_legacy:rebuild_manifest_legacy"),
    }
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
            authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
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
    assert policy_calls == ["board-1"]
    assert result.counts["policy_constraints_active"] == 2
    assert result.counts["policy_constraints_unadopted_active"] == 0
    assert result.drilldown["policy_constraint_projection"] == {
        "configured": True,
        "status": "completed",
        "activated_count": 2,
        "ended_count": 1,
        "active_count": 2,
        "unadopted_active_count": 0,
        "node_ids": [
            "guideline-revision:r1:rule:a",
            "guideline-revision:r1:rule:b",
        ],
        "replayed": False,
    }
    replay = step(
        RebuildStepInput(
            board_id="board-1",
            manifest_ref="manifest-1",
            source_set_hash="hash-1",
            actor_id="operator",
            operation="rebuild",
            owner_token="token-1",
            candidate_kg_generation_id="gen-2",
            authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
        )
    )
    assert replay.ok is True
    assert policy_calls == ["board-1"]
    assert any("f06-checkpoint" in key for key in store.rows)
    checkpoint = next(row for key, row in store.rows.items() if "f06-checkpoint" in key)
    persisted_source = checkpoint["command"]["source_rows"][0]
    assert persisted_source["content_hash"] == "current-v3-hash"
    assert persisted_source["_rebuild_rebaseline_evidence_id"] == (
        "run_legacy:rebuild_manifest_legacy"
    )
    with sqlite3.connect(str(adapter._path())) as connection:  # noqa: SLF001
        queue_payload = json.loads(
            connection.execute(
                "SELECT payload FROM consolidation_queue "
                "WHERE board_id='board-1' AND artifact_id='story-1'"
            ).fetchone()[0]
        )
    assert queue_payload["_rebuild_membership"] == {
        "content_hash": "current-v3-hash",
        "run_id": "manifest-1",
        "source_ref": "story:story-1",
        "source_version": "3",
    }
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


def test_f06_policy_constraint_rebuild_failure_is_fail_closed(
    monkeypatch, tmp_path: Path
) -> None:
    from okto_pulse.core.kg import canonical_cognitive_preservation as cognitive
    from okto_pulse.core.services import application_kg
    from okto_pulse.community.adapters import kg_runtime

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
        "queue_observation",
        lambda self, board_id, **_kwargs: (0, None),
    )

    projection_calls: list[str] = []
    failure_mode = {"enabled": True}

    def project_constraints(board_id: str) -> PolicyConstraintProjectionResult:
        projection_calls.append(board_id)
        if failure_mode["enabled"]:
            raise RuntimeError(
                "projection unavailable: C:\\secret\\pulse.db?token=private"
            )
        return PolicyConstraintProjectionResult(
            board_id=board_id,
            operation="rebuild",
            event_id=None,
            active_count=0,
            activated_count=0,
            ended_count=0,
            unadopted_active_count=0,
            replayed=True,
            node_ids=(),
        )

    candidate_path = tmp_path / "kg" / "boards" / "board-1" / "graph.lbug"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_bytes(b"failed-candidate")
    monkeypatch.setattr(
        CommunityRebuildEffects,
        "_compensate_exact_relational_commits",
        staticmethod(
            lambda command, *, mutation_guard: (
                build_exact_consolidation_compensation_binding(
                    board_id=command.board_id,
                    source=f"rebuild:{command.manifest_ref}",
                    reservation_lineage_id=str(command.reservation_lineage_id),
                    result=None,
                )
            )
        ),
    )
    monkeypatch.setattr(
        kg_runtime,
        "board_kuzu_path",
        lambda _board_id: candidate_path,
    )
    discard = CandidateDiscardProbe(candidate_path)
    adapter = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=DictArtifactStore(),
        quarantine_restore=discard,
        drain_timeout_seconds=0.05,
        drain_hard_timeout_seconds=0.1,
        drain_poll_interval_seconds=0.001,
        policy_constraint_rebuild=project_constraints,
    )
    step = adapter.build_step_adapter(
        lambda _request: ({"artifact_type": "story", "id": "story-1"},)
    )
    result = step(
        RebuildStepInput(
            board_id="board-1",
            manifest_ref="manifest-1",
            source_set_hash="hash-1",
            actor_id="operator",
            operation="rebuild",
            owner_token="token-1",
            previous_kg_generation_id="gen-1",
            candidate_kg_generation_id="gen-2",
            authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
        )
    )

    assert result.ok is False
    assert (
        result.detail == "promotion_failed:"
        "policy_constraint_projection_failed:RuntimeError"
    )
    assert result.current_kg_generation_id == "gen-1"
    assert result.previous_kg_generation_id == "gen-1"
    assert result.drilldown["rebuild_processor"] == {
        "state": "failed",
        "code": "promotion_failed",
        "promotion_allowed": False,
        "compensation_actions": [
            "cancel_enqueued_sources",
            "compensate_exact_relational_commits",
            "demote_candidate_generation",
            "restore_quarantine",
            "discard_candidate_generation",
        ],
    }
    assert result.drilldown["policy_constraint_projection"] == {
        "configured": True,
        "status": "failed",
        "code": "policy_constraint_projection_failed:RuntimeError",
    }
    rendered = repr(result.drilldown)
    assert "secret" not in rendered
    assert "token=private" not in rendered
    checkpoint = adapter._rebuild_checkpoint_cache["f06:manifest-1"]  # noqa: SLF001
    assert checkpoint.state.value == "failed"
    assert discard.calls
    assert not candidate_path.exists()
    assert all(
        receipt.effect != "promote" or not receipt.ok
        for receipt in checkpoint.receipts.values()
    )

    # A terminal run id is deliberately non-retriable: changing the external
    # condition cannot replace its durable failed receipt.  Retry requires a
    # fresh manifest/run id, which receives an independent effect namespace.
    failure_mode["enabled"] = False
    same_run = step(
        RebuildStepInput(
            board_id="board-1",
            manifest_ref="manifest-1",
            source_set_hash="hash-1",
            actor_id="operator",
            operation="rebuild",
            owner_token="token-1",
            previous_kg_generation_id="gen-1",
            candidate_kg_generation_id="gen-2",
            authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
        )
    )
    assert same_run.ok is False
    assert projection_calls == ["board-1"]

    fresh_run = step(
        RebuildStepInput(
            board_id="board-1",
            manifest_ref="manifest-2",
            source_set_hash="hash-1",
            actor_id="operator",
            operation="rebuild",
            owner_token="token-1",
            previous_kg_generation_id="gen-1",
            candidate_kg_generation_id="gen-3",
            authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
        )
    )
    assert fresh_run.ok is True
    assert projection_calls == ["board-1", "board-1"]


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
            authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
        )
    )
    assert result.ok is False
    assert result.detail is not None and result.detail.startswith("salvage_pending")
    assert called["quarantine"] == 0


def test_f06_build_step_honors_cooperative_cancellation_before_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    called = {"quarantine": 0, "renew": 0}

    def quarantine(self, *, board_id, reason):  # noqa: ANN001
        del self, board_id, reason
        called["quarantine"] += 1
        return ()

    def renew() -> bool:
        called["renew"] += 1
        return True

    monkeypatch.setattr(
        CommunityBoardRebuildIngestionAdapter,
        "prepare_board_graph_storage",
        quarantine,
    )
    adapter = CommunityBoardRebuildIngestionAdapter(
        db_path=_queue_db(tmp_path),
        artifact_store=DictArtifactStore(),
    )

    result = adapter.build_step_adapter(lambda _request: ())(
        RebuildStepInput(
            board_id="board-1",
            manifest_ref="manifest-cancelled",
            source_set_hash="hash",
            actor_id="operator",
            operation="rebuild",
            owner_token="token",
            cancel_requested=lambda: True,
            lease_renew=renew,
            authorized_confirmation_ref=AUTHORIZED_CONFIRMATION_REF,
        )
    )

    assert result.ok is False
    assert result.detail == "cancelled:cancellation requested"
    assert called == {"quarantine": 0, "renew": 2}
