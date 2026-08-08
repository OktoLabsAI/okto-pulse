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


def _policy_projection_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if (
        isinstance(code, str)
        and code.startswith("policy_constraint_")
        and code.replace("_", "").isalnum()
        and len(code) <= 120
    ):
        return code
    return f"policy_constraint_projection_failed:{type(exc).__name__}"


def _policy_projection_details(
    result: object,
    *,
    board_id: str,
) -> dict[str, object]:
    active_count = int(getattr(result, "active_count"))
    activated_count = int(getattr(result, "activated_count"))
    ended_count = int(getattr(result, "ended_count"))
    unadopted_active_count = int(
        getattr(result, "unadopted_active_count")
    )
    node_ids = tuple(str(value) for value in getattr(result, "node_ids"))
    if (
        getattr(result, "board_id", None) != board_id
        or getattr(result, "operation", None) != "rebuild"
        or getattr(result, "event_id", object()) is not None
        or any(
            value < 0
            for value in (
                active_count,
                activated_count,
                ended_count,
                unadopted_active_count,
            )
        )
        or unadopted_active_count != 0
        or active_count != len(node_ids)
        or len(set(node_ids)) != len(node_ids)
        or any(not value for value in node_ids)
    ):
        raise RuntimeError("policy_constraint_rebuild_result_invalid")
    return {
        "configured": True,
        "status": "completed",
        "activated_count": activated_count,
        "ended_count": ended_count,
        "active_count": active_count,
        "unadopted_active_count": unadopted_active_count,
        "node_ids": list(node_ids),
        "replayed": bool(getattr(result, "replayed")),
    }


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
        depth, blocking_reason = self._owner.queue_observation(command.board_id)
        return QueueObservation(
            depth=depth,
            observed_at=datetime.now(timezone.utc),
            sequence=after_sequence + 1,
            blocking_reason=blocking_reason,
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

        snapshot_receipt = self._load_receipt(command, f"{command.run_id}:snapshot")
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
        # The enclosing KGRebuildService performs the generation write only
        # after this step returns ``ok=True``.  Reconcile the separate
        # relational-policy derivative here, before issuing that authorization;
        # a failed projection therefore enters the processor's ordinary
        # PROMOTION_FAILED compensation path and cannot expose a promoted
        # generation with stale policy Constraints.
        projection_details: dict[str, object] = {
            "configured": self._owner.policy_constraint_rebuild is not None,
            "status": "legacy_not_configured",
        }
        if self._owner.policy_constraint_rebuild is not None:
            try:
                projection_details = _policy_projection_details(
                    self._owner.policy_constraint_rebuild(command.board_id),
                    board_id=command.board_id,
                )
            except Exception as exc:
                code = _policy_projection_error_code(exc)
                return self._store_receipt(
                    command,
                    RebuildEffectReceipt(
                        effect_key,
                        "promote",
                        False,
                        code=code,
                        details={
                            "candidate_generation_id": (
                                command.candidate_generation_id
                            ),
                            "policy_constraint_projection": {
                                "configured": True,
                                "status": "failed",
                                "code": code,
                            },
                        },
                    ),
                )
        return self._store_receipt(
            command,
            RebuildEffectReceipt(
                effect_key,
                "promote",
                True,
                code="promotion_authorized",
                details={
                    "candidate_generation_id": command.candidate_generation_id,
                    "policy_constraint_projection": projection_details,
                },
            ),
        )

    def compensate(
        self, command: CompensationCommand, *, effect_key: str
    ) -> RebuildEffectReceipt:
        rebuild_command = self._checkpoint_command(command.run_id)
        existing = self._load_receipt(rebuild_command, effect_key)
        if existing is not None:
            return existing
        details: dict[str, object] = {
            "actions": [action.value for action in command.actions]
        }
        ok = True
        had_previous_graph = self._quarantine_had_affected_files(
            rebuild_command
        )
        queue_fenced = True
        if CompensationAction.CANCEL_ENQUEUED_SOURCES in command.actions:
            queue_result = self._owner.compensate_pending_sources(
                board_id=command.board_id,
                run_id=rebuild_command.manifest_ref or rebuild_command.run_id,
            )
            details["queue"] = queue_result
            queue_fenced = int(queue_result.get("active_remaining", -1)) == 0
            ok = ok and queue_fenced
        if CompensationAction.DEMOTE_CANDIDATE_GENERATION in command.actions:
            details["candidate_demotion"] = {
                "status": "not_persisted_by_effect_adapter",
                "candidate_generation_id": rebuild_command.candidate_generation_id,
            }

        wants_discard = (
            CompensationAction.DISCARD_CANDIDATE_GENERATION
            in command.actions
        )
        restored: dict[str, object] | None = None
        if wants_discard and not had_previous_graph and queue_fenced:
            # A fresh board has no predecessor to restore.  Discard and prove
            # physical absence before the no-op restore phase.
            discarded = self._discard_fresh_candidate(rebuild_command)
            details["candidate_discard"] = discarded
            ok = ok and bool(
                discarded.get("status")
                in {"already_absent", "discarded"}
            )
        elif wants_discard and not queue_fenced:
            details["candidate_discard"] = {
                "status": "blocked_by_active_queue",
                "candidate_generation_id": (
                    rebuild_command.candidate_generation_id
                ),
            }

        if (
            CompensationAction.RESTORE_QUARANTINE in command.actions
            and queue_fenced
        ):
            restored = self._restore_latest_quarantine(rebuild_command)
            details["quarantine_restore"] = restored
            ok = ok and bool(restored.get("ok", False))
        elif CompensationAction.RESTORE_QUARANTINE in command.actions:
            details["quarantine_restore"] = {
                "ok": False,
                "reason": "active_queue_not_fenced",
            }

        if wants_discard and had_previous_graph:
            # ``apply_rebuild_compensation`` is one governed backup-swap:
            # candidate live files are moved to this new quarantine before the
            # predecessor is copied back and open-validated.  Expose that
            # DISCARD→RESTORE evidence explicitly; a second purge here would
            # destroy the restored predecessor.
            report = (
                dict(restored.get("report", {}))
                if restored is not None
                else {}
            )
            backup_quarantine_id = report.get("backup_quarantine_id")
            atomic_ok = bool(
                restored is not None
                and restored.get("ok", False)
                and backup_quarantine_id
            )
            details["candidate_discard"] = {
                "status": (
                    "discarded_by_atomic_backup_swap"
                    if atomic_ok
                    else "atomic_backup_swap_unconfirmed"
                ),
                "candidate_generation_id": (
                    rebuild_command.candidate_generation_id
                ),
                "candidate_quarantine_id": backup_quarantine_id,
                "live_candidate_absent_before_restore": atomic_ok,
            }
            ok = ok and atomic_ok
        receipt = RebuildEffectReceipt(
            effect_key,
            "compensate",
            ok,
            code="compensated" if ok else "compensation_incomplete",
            details=details,
        )
        return self._store_receipt(rebuild_command, receipt)

    def _quarantine_had_affected_files(
        self,
        command: RebuildCommand,
    ) -> bool:
        receipt = self._load_receipt(
            command,
            f"{command.run_id}:quarantine",
        )
        return bool(
            tuple(dict(receipt.details).get("affected_files", ()))
            if receipt is not None
            else ()
        )

    def _discard_fresh_candidate(
        self,
        command: RebuildCommand,
    ) -> dict[str, object]:
        restore = self._quarantine_restore
        if restore is None:
            try:
                from okto_pulse.core.services.application_kg import (
                    get_current_provider_registry,
                )

                restore = (
                    get_current_provider_registry().require_quarantine_restore()
                )
                self._quarantine_restore = restore
            except Exception as exc:
                return {
                    "status": "discard_failed",
                    "code": (
                        "rebuild_candidate_discard_unavailable:"
                        f"{type(exc).__name__}"
                    ),
                    "candidate_generation_id": (
                        command.candidate_generation_id
                    ),
                }
        discard = getattr(restore, "discard_rebuild_candidate", None)
        if not callable(discard):
            return {
                "status": "discard_failed",
                "code": "governed_candidate_discard_unavailable",
                "candidate_generation_id": command.candidate_generation_id,
            }
        try:
            result = dict(
                discard(
                    expected_board_id=command.board_id,
                    run_id=command.run_id,
                    owner_token=command.owner_token,
                )
            )
        except Exception as exc:
            logger.error(
                "kg.rebuild.candidate_discard_failed board=%s error_type=%s",
                command.board_id,
                type(exc).__name__,
                extra={
                    "event": "kg.rebuild.candidate_discard_failed",
                    "board_id": command.board_id,
                    "error_type": type(exc).__name__,
                },
            )
            return {
                "status": "discard_failed",
                "code": f"rebuild_candidate_discard_failed:{type(exc).__name__}",
                "candidate_generation_id": command.candidate_generation_id,
            }
        result["candidate_generation_id"] = (
            command.candidate_generation_id
        )
        return result

    def _checkpoint_command(self, run_id: str) -> RebuildCommand:
        checkpoint = self._owner._rebuild_checkpoint_cache.get(run_id)
        if checkpoint is None:
            raise RuntimeError(f"missing checkpoint for compensation: {run_id}")
        return checkpoint.command

    def _restore_latest_quarantine(self, command: RebuildCommand) -> dict[str, object]:
        quarantine_receipt = self._load_receipt(command, f"{command.run_id}:quarantine")
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
            build_plan = getattr(self._quarantine_restore, "plan", None)
            apply_compensation = getattr(
                self._quarantine_restore,
                "apply_rebuild_compensation",
                None,
            )
            if not callable(build_plan) or not callable(apply_compensation):
                return {
                    "ok": False,
                    "reason": "governed_quarantine_restore_unavailable",
                    "quarantine_id": quarantine_id,
                }
            plan = build_plan(quarantine_id)
            expected_files = tuple(
                str(getattr(entry, "name", ""))
                for entry in tuple(getattr(plan, "files", ()))
            )
            if (
                getattr(plan, "quarantine_id", None) != quarantine_id
                or getattr(plan, "board_id", None) != command.board_id
                or not expected_files
                or any(not value for value in expected_files)
            ):
                return {
                    "ok": False,
                    "reason": "quarantine_restore_plan_invalid",
                    "quarantine_id": quarantine_id,
                }
            report = apply_compensation(
                quarantine_id,
                expected_board_id=command.board_id,
                run_id=command.run_id,
                owner_token=command.owner_token,
            )
        except Exception as exc:
            logger.error(
                "kg.rebuild.quarantine_restore_failed board=%s "
                "quarantine_id=%s error_type=%s",
                command.board_id,
                quarantine_id,
                type(exc).__name__,
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
                "quarantine_id": quarantine_id,
            }
        restored_files = tuple(
            str(value) for value in getattr(report, "restored_files", ())
        )
        backup_quarantine_id = str(
            getattr(report, "backup_quarantine_id", "") or ""
        )
        report_ok = bool(
            getattr(report, "applied", False)
            and getattr(report, "open_validated", False)
            and getattr(report, "quarantine_id", None) == quarantine_id
            and getattr(report, "board_id", None) == command.board_id
            and backup_quarantine_id
            and restored_files == expected_files
        )
        return {
            "ok": report_ok,
            "reason": (
                "restored" if report_ok else "quarantine_restore_report_invalid"
            ),
            "quarantine_id": quarantine_id,
            "report": {
                "quarantine_id": quarantine_id,
                "board_id": command.board_id,
                "applied": bool(getattr(report, "applied", False)),
                "backup_quarantine_id": backup_quarantine_id or None,
                "restored_files": list(restored_files),
                "restored_count": len(restored_files),
                "open_validated": bool(
                    getattr(report, "open_validated", False)
                ),
            },
        }

    def record_audit(
        self, outcome: RebuildOutcome, *, effect_key: str
    ) -> RebuildEffectReceipt:
        command = (
            self._checkpoint_command(outcome.run_id)
            if outcome.run_id in self._owner._rebuild_checkpoint_cache
            else RebuildCommand(
                run_id=outcome.run_id,
                board_id=outcome.board_id,
                manifest_ref="",
                operation="rebuild",
                actor_id="system",
                reason="precondition",
            )
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
