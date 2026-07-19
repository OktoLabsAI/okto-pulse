from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityRecoveryWorker,
    RecoveryDispatchStage,
    RecoveryWorkerFenceError,
)
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryCheckpoint,
    RecoveryControlPlane,
    RecoveryProgressCounts,
    RecoveryResumeRejected,
    RecoveryRunBinding,
    RecoveryRunState,
    RecoveryStartCommand,
    RecoveryTerminalOutcome,
    RecoveryWorkerResult,
)


NOW = datetime(2026, 7, 16, 15, 30, tzinfo=timezone.utc)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[tuple[str, int]] = []

    def dispatch(self, *, run_id: str, epoch: int, **_kwargs) -> None:
        self.dispatched.append((run_id, epoch))


def command(run_id: str, *, started_at: datetime, attempt_budget_ms: int):
    return RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id=run_id,
            actor_id="agent-test",
            confirmation_fingerprint=f"sha256:{run_id}",
            manifest_ref=f"manifest://{run_id}",
            preflight_hash=f"preflight-{run_id}",
            reason="controlled timeout integration",
        ),
        started_at=started_at,
        counts=RecoveryProgressCounts(sources_total=1),
        attempt_budget_ms=attempt_budget_ms,
    )


def wait_until_terminal(
    control: RecoveryControlPlane,
    *,
    run_id: str,
    timeout_seconds: float = 2.0,
):
    deadline = time.monotonic() + timeout_seconds
    status = control.status(run_id)
    while not status.state.is_terminal:
        if time.monotonic() >= deadline:
            raise AssertionError(f"terminal status timed out; last={status!r}")
        time.sleep(0.01)
        status = control.status(run_id)
    return status


def test_expired_physical_attempt_requires_same_epoch_journal_reconciliation(
    tmp_path: Path,
    recovery_store_factory,
    prepared_recovery_admitter,
) -> None:
    """W4/TR11: journal reconciliation precedes a durable late timeout."""

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / 'stale-timeout.sqlite3').as_posix()}"
    )
    dispatcher = RecordingDispatcher()
    control = RecoveryControlPlane(store=store, dispatcher=dispatcher)
    start_command = command(
        "run-stale-timeout",
        started_at=NOW,
        attempt_budget_ms=10_000,
    )
    prepared_recovery_admitter(store, start_command)
    control.start(start_command)
    running = store.mark_running(
        run_id="run-stale-timeout",
        epoch=1,
        at=NOW,
    )
    heartbeat = store.compare_and_set_checkpoint(
        RecoveryCheckpoint(
            run_id="run-stale-timeout",
            epoch=1,
            expected_progress_seq=running.progress_seq,
            phase=running.phase,
            heartbeat_at=NOW + timedelta(seconds=9),
            active_elapsed_ms=9_000,
            counts=running.counts,
        )
    )

    with pytest.raises(RecoveryResumeRejected) as pending_reconciliation:
        store.admit_explicit_resume(
            run_id=heartbeat.run_id,
            expected_epoch=1,
            requested_at=heartbeat.heartbeat_at + timedelta(seconds=15),
            requested_by_actor_id="operator-timeout-test",
            reason="request must not skip physical journal reconciliation",
        )

    assert pending_reconciliation.value.code == (
        "recovery_physical_reconciliation_pending"
    )
    assert dispatcher.dispatched == [("run-stale-timeout", 1)]
    still_running = store.get_status(run_id=heartbeat.run_id)
    assert still_running is not None
    assert still_running.state is RecoveryRunState.RUNNING

    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.RECOVERY,
        worker_id="deadline-journal-reconciler",
        claimed_at=heartbeat.heartbeat_at + timedelta(seconds=15),
        claim_expires_at=heartbeat.heartbeat_at + timedelta(seconds=30),
    )
    assert claim is not None
    assert claim.epoch == 1
    assert claim.reconciliation_only is True
    current = store.get_status(run_id=heartbeat.run_id)
    assert current is not None
    store.complete_recovery(
        dispatch_id=claim.dispatch_id,
        claim_token=claim.claim_token,
        expected_progress_seq=current.progress_seq,
        completed_at=heartbeat.heartbeat_at + timedelta(seconds=16),
        active_elapsed_ms=current.attempt_budget_ms,
        result=RecoveryWorkerResult(
            outcome=RecoveryTerminalOutcome.TIMEOUT,
            reason_code="recovery_attempt_budget_exhausted",
            retryable=False,
            counts=current.counts,
        ),
    )

    terminal = store.get_status(run_id=heartbeat.run_id)
    assert terminal is not None
    assert terminal.state is RecoveryRunState.TIMEOUT
    assert terminal.terminal_outcome is not None
    assert terminal.reason_code == "recovery_attempt_budget_exhausted"
    assert terminal.active_elapsed_ms == 10_000
    assert terminal.cumulative_active_ms == 10_000
    assert terminal.updated_at == NOW + timedelta(seconds=25)
    assert terminal.superseded_by_epoch is None
    assert len(store.list_attempts(run_id=heartbeat.run_id)) == 1

    frozen = store.list_attempts(run_id=heartbeat.run_id)
    with pytest.raises(RecoveryResumeRejected) as never_resumable:
        store.admit_explicit_resume(
            run_id=heartbeat.run_id,
            expected_epoch=1,
            requested_at=heartbeat.heartbeat_at + timedelta(seconds=16),
            requested_by_actor_id="operator-timeout-test",
            reason="terminal timeout remains non-resumable",
        )
    assert never_resumable.value.code == "recovery_timeout_not_resumable"
    assert store.list_attempts(run_id=heartbeat.run_id) == frozen
    assert dispatcher.dispatched == [("run-stale-timeout", 1)]


def test_owned_worker_terminalizes_deadline_and_fences_draining_native_work(
    tmp_path: Path,
    recovery_store_factory,
    prepared_recovery_admitter,
) -> None:
    """FR5: deadline closes status while uninterruptible native work drains."""

    native_entered = Event()
    release_native = Event()
    fence_errors: list[str] = []

    def blocked_native(*, run_id: str, epoch: int, attempt_id: str, fence_check):
        del run_id, epoch, attempt_id
        native_entered.set()
        if not release_native.wait(timeout=10):
            raise AssertionError("test did not release the native operation")
        try:
            fence_check()
        except RecoveryWorkerFenceError as exc:
            fence_errors.append(exc.code)
            raise
        return RecoveryWorkerResult(
            outcome="success",
            reason_code="native_completed",
            retryable=False,
            counts=RecoveryProgressCounts(
                sources_total=1,
                sources_processed=1,
            ),
        )

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / 'worker-timeout.sqlite3').as_posix()}"
    )
    worker = CommunityRecoveryWorker(
        store=store,
        native_operation=blocked_native,
        heartbeat_interval_ms=20,
    )
    control = RecoveryControlPlane(store=store, dispatcher=worker)

    try:
        # The deadline must expire while the native call drains, never before
        # the worker claims: a 120ms budget raced the claim under load, the
        # expired attempt was claimed reconcile_only and the native fake was
        # bypassed.  A 2s budget keeps claim+entry far inside the budget
        # (worker polls every 20ms) while the blocked native still drains
        # long past the deadline, so the proof itself is unchanged.
        started = time.monotonic()
        start_command = command(
            "run-worker-timeout",
            started_at=datetime.now(timezone.utc),
            attempt_budget_ms=2_000,
        )
        prepared_recovery_admitter(store, start_command)
        control.start(start_command)
        assert native_entered.wait(timeout=1)
        entry_elapsed_ms = (time.monotonic() - started) * 1_000
        # Precondition the proof relies on: native entry happened well inside
        # the budget, so the coming TIMEOUT is a deadline AFTER native entry.
        assert entry_elapsed_ms < 1_000, entry_elapsed_ms
        assert control.status("run-worker-timeout").state.is_terminal is False
        terminal = wait_until_terminal(
            control,
            run_id="run-worker-timeout",
            timeout_seconds=8.0,
        )
    finally:
        release_native.set()
        worker.close(timeout_seconds=2)

    assert terminal.state is RecoveryRunState.TIMEOUT
    assert terminal.reason_code == "recovery_attempt_budget_exhausted"
    assert terminal.active_elapsed_ms == 2_000
    assert terminal.cumulative_active_ms == 2_000
    assert fence_errors == ["attempt_not_running"]
    assert control.status("run-worker-timeout") == terminal
