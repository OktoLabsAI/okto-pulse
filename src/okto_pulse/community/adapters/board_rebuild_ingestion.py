"""Community-owned production rebuild ingestion adapter for KG-02.

Wires the KG-02 rebuild service to the existing consolidation pipeline
without reimplementing materialization. Strategy is **enqueue-then-wake**:

1. The rebuild service holds the KG-01 admin single-writer lock.
2. This adapter receives the source set already enumerated by
   the BoardSourceReader port (KG-02.2 ``RebuildSourceEnumerator`` passes
   it forward through ``sources_payload``).
3. For each source row we UPSERT into ``ConsolidationQueue`` with the
   same dedup semantics as ``ConsolidationEnqueuer`` (insert if new,
   reset terminal rows to pending, leave pending/claimed alone). Rows use
   high priority because explicit recovery must not sit behind unrelated
   corrupt-board backlog.
4. We signal the consolidation worker so it picks up the new rows
   without waiting for its heartbeat.
5. The structural_hash / source_hash come from KG-02.5
   ``DeterministicStructuralRebuilder`` over the same source set so the
   rebuild report carries a deterministic receipt.

Trade-off documented:
* The adapter returns ``ok=True`` as soon as the rows are enqueued.
  Actual KG mutation happens asynchronously inside the consolidation
  worker (which has its own per-board commit lock that nests safely
  inside the KG-01 admin lock — the existing worker already serialises
  board-by-board). For E2E we expose a ``drain_until_idle`` helper that
  can be wired by callers that want synchronous wait-until-done
  semantics.
* The adapter uses stdlib ``sqlite3`` for the UPSERT because the KG-01
  ``rebuild_step_adapter`` callable is synchronous. The same SQLite
  file is shared with the async SQLAlchemy engine; readers and writers
  are serialised by SQLite's own file-level locks.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from okto_pulse.core.kg.async_bridge import run_async_blocking
from okto_pulse.core.kg.board_rebuild_adapter import (
    DETERMINISTIC_SOURCE_ARTIFACT_TYPES,
    expected_layers_from_sources,
    queue_artifact_type,
)
from okto_pulse.core.kg.interfaces.graph_lifecycle import PurgeReport

from okto_pulse.community.adapters.board_source_reader import resolve_pulse_db_path

logger = logging.getLogger("okto_pulse.community.board_rebuild_ingestion")


@dataclass(frozen=True, slots=True)
class CommunityBoardRebuildIngestionAdapter:
    """Sync rebuild step adapter that enqueues sources for the existing
    consolidation worker to drain. Produces a deterministic structural
    hash + source hash via KG-02.5 primitives so the rebuild report and
    KG-02.4 promotion path receive a real receipt — not a stub one."""

    db_path: Path | None = None
    db_path_provider: Callable[[], Path] | None = None
    drain_timeout_seconds: float = 900.0
    drain_poll_interval_seconds: float = 0.5
    drain_final_grace_seconds: float = 180.0
    drain_low_depth_threshold: int = 10
    # Teto ABSOLUTO do drain (campo 2026-06-10): o timeout acima é a janela
    # de ESTAGNAÇÃO (sem progresso), não o teto total. Um board grande
    # (520+ sources a ~6 entries/min) leva >1h para drenar — com teto fixo
    # de 15min o rebuild SEMPRE falhava (queue_drain_timeout), a generation
    # nunca promovia e o cognitive pending nunca materializava (sem badges),
    # apesar de o worker completar o grafo minutos depois. Enquanto a fila
    # PROGRIDE o drain continua; este teto só protege contra um worker
    # zumbi que progride para sempre sem terminar.
    drain_hard_timeout_seconds: float = 14400.0
    artifact_store: Any | None = None
    quarantine_restore: Any | None = None
    salvage_pending_provider: Callable[[str], bool] | None = None
    _rebuild_effect_cache: dict[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )
    _rebuild_checkpoint_cache: dict[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )
    _rebuild_run_boards: dict[str, str] = field(
        default_factory=dict, compare=False, repr=False
    )

    def _path(self) -> Path:
        if self.db_path is not None:
            return Path(self.db_path)
        if self.db_path_provider is not None:
            return Path(self.db_path_provider())
        return resolve_pulse_db_path()

    def prepare_board_graph_storage(
        self,
        *,
        board_id: str,
        reason: str,
    ) -> tuple[str, ...]:
        report = self.prepare_board_graph_storage_report(
            board_id=board_id,
            reason=reason,
        )
        return tuple(ref.token for ref in report.affected_storage_refs)

    def prepare_board_graph_storage_report(
        self,
        *,
        board_id: str,
        reason: str,
    ) -> PurgeReport:
        """Quarantine existing board graph files for an explicit rebuild.

        The bootstrap path is fail-closed and must never purge an existing
        graph just because opening it failed. A confirmed rebuild is different:
        the operator already requested replacement, so we move the current
        graph files to quarantine before the deterministic worker bootstraps a
        fresh graph. If quarantine fails, the rebuild step fails and preserves
        the original files.
        """

        from okto_pulse.community.adapters.kg_runtime import board_kuzu_path

        path = board_kuzu_path(board_id)
        targets: list[Path] = []
        if path.exists():
            targets.append(path)
        if path.parent.exists():
            targets.extend(sorted(path.parent.glob(path.name + ".*")))
        if not targets:
            return PurgeReport(
                board_id=board_id,
                status="noop",
                reason=reason,
            )

        from okto_pulse.core.services.application_kg import get_current_provider_registry

        registry = get_current_provider_registry()
        report = run_async_blocking(
            registry.graph_lifecycle.purge(board_id, reason=reason)
        )
        still_present = [p for p in targets if p.exists()]
        if still_present:
            raise RuntimeError(
                "explicit rebuild could not quarantine existing graph files: "
                + ", ".join(str(p) for p in still_present)
            )
        return report

    def enqueue_sources(
        self,
        *,
        board_id: str,
        run_id: str,
        sources: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """UPSERT one ConsolidationQueue row per source. Returns counts
        bucketed by (inserted | reset_to_pending | left_alone). Pending and
        claimed rows are left alone, preserving attempts, errors, retry and
        claim ownership fields; terminal/retryable rows are reset to pending.
        This supersedes older behavior that reset pending/claimed rows; active
        rows now intentionally count as ``left_alone``. Uses
        ``priority='high'`` because an explicit rebuild is an operator recovery
        action; it must preempt unrelated backlog from other boards that may
        themselves be corrupt."""

        counts = {"inserted": 0, "reset_to_pending": 0, "left_alone": 0}
        if not sources:
            return counts

        with sqlite3.connect(str(self._path()), timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            for row in sources:
                artifact_type = str(row.get("artifact_type", ""))
                artifact_id = str(row.get("id", ""))
                if artifact_type not in DETERMINISTIC_SOURCE_ARTIFACT_TYPES:
                    continue
                queued_artifact_type = queue_artifact_type(artifact_type)
                if not artifact_id:
                    continue
                queue_id = str(uuid.uuid4())
                written = conn.execute(
                    "INSERT INTO consolidation_queue "
                    "(id, board_id, artifact_type, artifact_id, priority, "
                    "source, status, triggered_at, attempts) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', datetime('now'), 0) "
                    "ON CONFLICT(board_id, artifact_type, artifact_id) "
                    "DO UPDATE SET "
                    "status='pending', attempts=0, last_error=NULL, "
                    "claimed_by_session_id=NULL, claimed_at=NULL, "
                    "worker_id=NULL, claim_timeout_at=NULL, "
                    "next_retry_at=NULL, priority=excluded.priority, "
                    "source=excluded.source "
                    "WHERE consolidation_queue.status NOT IN "
                    "('pending', 'claimed') "
                    "RETURNING id",
                    (
                        queue_id,
                        board_id,
                        queued_artifact_type,
                        artifact_id,
                        "high",
                        f"rebuild:{run_id}",
                    ),
                ).fetchone()
                if written is None:
                    counts["left_alone"] += 1
                elif written["id"] == queue_id:
                    counts["inserted"] += 1
                else:
                    counts["reset_to_pending"] += 1
            conn.commit()
        return counts

    def queue_depth(self, board_id: str) -> int:
        """Return one technical queue observation for the Core state machine."""

        with sqlite3.connect(str(self._path()), timeout=5.0) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM consolidation_queue "
                "WHERE board_id=? AND status IN ('pending', 'claimed')",
                (board_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def compensate_pending_sources(self, *, board_id: str, run_id: str) -> dict[str, int]:
        """Fail pending rows from this rebuild while preserving active claims."""

        with sqlite3.connect(str(self._path()), timeout=10.0) as conn:
            cursor = conn.execute(
                "UPDATE consolidation_queue SET status='failed', "
                "last_error='rebuild_compensated', next_retry_at=NULL "
                "WHERE board_id=? AND source=? AND status='pending'",
                (board_id, f"rebuild:{run_id}"),
            )
            conn.commit()
        return {"pending_compensated": max(0, int(cursor.rowcount or 0))}

    def build_step_adapter(self, source_resolver):
        """Compose Local First effects behind the Core rebuild state machine."""

        from okto_pulse.community.adapters.rebuild_effects import (
            CommunityRebuildEffects,
        )
        from okto_pulse.core.application.rebuild_processor import (
            RebuildCommand,
            RebuildOutcomeCode,
            RebuildPlan,
            RebuildProcessor,
        )
        from okto_pulse.core.kg.rebuild_deterministic import (
            DeterministicStructuralRebuilder,
        )
        from okto_pulse.core.kg.rebuild_service import RebuildStepResult

        deterministic = DeterministicStructuralRebuilder().as_rebuild_step_adapter(
            source_resolver=source_resolver,
        )

        def _adapter(req):
            base_result = deterministic(req)
            if not base_result.ok:
                return base_result

            sources = tuple(dict(row) for row in source_resolver(req))
            run_id = f"f06:{req.manifest_ref or req.candidate_kg_generation_id or req.board_id}"
            self._rebuild_run_boards[run_id] = req.board_id
            command = RebuildCommand(
                run_id=run_id,
                board_id=req.board_id,
                manifest_ref=req.manifest_ref,
                operation=req.operation,
                actor_id=req.actor_id,
                reason=f"explicit_rebuild:{req.manifest_ref or req.operation}",
                source_rows=sources,
                previous_generation_id=req.previous_kg_generation_id,
                candidate_generation_id=req.candidate_kg_generation_id,
                salvage_pending=(
                    bool(self.salvage_pending_provider(req.board_id))
                    if self.salvage_pending_provider is not None
                    else False
                ),
            )
            effects = CommunityRebuildEffects(self, artifact_store=self.artifact_store)
            outcome = RebuildProcessor(
                effects,
                plan=RebuildPlan(
                    stall_timeout_seconds=self.drain_timeout_seconds,
                    hard_timeout_seconds=self.drain_hard_timeout_seconds,
                    observation_wait_seconds=self.drain_poll_interval_seconds,
                    final_grace_seconds=self.drain_final_grace_seconds,
                    low_depth_threshold=self.drain_low_depth_threshold,
                ),
            ).execute(command)

            by_effect = {receipt.effect: receipt for receipt in outcome.receipts}
            quarantine = by_effect.get("quarantine")
            enqueue = by_effect.get("enqueue")
            restore = by_effect.get("restore")
            affected_files = tuple(
                dict(quarantine.details).get("affected_files", ())
                if quarantine is not None
                else ()
            )
            enqueue_counts = dict(enqueue.details) if enqueue is not None else {}
            merged_counts = {
                **base_result.counts,
                "enqueue_inserted": int(enqueue_counts.get("inserted", 0)),
                "enqueue_reset_to_pending": int(
                    enqueue_counts.get("reset_to_pending", 0)
                ),
                "enqueue_left_alone": int(enqueue_counts.get("left_alone", 0)),
                "expected_by_layer": expected_layers_from_sources(sources),
            }
            checkpoint = self._rebuild_checkpoint_cache.get(run_id)
            queue_drain = {
                "idle": outcome.code is RebuildOutcomeCode.COMPLETED,
                "final_depth": (
                    checkpoint.best_queue_depth if checkpoint is not None else None
                ),
                "progress_events": (
                    checkpoint.queue_progress_events if checkpoint is not None else 0
                ),
                "stall_window_seconds": self.drain_timeout_seconds,
                "hard_timeout_seconds": self.drain_hard_timeout_seconds,
                "grace_applied": (
                    checkpoint.queue_grace_applied if checkpoint is not None else False
                ),
                "grace_reason": (
                    checkpoint.queue_grace_reason if checkpoint is not None else None
                ),
                "final_grace_seconds": self.drain_final_grace_seconds,
                "low_depth_threshold": self.drain_low_depth_threshold,
            }
            drilldown = {
                **base_result.drilldown,
                "graph_prepare": {"quarantined_files": len(affected_files)},
                "enqueue": enqueue_counts,
                "queue_drain": queue_drain,
                "ingestion_mode": "community_rebuild_effects",
                "cognitive_preservation": (
                    dict(restore.details) if restore is not None else {}
                ),
                "layer_materialization": {
                    "expected_by_layer": merged_counts["expected_by_layer"]
                },
                "rebuild_processor": {
                    "state": outcome.state.value,
                    "code": outcome.code.value,
                    "promotion_allowed": outcome.promotion_allowed,
                    "compensation_actions": [
                        action.value for action in outcome.compensation_actions
                    ],
                },
            }
            if outcome.code is not RebuildOutcomeCode.COMPLETED:
                if outcome.code in {
                    RebuildOutcomeCode.DRAIN_STALLED,
                    RebuildOutcomeCode.HARD_TIMEOUT,
                }:
                    detail = (
                        "queue_drain_timeout:"
                        f"final_depth={queue_drain['final_depth']} "
                        f"cause={outcome.code.value}"
                    )
                elif (
                    outcome.code is RebuildOutcomeCode.RESTORE_FAILED
                    and outcome.detail == "integrity_error"
                ):
                    detail = "cognitive_preservation_integrity_error"
                else:
                    detail = f"{outcome.code.value}:{outcome.detail or ''}".rstrip(
                        ":"
                    )
                return RebuildStepResult(
                    ok=False,
                    detail=detail,
                    current_kg_generation_id=base_result.current_kg_generation_id,
                    previous_kg_generation_id=base_result.previous_kg_generation_id,
                    affected_files=tuple(base_result.affected_files) + affected_files,
                    structural_hash=base_result.structural_hash,
                    source_hash=base_result.source_hash,
                    counts=merged_counts,
                    reconciliation_decisions=base_result.reconciliation_decisions,
                    drilldown=drilldown,
                )

            return RebuildStepResult(
                ok=True,
                current_kg_generation_id=base_result.current_kg_generation_id,
                previous_kg_generation_id=base_result.previous_kg_generation_id,
                affected_files=tuple(base_result.affected_files) + affected_files,
                structural_hash=base_result.structural_hash,
                source_hash=base_result.source_hash,
                counts=merged_counts,
                reconciliation_decisions=base_result.reconciliation_decisions,
                drilldown=drilldown,
            )

        return _adapter

    def drain_until_idle(
        self,
        *,
        board_id: str,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.5,
        final_grace_seconds: float | None = None,
        low_depth_threshold: int | None = None,
        hard_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Compatibility runner; all timeout decisions come from Core policy."""

        from datetime import datetime, timedelta, timezone

        from okto_pulse.core.application.rebuild_processor import (
            QueueDrainDecision,
            QueueDrainPolicy,
            evaluate_queue_depth,
            start_queue_drain,
        )

        stall_window = max(0.5, float(timeout_seconds))
        hard_ceiling = max(
            stall_window,
            float(
                self.drain_hard_timeout_seconds
                if hard_timeout_seconds is None
                else hard_timeout_seconds
            ),
        )
        grace_seconds = max(
            0.0,
            float(
                self.drain_final_grace_seconds
                if final_grace_seconds is None
                else final_grace_seconds
            ),
        )
        low_depth = max(
            0,
            int(
                self.drain_low_depth_threshold
                if low_depth_threshold is None
                else low_depth_threshold
            ),
        )
        policy = QueueDrainPolicy(
            stall_timeout_seconds=stall_window,
            hard_timeout_seconds=hard_ceiling,
            final_grace_seconds=grace_seconds,
            low_depth_threshold=low_depth,
        )
        monotonic_start = time.monotonic()
        wall_start = datetime.now(timezone.utc)
        tracker = start_queue_drain(policy, now=wall_start)
        final_depth = -1
        decision = QueueDrainDecision.CONTINUE

        while decision is QueueDrainDecision.CONTINUE:
            final_depth = self.queue_depth(board_id)
            monotonic_now = time.monotonic()
            wall_now = wall_start + timedelta(seconds=monotonic_now - monotonic_start)
            evaluation = evaluate_queue_depth(
                policy,
                tracker,
                depth=final_depth,
                now=wall_now,
            )
            tracker = evaluation.tracker
            decision = evaluation.decision
            if decision is QueueDrainDecision.CONTINUE:
                remaining = min(
                    max(0.0, (tracker.stall_deadline - wall_now).total_seconds()),
                    max(0.0, (tracker.hard_deadline - wall_now).total_seconds()),
                )
                time.sleep(min(float(poll_interval_seconds), remaining))

        waited = round(time.monotonic() - monotonic_start, 2)
        return {
            "final_depth": final_depth,
            "waited_seconds": waited,
            "idle": decision is QueueDrainDecision.IDLE,
            "base_timeout_seconds": stall_window,
            "stall_window_seconds": stall_window,
            "hard_timeout_seconds": hard_ceiling,
            "hard_timed_out": decision is QueueDrainDecision.HARD_TIMEOUT,
            "progress_events": tracker.progress_events,
            "best_depth": tracker.best_depth,
            "final_grace_seconds": grace_seconds,
            "low_depth_threshold": low_depth,
            "grace_applied": tracker.grace_applied,
            "grace_reason": tracker.grace_reason,
        }


BoardRebuildIngestionAdapter = CommunityBoardRebuildIngestionAdapter


__all__ = [
    "BoardRebuildIngestionAdapter",
    "CommunityBoardRebuildIngestionAdapter",
]
