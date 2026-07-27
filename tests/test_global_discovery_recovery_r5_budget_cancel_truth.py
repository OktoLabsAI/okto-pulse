from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import okto_pulse.core.ports.global_discovery_recovery_control as recovery_contract
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityRecoveryWorker,
)


NOW = datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc)


class ManualRecoveryClock:
    def __init__(self, *, wall: datetime = NOW, monotonic: float = 100.0) -> None:
        self._wall = wall
        self._monotonic = float(monotonic)

    def wall_now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def advance_ms(self, milliseconds: int) -> None:
        delta = timedelta(milliseconds=milliseconds)
        self._wall += delta
        self._monotonic += milliseconds / 1_000

    def advance_monotonic_ms(self, milliseconds: int) -> None:
        self._monotonic += milliseconds / 1_000

    def jump_wall(self, delta: timedelta) -> None:
        self._wall += delta


def _required(owner: object, name: str):
    value = getattr(owner, name, None)
    assert value is not None, f"R5 contract is missing {name}"
    return value


def _running_status(*, active_elapsed_ms: int = 2_000, budget_ms: int = 10_000):
    binding_type = _required(recovery_contract, "RecoveryRunBinding")
    counts_type = _required(recovery_contract, "RecoveryProgressCounts")
    status_type = _required(recovery_contract, "RecoveryRunStatus")
    states = _required(recovery_contract, "RecoveryRunState")
    phases = _required(recovery_contract, "RecoveryRunPhase")
    confirmations = _required(recovery_contract, "RecoveryConfirmationState")
    prepared_at = NOW - timedelta(seconds=1)
    return status_type(
        binding=binding_type(
            run_id="gdr_r5_monotonic",
            actor_id="agent-budget",
            confirmation_fingerprint="sha256:consumed",
            manifest_ref="manifest://gdr_r5_monotonic/attempt-1",
            preflight_hash="preflight-gdr-r5-monotonic",
            reason="confirmed budget test",
        ),
        epoch=1,
        state=states.RUNNING,
        progress_seq=4,
        phase=phases.CUTOVER,
        counts=counts_type(
            boards_total=1,
            boards_scanned=1,
            sources_total=1,
            sources_processed=1,
        ),
        heartbeat_at=NOW,
        started_at=NOW,
        updated_at=NOW,
        active_elapsed_ms=active_elapsed_ms,
        active_deadline_at=NOW + timedelta(milliseconds=budget_ms),
        cumulative_active_ms=active_elapsed_ms,
        confirmation_state=confirmations.CONSUMED,
        prepared_at=prepared_at,
        expires_at=prepared_at + timedelta(seconds=300),
        snapshot_fingerprint="sha256:snapshot",
        confirmed_by_actor_id="agent-confirmer",
        confirmation_consumed_at=NOW,
        audit_reason="confirmed budget test",
        attempt_budget_ms=budget_ms,
    )


class _RecordingStore:
    def __init__(self, status) -> None:
        self.status = status
        self.checkpoints: list[object] = []
        self.completions: list[dict[str, object]] = []

    def get_status(self, *, run_id: str):
        assert run_id == self.status.run_id
        return self.status

    def compare_and_set_checkpoint(self, checkpoint):
        self.checkpoints.append(checkpoint)
        self.status = self.status.advance_checkpoint(checkpoint)
        return self.status

    def complete(self, **values):
        self.completions.append(dict(values))
        self.status = self.status.complete(
            result=values["result"],
            completed_at=max(values["completed_at"], self.status.updated_at),
            active_elapsed_ms=values["active_elapsed_ms"],
        )
        return self.status


def _worker(store: _RecordingStore, clock: ManualRecoveryClock):
    worker = object.__new__(CommunityRecoveryWorker)
    worker._store = store  # noqa: SLF001
    worker._wall_clock = clock.wall_now  # noqa: SLF001
    worker._monotonic_clock = clock.monotonic  # noqa: SLF001
    return worker


def _heartbeat(worker, status, *, started_monotonic: float):
    parameters = inspect.signature(worker._heartbeat).parameters  # noqa: SLF001
    assert "attempt_id" in parameters
    return worker._heartbeat(  # noqa: SLF001
        run_id=status.run_id,
        attempt_id=status.attempt_id,
        epoch=status.epoch,
        started_monotonic=started_monotonic,
        baseline_elapsed_ms=status.active_elapsed_ms,
    )


def _complete(worker, status, *, started_monotonic: float, baseline: int, result):
    parameters = inspect.signature(worker._complete).parameters  # noqa: SLF001
    assert "attempt_id" in parameters
    return worker._complete(  # noqa: SLF001
        run_id=status.run_id,
        attempt_id=status.attempt_id,
        epoch=status.epoch,
        started_monotonic=started_monotonic,
        baseline_elapsed_ms=baseline,
        result=result,
    )


def test_forward_wall_jump_does_not_consume_monotonic_attempt_budget() -> None:
    status = _running_status()
    store = _RecordingStore(status)
    clock = ManualRecoveryClock()
    worker = _worker(store, clock)
    adopted = clock.monotonic()

    clock.jump_wall(timedelta(days=1))
    clock.advance_monotonic_ms(1_000)
    assert _heartbeat(worker, status, started_monotonic=adopted) is True

    assert store.completions == []
    assert len(store.checkpoints) == 1
    checkpoint = store.checkpoints[0]
    assert checkpoint.active_elapsed_ms == 3_000


def test_backward_wall_jump_does_not_extend_attempt_budget_and_cap_is_exact() -> None:
    status = _running_status()
    store = _RecordingStore(status)
    clock = ManualRecoveryClock()
    worker = _worker(store, clock)
    adopted = clock.monotonic()

    clock.jump_wall(-timedelta(days=1))
    clock.advance_monotonic_ms(8_000)
    assert _heartbeat(worker, status, started_monotonic=adopted) is False

    assert store.checkpoints == []
    assert len(store.completions) == 1
    completion = store.completions[0]
    assert completion["active_elapsed_ms"] == 10_000
    assert completion["result"].outcome.value == "timeout"
    assert completion["result"].reason_code == "recovery_attempt_budget_exhausted"


@pytest.mark.parametrize(
    ("outcome", "journal_phase", "pointer_replaced", "rollback_performed"),
    [
        ("success", "completed", True, False),
        ("partial", "rolled_back", False, True),
    ],
)
def test_late_cancel_cannot_rewrite_completed_or_rolled_back_physical_truth(
    outcome: str,
    journal_phase: str,
    pointer_replaced: bool,
    rollback_performed: bool,
) -> None:
    status = replace(
        _running_status(),
        cancel_requested_at=NOW + timedelta(milliseconds=100),
        cancel_requested_by_actor_id="agent-canceller",
        audit_reason="operator requested cancellation after physical terminal",
    )
    store = _RecordingStore(status)
    clock = ManualRecoveryClock()
    worker = _worker(store, clock)
    truth_type = _required(recovery_contract, "RecoveryPhysicalTruth")
    result_type = _required(recovery_contract, "RecoveryWorkerResult")
    result = result_type(
        outcome=outcome,
        reason_code=(
            "global_discovery_recovery_completed"
            if outcome == "success"
            else "global_discovery_recovery_rolled_back"
        ),
        retryable=False,
        counts=status.counts,
        physical_truth=truth_type(
            attempt_id=status.attempt_id,
            journal_phase=journal_phase,
            pointer_replaced=pointer_replaced,
            rollback_performed=rollback_performed,
            evidence_ref=f"journal://{status.attempt_id}",
        ),
    )

    _complete(
        worker,
        status,
        started_monotonic=clock.monotonic(),
        baseline=status.active_elapsed_ms,
        result=result,
    )

    persisted = store.status
    assert persisted.terminal_outcome.value == outcome
    assert persisted.physical_truth == result.physical_truth
    assert persisted.cancel_requested_at == status.cancel_requested_at
    assert persisted.audit_reason == status.audit_reason


def test_cancel_before_any_physical_terminal_truth_remains_cancelled() -> None:
    status = replace(
        _running_status(),
        cancel_requested_at=NOW + timedelta(milliseconds=100),
        cancel_requested_by_actor_id="agent-canceller",
        audit_reason="cancel before pointer replacement",
    )
    store = _RecordingStore(status)
    clock = ManualRecoveryClock()
    worker = _worker(store, clock)
    result_type = _required(recovery_contract, "RecoveryWorkerResult")
    native_result = result_type(
        outcome="failed",
        reason_code="native_stopped_at_cancel_fence",
        retryable=False,
        counts=status.counts,
        physical_truth=None,
    )

    _complete(
        worker,
        status,
        started_monotonic=clock.monotonic(),
        baseline=status.active_elapsed_ms,
        result=native_result,
    )

    assert store.status.terminal_outcome.value == "cancelled"
    assert store.status.reason_code == "recovery_cancelled"
    assert store.status.physical_truth is None
