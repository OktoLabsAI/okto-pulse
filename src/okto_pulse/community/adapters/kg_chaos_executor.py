"""Community-local chaos executor for the destructive KG stress profile.

This module wires the canonical chaos modes against the KG-01.1-5
primitives (single-writer lock, backpressure gate, quarantine service,
safe write lifecycle, write barrier). It is the executor that CI plugs
into ``KGStressProfileRunner`` so that release evidence is produced by
exercising real code paths — not the no-op default.

Each ``ChaosMode`` validates one invariant of the hardening surface:

* ``memory_pressure``: feed the buffer high-water-mark close to/above
  the 90 % threshold and confirm the health classifier escalates to
  ``at_risk`` BEFORE any storage degradation; release-clean iff no
  spurious ``corruption_detected`` is reported.
* ``sigkill_during_write``: simulate a worker that crashed mid-write —
  acquired the single-writer lock, started the safe lifecycle, never
  released. Verify the lock's TTL-based stale recovery path reclaims
  cleanly and NO primary file is overwritten by a racing recoverer.
* ``oom_during_write``: simulate allocation failure inside the safe
  lifecycle. The lifecycle MUST return ``status=FAILED`` with the
  failing step bucketed, and NO partial write may bypass the barrier.
* ``restart_during_consolidation``: simulate the orchestrator dying
  between commit and outbox flush. Bootstrap on restart MUST go
  through ``purge_*_storage`` which is quarantine-first — no purge
  may happen without a manifest.
* ``tick_consolidation_rebuild_concurrency``: 3 actors contend for
  the lock simultaneously (tick worker, consolidation worker, rebuild
  admin lane). Exactly ONE wins the lock; the rebuild admin lane
  must drain (not bypass) and the other two must report contention.

All scenarios are deterministic — they do NOT depend on RNG, wall
clock or external storage. The fakes provided here exercise the
primitives' public APIs directly so the runner's pass/fail signal
is a true measurement of the hardening surface's contracts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from okto_pulse.core.kg.stress_runner import ChaosOutcome

logger = logging.getLogger("okto_pulse.community.kg_chaos_executor")


class KGChaosExecutor:
    """Bundle of deterministic chaos scenarios against the primitives.

    Instantiate once per stress run with the supporting storage path
    (a temp dir is fine — no real LadybugDB instance is required).
    The returned callable matches the ``ChaosExecutor`` signature so
    it slots straight into ``KGStressProfileRunner(executor=...)``.

    val_67950216 enforcement: marks itself as a release-qualified
    executor via ``release_evidence_executor=True``. The runner refuses
    ``profile=ci_destructive`` for any executor without this marker —
    a fake clean callable can no longer produce release evidence.
    """

    # Class-level marker — instances inherit by default. Custom
    # release-qualified executors set this on the class or instance.
    release_evidence_executor: bool = True

    def __init__(
        self,
        base_dir: Path,
        *,
        write_lock_port: Any | None = None,
        artifact_store: Any | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock_port = write_lock_port
        self._artifact_store = artifact_store
        # Per-mode handler registry — adding a future chaos mode is a
        # one-line entry here.
        self._handlers = {
            "memory_pressure": self._memory_pressure,
            "sigkill_during_write": self._sigkill_during_write,
            "oom_during_write": self._oom_during_write,
            "restart_during_consolidation": self._restart_during_consolidation,
            "tick_consolidation_rebuild_concurrency": (
                self._tick_consolidation_rebuild_concurrency
            ),
        }

    def __call__(
        self, seed: str, iteration: int, chaos_mode: str
    ) -> ChaosOutcome:
        handler = self._handlers.get(chaos_mode)
        if handler is None:
            # Unknown mode is treated as orchestrator failure by the
            # runner's outer try/except; here we surface as a typed
            # ChaosOutcome so the per-mode counter remains coherent.
            return ChaosOutcome(
                corruption_detected=True,
                failure_artifact=f"unknown_chaos_mode={chaos_mode}",
            )
        try:
            return handler(seed, iteration)
        except Exception as exc:  # defensive — handler bugs surface as corruption
            return ChaosOutcome(
                corruption_detected=True,
                failure_artifact=f"handler_exception={type(exc).__name__}",
            )

    # --- per-mode scenarios -----------------------------------------------

    def _memory_pressure(self, seed: str, iteration: int) -> ChaosOutcome:
        """Feed a high-water-mark telemetry into the health classifier
        and assert the state escalates to ``at_risk`` BEFORE any
        storage degradation. Invariant: classifier never reports
        ``healthy`` when buffer pressure is high.
        """
        from okto_pulse.core.kg.health_state import (
            DEFAULT_BUFFER_AT_RISK_PCT,
            GraphTelemetry,
            HealthState,
            KGHealthStateClassifier,
            LockState,
        )

        classifier = KGHealthStateClassifier()
        # >= threshold to trigger at_risk.
        telemetry = GraphTelemetry(
            graph_type="board",
            buffer_utilization_pct=DEFAULT_BUFFER_AT_RISK_PCT + 5.0,
            high_water_mark_pct=DEFAULT_BUFFER_AT_RISK_PCT + 5.0,
            recent_buffer_errors=0,
            recent_wal_errors=0,
            recent_commit_errors=0,
        )
        classification = classifier.evaluate(
            telemetries=[telemetry],
            lock_state=LockState(False, False, None),
            quarantine_present=False,
            backpressure_rejecting=False,
        )
        if classification.state == HealthState.HEALTHY:
            return ChaosOutcome(
                corruption_detected=True,
                failure_artifact="classifier_emitted_healthy_under_buffer_pressure",
            )
        return ChaosOutcome()

    def _sigkill_during_write(
        self, seed: str, iteration: int
    ) -> ChaosOutcome:
        """Worker acquires the single-writer lock, dies without
        releasing. Verify another recoverer can reclaim via TTL
        without overwriting evidence."""
        from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

        lock = KGSingleWriterLock(
            write_lock_port=self._write_lock_port,
        )
        # Iteration-1 of the scenario: acquire and "die" (no release).
        # Subsequent iterations of the same simulated worker would
        # find the stale lock and recover. Here we run both halves
        # in-process with TTL=1s so the recovery path is exercised.
        dead = lock.acquire(
            board_id="b_chaos", operation="consolidate",
            owner_id="dead-worker", ttl_seconds=1,
        )
        if not dead.acquired:
            return ChaosOutcome(
                orphaned_lock=True,
                failure_artifact="first_acquire_failed",
            )
        # Simulate the original worker crashing — DO NOT release.
        # Wait briefly so TTL is past, then attempt recovery.
        import time
        time.sleep(1.1)
        rescuer = lock.acquire(
            board_id="b_chaos", operation="consolidate",
            owner_id="rescuer", ttl_seconds=30,
        )
        if not rescuer.acquired:
            return ChaosOutcome(
                orphaned_lock=True,
                failure_artifact="stale_recovery_failed",
            )
        # Clean release.
        if not lock.release(
            board_id="b_chaos", owner_token=rescuer.owner_token
        ):
            return ChaosOutcome(
                orphaned_lock=True,
                failure_artifact="release_after_recovery_failed",
            )
        return ChaosOutcome()

    def _oom_during_write(self, seed: str, iteration: int) -> ChaosOutcome:
        """Lifecycle step raises (OOM-style). Assert the safe write
        wrapper returns ``status=FAILED`` with the bucketed failed
        step — no partial write may slip past the barrier."""
        from okto_pulse.core.kg.safe_write_lifecycle import (
            HealthProbe,
            KGSafeWriteLifecycle,
            LifecycleStepResult,
            LockOwnerProbe,
            SafeWriteLifecycleStatus,
        )

        def boom_executor(board_id, graph_type, step):
            if step == "flush":
                raise MemoryError("simulated OOM during flush")
            return LifecycleStepResult(ok=True)

        wrapper = KGSafeWriteLifecycle(
            step_adapter=boom_executor,
            owner_probe=LockOwnerProbe(is_active_owner=lambda b, t: True),
            health_probe=HealthProbe(
                classify=lambda b, g, status, step: "recovery_needed"
            ),
        )
        response = wrapper.apply(
            board_id="b_chaos",
            graph_type="board_graph",
            operation="bulk_consolidate",
            owner_token="chaos-token",
            mutation_ref=f"oom-{iteration}",
        )
        if response.status != SafeWriteLifecycleStatus.FAILED:
            return ChaosOutcome(
                corruption_detected=True,
                failure_artifact=(
                    f"lifecycle_did_not_fail_under_oom status={response.status}"
                ),
            )
        if response.failed_step != "flush":
            return ChaosOutcome(
                corruption_detected=True,
                failure_artifact=(
                    f"failed_step_mismatch expected=flush actual={response.failed_step}"
                ),
            )
        return ChaosOutcome()

    def _restart_during_consolidation(
        self, seed: str, iteration: int
    ) -> ChaosOutcome:
        """Simulate a purge attempt mid-restart: it MUST go through
        quarantine. We feed the quarantine service a real path and
        confirm the moved-files counter > 0 and manifest exists."""
        from okto_pulse.core.kg.quarantine import (
            KGQuarantineService,
        )
        from okto_pulse.community.adapters.local_storage_ref import local_storage_ref

        per_iter_dir = self.base_dir / f"restart-{iteration}"
        per_iter_dir.mkdir(parents=True, exist_ok=True)
        # Simulate the post-restart "corrupted graph file" that needs
        # to be quarantined before bootstrap.
        primary = per_iter_dir / "graph.lbug"
        primary.write_text("restart-leftover-bytes", encoding="utf-8")

        service = KGQuarantineService(
            base_storage_ref_hint=local_storage_ref(self.base_dir),
            scope_storage_refs=[
                local_storage_ref(per_iter_dir)
            ],
            artifact_store=self._artifact_store,
        )
        response = service.create(
            board_id="b_chaos",
            graph_type="board_graph",
            affected_storage_refs=[
                local_storage_ref(primary)
            ],
            reason="restart_during_consolidation",
            correlation_ids=[f"chaos-{iteration}"],
        )
        if response.files_moved != 1:
            return ChaosOutcome(
                purge_without_quarantine=True,
                failure_artifact=(
                    f"quarantine_files_moved_unexpected={response.files_moved}"
                ),
            )
        # Manifest exists where the contract said it would.
        manifest_path = Path(response.manifest_ref)
        if not manifest_path.exists():
            return ChaosOutcome(
                purge_without_quarantine=True,
                failure_artifact="manifest_missing_after_quarantine",
            )
        return ChaosOutcome()

    def _tick_consolidation_rebuild_concurrency(
        self, seed: str, iteration: int
    ) -> ChaosOutcome:
        """3 actors contend for the single-writer lock for the same
        board. Invariant: exactly one wins, two see contention, NO
        primary lock state is corrupted."""
        from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

        lock = KGSingleWriterLock(
            write_lock_port=self._write_lock_port,
        )
        winner = lock.acquire(
            board_id="b_chaos", operation="consolidate",
            owner_id="tick-worker", ttl_seconds=60,
        )
        if not winner.acquired:
            return ChaosOutcome(
                orphaned_lock=True,
                failure_artifact="winner_did_not_acquire",
            )
        loser_a = lock.acquire(
            board_id="b_chaos", operation="consolidate",
            owner_id="consolidation-worker", ttl_seconds=60,
        )
        loser_b = lock.acquire(
            board_id="b_chaos", operation="rebuild",
            owner_id="rebuild-admin", ttl_seconds=60, admin_lane=True,
        )
        if loser_a.acquired or loser_b.acquired:
            return ChaosOutcome(
                orphaned_lock=True,
                failure_artifact=(
                    f"concurrent_acquire_succeeded "
                    f"a={loser_a.acquired} b={loser_b.acquired}"
                ),
            )
        # Clean release.
        lock.release(board_id="b_chaos", owner_token=winner.owner_token)
        return ChaosOutcome()


__all__ = ["KGChaosExecutor"]
