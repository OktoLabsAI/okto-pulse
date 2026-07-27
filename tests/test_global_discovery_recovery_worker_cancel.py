from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityRecoveryWorker,
)
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryControlPlane,
    RecoveryProgressCounts,
    RecoveryRunBinding,
    RecoveryRunState,
    RecoveryStartCommand,
    RecoveryWorkerResult,
)


def wait_until_status(
    control: RecoveryControlPlane,
    *,
    run_id: str,
    predicate,
    timeout_seconds: float = 2.0,
):
    deadline = time.monotonic() + timeout_seconds
    last = control.status(run_id)
    while not predicate(last):
        if time.monotonic() >= deadline:
            raise AssertionError(f"status predicate timed out; last={last!r}")
        time.sleep(0.01)
        last = control.status(run_id)
    return last


def test_cancel_is_durable_without_claiming_the_native_thread_was_killed(
    tmp_path: Path,
    recovery_store_factory,
    prepared_recovery_admitter,
) -> None:
    """ts_fa3ba8ce: cancel fences later commit but still drains native work."""

    native_entered = Event()
    release_native = Event()
    unsafe_commit = Event()

    def blocked_native(*, run_id: str, epoch: int, attempt_id: str, fence_check):
        del run_id, epoch, attempt_id
        native_entered.set()
        if not release_native.wait(timeout=3):
            raise AssertionError("test did not release the native operation")
        fence_check()
        unsafe_commit.set()
        return RecoveryWorkerResult(
            outcome="success",
            reason_code="should_not_commit_after_cancel",
            retryable=False,
            counts=RecoveryProgressCounts(sources_total=1, sources_processed=1),
        )

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / 'cancel.sqlite3').as_posix()}"
    )
    worker = CommunityRecoveryWorker(
        store=store,
        native_operation=blocked_native,
        heartbeat_interval_ms=50,
    )
    control = RecoveryControlPlane(store=store, dispatcher=worker)
    command = RecoveryStartCommand(
            binding=RecoveryRunBinding(
                run_id="run-cancel",
                actor_id="agent-test",
                confirmation_fingerprint="sha256:cancel-fixture",
                manifest_ref="manifest://cancel",
                preflight_hash="cancel-preflight",
                reason="controlled cancellation integration",
            ),
            started_at=datetime.now(timezone.utc),
            counts=RecoveryProgressCounts(sources_total=1),
    )
    prepared_recovery_admitter(store, command)
    control.start(command)

    assert native_entered.wait(timeout=1.0)
    running = wait_until_status(
        control,
        run_id="run-cancel",
        predicate=lambda status: status.progress_seq >= 2,
    )

    before = time.monotonic()
    acknowledged = control.cancel(
        run_id="run-cancel",
        expected_epoch=running.epoch,
        requested_at=datetime.now(timezone.utc),
        requested_by_actor_id="agent-test",
        reason="controlled cancellation integration",
    )
    cancel_elapsed = time.monotonic() - before

    assert cancel_elapsed < 5.0
    assert acknowledged.state is RecoveryRunState.RUNNING
    assert acknowledged.cancel_requested_at is not None
    assert acknowledged.reason_code == "recovery_cancel_requested"
    assert acknowledged.progress_seq == running.progress_seq + 1

    time.sleep(0.15)
    while_native_is_blocked = control.status("run-cancel")
    assert while_native_is_blocked.state is RecoveryRunState.RUNNING
    assert while_native_is_blocked.cancel_requested_at == (
        acknowledged.cancel_requested_at
    )
    assert while_native_is_blocked.progress_seq == acknowledged.progress_seq
    assert while_native_is_blocked.heartbeat_at == acknowledged.heartbeat_at
    assert release_native.is_set() is False
    assert unsafe_commit.is_set() is False

    with pytest.raises(TimeoutError, match="did not drain"):
        worker.close(timeout_seconds=0.05)

    release_native.set()
    terminal = wait_until_status(
        control,
        run_id="run-cancel",
        predicate=lambda status: status.state is RecoveryRunState.CANCELLED,
    )
    worker.close(timeout_seconds=2.0)

    assert terminal.terminal_outcome.value == "cancelled"
    assert terminal.reason_code == "recovery_cancelled"
    assert terminal.cancel_requested_at == acknowledged.cancel_requested_at
    assert unsafe_commit.is_set() is False
