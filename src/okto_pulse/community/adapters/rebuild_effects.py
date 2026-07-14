"""Local First effect implementation for the Core rebuild processor."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from okto_pulse.core.application.rebuild_processor import (
    CompensationAction,
    CompensationCommand,
    QueueObservation,
    RebuildCheckpoint,
    RebuildCommand,
    RebuildEffectReceipt,
    RebuildOutcome,
    RebuildState,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import (
    RebuildAuditArtifactStore,
    RebuildAuditKey,
)

if TYPE_CHECKING:
    from okto_pulse.community.adapters.board_rebuild_ingestion import (
        CommunityBoardRebuildIngestionAdapter,
    )


logger = logging.getLogger(__name__)


def _safe(value: object) -> object:
    return json.loads(json.dumps(value, default=str))


def _command_payload(command: RebuildCommand) -> dict[str, object]:
    return {
        "run_id": command.run_id,
        "board_id": command.board_id,
        "manifest_ref": command.manifest_ref,
        "operation": command.operation,
        "actor_id": command.actor_id,
        "reason": command.reason,
        "source_rows": _safe(command.source_rows),
        "previous_generation_id": command.previous_generation_id,
        "candidate_generation_id": command.candidate_generation_id,
        "salvage_pending": command.salvage_pending,
    }


def _receipt_payload(receipt: RebuildEffectReceipt) -> dict[str, object]:
    return {
        "effect_key": receipt.effect_key,
        "effect": receipt.effect,
        "ok": receipt.ok,
        "code": receipt.code,
        "details": _safe(dict(receipt.details)),
    }


def _receipt_from_payload(payload: dict[str, Any]) -> RebuildEffectReceipt:
    return RebuildEffectReceipt(
        effect_key=str(payload["effect_key"]),
        effect=str(payload["effect"]),
        ok=bool(payload["ok"]),
        code=str(payload.get("code", "ok")),
        details=dict(payload.get("details", {})),
    )


class CommunityRebuildEffects:
    def __init__(
        self,
        owner: "CommunityBoardRebuildIngestionAdapter",
        *,
        artifact_store: RebuildAuditArtifactStore | None = None,
        quarantine_restore: Any | None = None,
    ) -> None:
        self._owner = owner
        self._artifact_store = artifact_store or owner.artifact_store
        self._quarantine_restore = quarantine_restore or owner.quarantine_restore
        if self._artifact_store is None:
            from okto_pulse.core.services.application_kg import (
                get_current_provider_registry,
            )

            registry = get_current_provider_registry()
            self._artifact_store = registry.require_rebuild_audit_artifact_store()

    def _key(self, command: RebuildCommand, artifact_id: str) -> RebuildAuditKey:
        return RebuildAuditKey(
            namespace="run_audit",
            board_id=command.board_id,
            artifact_id=artifact_id,
        )

    @staticmethod
    def _effect_id(effect_key: str) -> str:
        digest = hashlib.sha256(effect_key.encode("utf-8")).hexdigest()[:24]
        return f"f06-effect-{digest}"

    @staticmethod
    def _checkpoint_id(run_id: str) -> str:
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
        return f"f06-checkpoint-{digest}"

    def load_checkpoint(self, run_id: str) -> RebuildCheckpoint | None:
        cached = self._owner._rebuild_checkpoint_cache.get(run_id)
        if cached is not None:
            return cached
        if self._artifact_store is None:
            return None
        board_id = self._owner._rebuild_run_boards.get(run_id)
        if board_id is None:
            return None
        payload = self._artifact_store.read_json(
            RebuildAuditKey(
                namespace="run_audit",
                board_id=board_id,
                artifact_id=self._checkpoint_id(run_id),
            )
        )
        if payload is None:
            return None
        command_payload = dict(payload["command"])
        command_payload["source_rows"] = tuple(command_payload.get("source_rows", ()))
        command = RebuildCommand(**command_payload)
        receipts = {
            str(key): _receipt_from_payload(value)
            for key, value in dict(payload.get("receipts", {})).items()
        }
        checkpoint = RebuildCheckpoint(
            command=command,
            state=RebuildState(str(payload["state"])),
            started_at=datetime.fromisoformat(str(payload["started_at"])),
            last_progress_at=datetime.fromisoformat(str(payload["last_progress_at"])),
            best_queue_depth=payload.get("best_queue_depth"),
            last_sequence=int(payload.get("last_sequence", 0)),
            queue_progress_events=int(payload.get("queue_progress_events", 0)),
            queue_grace_applied=bool(payload.get("queue_grace_applied", False)),
            queue_grace_reason=payload.get("queue_grace_reason"),
            receipts=receipts,
        )
        self._owner._rebuild_checkpoint_cache[run_id] = checkpoint
        return checkpoint

    def save_checkpoint(self, checkpoint: RebuildCheckpoint) -> None:
        run_id = checkpoint.command.run_id
        self._owner._rebuild_checkpoint_cache[run_id] = checkpoint
        self._owner._rebuild_run_boards[run_id] = checkpoint.command.board_id
        if self._artifact_store is None:
            return
        self._artifact_store.write_json_atomic(
            self._key(checkpoint.command, self._checkpoint_id(run_id)),
            {
                "kind": "f06_rebuild_checkpoint",
                "command": _command_payload(checkpoint.command),
                "state": checkpoint.state.value,
                "started_at": checkpoint.started_at.isoformat(),
                "last_progress_at": checkpoint.last_progress_at.isoformat(),
                "best_queue_depth": checkpoint.best_queue_depth,
                "last_sequence": checkpoint.last_sequence,
                "queue_progress_events": checkpoint.queue_progress_events,
                "queue_grace_applied": checkpoint.queue_grace_applied,
                "queue_grace_reason": checkpoint.queue_grace_reason,
                "receipts": {
                    key: _receipt_payload(receipt)
                    for key, receipt in checkpoint.receipts.items()
                },
            },
        )

    def _load_receipt(
        self, command: RebuildCommand, effect_key: str
    ) -> RebuildEffectReceipt | None:
        cached = self._owner._rebuild_effect_cache.get(effect_key)
        if cached is not None:
            return cached
        if self._artifact_store is None:
            return None
        payload = self._artifact_store.read_json(
            self._key(command, self._effect_id(effect_key))
        )
        if payload is None:
            return None
        receipt = _receipt_from_payload(payload)
        self._owner._rebuild_effect_cache[effect_key] = receipt
        return receipt

    def _store_receipt(
        self, command: RebuildCommand, receipt: RebuildEffectReceipt
    ) -> RebuildEffectReceipt:
        self._owner._rebuild_effect_cache[receipt.effect_key] = receipt
        if self._artifact_store is not None:
            self._artifact_store.write_json_atomic(
                self._key(command, self._effect_id(receipt.effect_key)),
                _receipt_payload(receipt),
            )
        return receipt

    def snapshot(
        self, command: RebuildCommand, *, effect_key: str
    ) -> RebuildEffectReceipt:
        existing = self._load_receipt(command, effect_key)
        if existing is not None:
            return existing
        from okto_pulse.core.kg.canonical_cognitive_preservation import (
            snapshot_canonical_cognitive,
        )

        snapshot = snapshot_canonical_cognitive(command.board_id)
        logger.info(
            "kg.rebuild.cognitive_snapshot board=%s readable=%s nodes=%d edges=%d",
            command.board_id,
            snapshot.readable,
            len(snapshot.nodes),
            len(snapshot.edges),
            extra={
                "event": "kg.rebuild.cognitive_snapshot",
                "board_id": command.board_id,
                "readable": snapshot.readable,
                "node_count": len(snapshot.nodes),
                "edge_count": len(snapshot.edges),
            },
        )
        return self._store_receipt(
            command,
            RebuildEffectReceipt(
                effect_key,
                "snapshot",
                True,
                details={
                    "board_id": snapshot.board_id,
                    "readable": snapshot.readable,
                    "nodes": _safe(snapshot.nodes),
                    "edges": _safe(snapshot.edges),
                },
            ),
        )

    def quarantine(
        self, command: RebuildCommand, *, effect_key: str
    ) -> RebuildEffectReceipt:
        existing = self._load_receipt(command, effect_key)
        if existing is not None:
            return existing
        try:
            report = self._owner.prepare_board_graph_storage_report(
                board_id=command.board_id,
                reason=f"explicit_rebuild:{command.manifest_ref or command.operation}",
            )
            affected = tuple(ref.token for ref in report.affected_storage_refs)
            receipt = RebuildEffectReceipt(
                effect_key,
                "quarantine",
                True,
                details={
                    "affected_files": list(affected),
                    "quarantine_ref": report.quarantine_ref,
                },
            )
        except Exception as exc:
            logger.warning(
                "kg.rebuild.quarantine_failed board=%s error=%s",
                command.board_id,
                exc,
                extra={
                    "event": "kg.rebuild.quarantine_failed",
                    "board_id": command.board_id,
                    "error_type": type(exc).__name__,
                },
            )
            receipt = RebuildEffectReceipt(
                effect_key,
                "quarantine",
                False,
                code=f"graph_prepare_failed:{type(exc).__name__}",
                details={"error": str(exc)},
            )
        return self._store_receipt(command, receipt)

    def enqueue(
        self, command: RebuildCommand, *, effect_key: str
    ) -> RebuildEffectReceipt:
        existing = self._load_receipt(command, effect_key)
        if existing is not None:
            return existing
        try:
            counts = self._owner.enqueue_sources(
                board_id=command.board_id,
                run_id=command.manifest_ref or command.run_id,
                sources=command.source_rows,
            )
            from okto_pulse.core.services.application_kg import (
                signal_consolidation_worker,
            )

            signal_consolidation_worker()
            receipt = RebuildEffectReceipt(
                effect_key,
                "enqueue",
                True,
                details=dict(counts),
            )
        except Exception as exc:
            receipt = RebuildEffectReceipt(
                effect_key,
                "enqueue",
                False,
                code=f"enqueue_failed:{type(exc).__name__}",
                details={"error": str(exc)},
            )
        return self._store_receipt(command, receipt)

    def wait_for_queue_observation(
        self,
        command: RebuildCommand,
        *,
        after_sequence: int,
        max_wait_seconds: float,
    ) -> QueueObservation:
        if max_wait_seconds > 0:
            time.sleep(max_wait_seconds)
        return QueueObservation(
            depth=self._owner.queue_depth(command.board_id),
            observed_at=datetime.now(timezone.utc),
            sequence=after_sequence + 1,
        )

    def restore(
        self, command: RebuildCommand, *, effect_key: str
    ) -> RebuildEffectReceipt:
        existing = self._load_receipt(command, effect_key)
        if existing is not None:
            return existing
        from okto_pulse.core.kg.canonical_cognitive_preservation import (
            STATUS_DEGRADED,
            STATUS_INTEGRITY_ERROR,
            STATUS_UNREADABLE,
            CognitiveSnapshot,
            preservation_summary,
            record_cognitive_loss_fallback,
            restore_canonical_cognitive,
        )

        snapshot_receipt = self._load_receipt(
            command, f"{command.run_id}:snapshot"
        )
        if snapshot_receipt is None:
            return self._store_receipt(
                command,
                RebuildEffectReceipt(
                    effect_key,
                    "restore",
                    False,
                    code="snapshot_receipt_missing",
                ),
            )
        details = dict(snapshot_receipt.details)
        snapshot = CognitiveSnapshot(
            board_id=command.board_id,
            readable=bool(details.get("readable")),
            nodes=list(details.get("nodes", [])),
            edges=list(details.get("edges", [])),
        )
        restored = restore_canonical_cognitive(command.board_id, snapshot)
        summary = preservation_summary(snapshot, restored)
        # Spec MKG-A-S1 (FR5/D4, OR2): literal replay of the durable
        # cognitive source AFTER the snapshot restore — with an unreadable
        # snapshot this is what brings Learning/Alternative/Assumption
        # back. Create-if-absent, so restore+replay never duplicates.
        from okto_pulse.core.kg.canonical_cognitive_preservation import (
            replay_durable_cognitive,
        )

        summary.update(replay_durable_cognitive(command.board_id))
        if summary["status"] in (STATUS_DEGRADED, STATUS_UNREADABLE):
            summary["fallback_holds_recorded"] = record_cognitive_loss_fallback(
                command.board_id, summary
            )
        return self._store_receipt(
            command,
            RebuildEffectReceipt(
                effect_key,
                "restore",
                summary["status"] != STATUS_INTEGRITY_ERROR,
                code=str(summary["status"]),
                details=_safe(summary),
            ),
        )

    def promote(
        self, command: RebuildCommand, *, effect_key: str
    ) -> RebuildEffectReceipt:
        existing = self._load_receipt(command, effect_key)
        if existing is not None:
            return existing
        # The enclosing KGRebuildService performs the generation write only when
        # the step result is ok. This receipt is the Core processor's explicit
        # authorization for that existing atomic promotion point.
        return self._store_receipt(
            command,
            RebuildEffectReceipt(
                effect_key,
                "promote",
                True,
                code="promotion_authorized",
                details={"candidate_generation_id": command.candidate_generation_id},
            ),
        )

    def compensate(
        self, command: CompensationCommand, *, effect_key: str
    ) -> RebuildEffectReceipt:
        rebuild_command = self._checkpoint_command(command.run_id)
        existing = self._load_receipt(rebuild_command, effect_key)
        if existing is not None:
            return existing
        details: dict[str, object] = {"actions": [action.value for action in command.actions]}
        ok = True
        if CompensationAction.CANCEL_ENQUEUED_SOURCES in command.actions:
            details["queue"] = self._owner.compensate_pending_sources(
                board_id=command.board_id,
                run_id=rebuild_command.manifest_ref or rebuild_command.run_id,
            )
        if CompensationAction.RESTORE_QUARANTINE in command.actions:
            restored = self._restore_latest_quarantine(rebuild_command)
            details["quarantine_restore"] = restored
            ok = bool(restored.get("ok", False))
        if CompensationAction.DEMOTE_CANDIDATE_GENERATION in command.actions:
            details["candidate_demotion"] = {
                "status": "not_persisted_by_effect_adapter",
                "candidate_generation_id": rebuild_command.candidate_generation_id,
            }
        if CompensationAction.DISCARD_CANDIDATE_GENERATION in command.actions:
            details["candidate_discard"] = {
                "status": "not_persisted_by_effect_adapter",
                "candidate_generation_id": rebuild_command.candidate_generation_id,
            }
        receipt = RebuildEffectReceipt(
            effect_key,
            "compensate",
            ok,
            code="compensated" if ok else "quarantine_restore_unavailable",
            details=details,
        )
        return self._store_receipt(rebuild_command, receipt)

    def _checkpoint_command(self, run_id: str) -> RebuildCommand:
        checkpoint = self._owner._rebuild_checkpoint_cache.get(run_id)
        if checkpoint is None:
            raise RuntimeError(f"missing checkpoint for compensation: {run_id}")
        return checkpoint.command

    def _restore_latest_quarantine(
        self, command: RebuildCommand
    ) -> dict[str, object]:
        quarantine_receipt = self._load_receipt(
            command, f"{command.run_id}:quarantine"
        )
        affected = list(
            dict(quarantine_receipt.details).get("affected_files", [])
            if quarantine_receipt is not None
            else []
        )
        if not affected:
            return {"ok": True, "reason": "nothing_quarantined"}

        if self._quarantine_restore is None:
            try:
                from okto_pulse.core.services.application_kg import (
                    get_current_provider_registry,
                )

                self._quarantine_restore = (
                    get_current_provider_registry().require_quarantine_restore()
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "reason": f"quarantine_restore_unavailable:{type(exc).__name__}",
                }
        quarantine_id = str(
            dict(quarantine_receipt.details).get("quarantine_ref") or ""
        )
        if not quarantine_id and self._artifact_store is not None:
            manifests = self._artifact_store.list_quarantine_manifests(
                active_after_iso=None,
                base_storage_ref_hint=None,
            )
            matching = [
                item
                for item in manifests
                if str(item.get("board_id", "")) == command.board_id
            ]
            if matching:
                latest = matching[-1]
                quarantine_id = str(
                    latest.get("quarantine_id") or latest.get("id") or ""
                )
        if not quarantine_id:
            return {"ok": False, "reason": "quarantine_id_missing"}
        if self._quarantine_restore is None:
            return {"ok": False, "reason": "quarantine_restore_unavailable"}
        try:
            from okto_pulse.core.kg.async_bridge import run_async_blocking
            from okto_pulse.core.services.application_kg import (
                get_current_provider_registry,
            )

            lifecycle = get_current_provider_registry().graph_lifecycle
            if lifecycle is not None:
                run_async_blocking(lifecycle.close(command.board_id))
            report = self._quarantine_restore.apply(quarantine_id)
        except Exception as exc:
            logger.exception(
                "kg.rebuild.quarantine_restore_failed board=%s quarantine_id=%s",
                command.board_id,
                quarantine_id,
                extra={
                    "event": "kg.rebuild.quarantine_restore_failed",
                    "board_id": command.board_id,
                    "quarantine_id": quarantine_id,
                    "error_type": type(exc).__name__,
                },
            )
            return {
                "ok": False,
                "reason": f"quarantine_restore_failed:{type(exc).__name__}",
                "error": str(exc),
                "quarantine_id": quarantine_id,
            }
        return {
            "ok": bool(
                getattr(report, "applied", False)
                and getattr(report, "open_validated", False)
            ),
            "quarantine_id": quarantine_id,
            "report": _safe(getattr(report, "__dict__", {})),
        }

    def record_audit(
        self, outcome: RebuildOutcome, *, effect_key: str
    ) -> RebuildEffectReceipt:
        command = self._checkpoint_command(outcome.run_id) if outcome.run_id in self._owner._rebuild_checkpoint_cache else RebuildCommand(
            run_id=outcome.run_id,
            board_id=outcome.board_id,
            manifest_ref="",
            operation="rebuild",
            actor_id="system",
            reason="precondition",
        )
        existing = self._load_receipt(command, effect_key)
        if existing is not None:
            return existing
        receipt = RebuildEffectReceipt(
            effect_key,
            "audit",
            True,
            details={
                "state": outcome.state.value,
                "code": outcome.code.value,
                "promotion_allowed": outcome.promotion_allowed,
                "compensation_actions": [
                    action.value for action in outcome.compensation_actions
                ],
                "detail": outcome.detail,
            },
        )
        return self._store_receipt(command, receipt)


__all__ = ["CommunityRebuildEffects"]
