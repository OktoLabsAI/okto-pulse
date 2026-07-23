from __future__ import annotations

import asyncio
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import pytest
from sqlalchemy import create_engine, event, func, insert, select, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityRecoveryPreparationPoller,
    RecoveryDispatchClaimConflict,
    RecoveryDispatchStage,
    RecoveryPreparationRetryableError,
    SQLAlchemyRecoveryRunStore,
    _PreparationProgressTracker,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityPreparedRecoveryRevoker,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    GlobalDiscoveryRecoveryAttempt,
    GlobalDiscoveryRecoveryDispatch,
    GlobalDiscoveryRecoverySlot,
    GlobalDiscoveryRecoveryTransition,
)
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryConfirmationState,
    RecoveryPreparationCommand,
    RecoveryPreparedResult,
    RecoveryProgressCounts,
    RecoveryRunBinding,
    RecoveryRunPhase,
    RecoveryRunState,
    RecoveryTerminalOutcome,
    RecoveryWorkerResult,
)
from okto_pulse.core.kg.global_discovery_recovery import (
    GlobalDiscoveryRecoveryPreparedInputStore,
    GlobalDiscoveryRecoveryService,
    GlobalDiscoveryRecoveryWorkerInputStore,
)
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
    GlobalDiscoveryBoardSeed,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    GlobalDiscoveryBoardInventory,
    GlobalDiscoveryRecoveryPreparationService,
)


NOW = datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc)


def test_reclaimed_preparation_replays_prefix_without_public_regression() -> None:
    durable = RecoveryProgressCounts(
        boards_total=1_500,
        boards_scanned=1_193,
        sources_total=10,
        sources_processed=8,
        nodes_written=1_200,
        edges_written=7,
        errors=1,
    )
    tracker = _PreparationProgressTracker(durable, replay_prefix=True)

    tracker.record(RecoveryProgressCounts(boards_total=1_500, sources_total=10))
    assert tracker.snapshot() == durable

    replayed = RecoveryProgressCounts(
        boards_total=1_500,
        boards_scanned=1_194,
        sources_total=10,
        sources_processed=9,
        nodes_written=1_201,
        edges_written=8,
    )
    tracker.record(replayed)
    assert tracker.snapshot() == RecoveryProgressCounts(
        boards_total=1_500,
        boards_scanned=1_194,
        sources_total=10,
        sources_processed=9,
        nodes_written=1_201,
        edges_written=8,
        errors=1,
    )

    with pytest.raises(ValueError, match="boards_scanned"):
        tracker.record(
            RecoveryProgressCounts(
                boards_total=1_500,
                boards_scanned=1_193,
                sources_total=10,
                sources_processed=9,
                nodes_written=1_201,
                edges_written=8,
            )
        )


def test_first_preparation_attempt_still_rejects_progress_regression() -> None:
    tracker = _PreparationProgressTracker(
        RecoveryProgressCounts(
            boards_total=3,
            boards_scanned=2,
            sources_total=0,
        )
    )

    with pytest.raises(ValueError, match="boards_scanned"):
        tracker.record(
            RecoveryProgressCounts(
                boards_total=3,
                boards_scanned=1,
                sources_total=0,
            )
        )


@dataclass
class _Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class _Revoker:
    def __init__(self) -> None:
        self.revocations: list[tuple[str, int, str]] = []

    def revoke_prepared(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
        **_kwargs,
    ) -> None:
        key = (run_id, epoch, manifest_ref)
        if key not in self.revocations:
            self.revocations.append(key)

    def is_prepared_revoked(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
    ) -> bool:
        return (run_id, epoch, manifest_ref) in self.revocations


def _open_store(
    path: Path,
    *,
    clock: _Clock,
    revoker: _Revoker,
    sqlite_timeout_seconds: float = 5.0,
) -> tuple[SQLAlchemyRecoveryRunStore, object]:
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={
            "check_same_thread": False,
            "timeout": sqlite_timeout_seconds,
        },
    )

    @event.listens_for(engine, "connect")
    def set_test_busy_timeout(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute(
            f"PRAGMA busy_timeout={max(1, int(sqlite_timeout_seconds * 1_000))}"
        )

    Base.metadata.create_all(
        engine,
        tables=[
            Board.__table__,
            GlobalDiscoveryRecoveryAttempt.__table__,
            GlobalDiscoveryRecoverySlot.__table__,
            GlobalDiscoveryRecoveryDispatch.__table__,
            GlobalDiscoveryRecoveryTransition.__table__,
        ],
    )
    with engine.begin() as connection:
        connection.execute(
            insert(Board.__table__).values(
                id="board-preparation-failure",
                name="Preparation failure",
                owner_id="agent-preparer",
                realm_id="local",
            )
        )
    return (
        SQLAlchemyRecoveryRunStore(
            engine=engine,
            prepared_revoker=revoker,
            wall_clock=clock,
        ),
        engine,
    )


def _admit(
    store: SQLAlchemyRecoveryRunStore,
    run_id: str,
    *,
    budget_ms: int = 60_000,
):
    status, created = store.admit_preparation(
        RecoveryPreparationCommand(
            binding=RecoveryRunBinding(
                run_id=run_id,
                actor_id="agent-preparer",
            ),
            admitted_at=NOW,
            counts=RecoveryProgressCounts(
                boards_total=99,
                sources_total=1,
            ),
            attempt_budget_ms=budget_ms,
        )
    )
    assert created is True
    assert status.counts.boards_total == 1
    return status


def _rows(engine) -> tuple[int, dict[str, object]]:
    with engine.connect() as connection:
        slot_count = int(
            connection.scalar(
                select(func.count()).select_from(GlobalDiscoveryRecoverySlot.__table__)
            )
            or 0
        )
        dispatch = dict(
            connection.execute(select(GlobalDiscoveryRecoveryDispatch.__table__))
            .mappings()
            .one()
        )
    return slot_count, dispatch


def _claim_preparation(
    store: SQLAlchemyRecoveryRunStore,
    run_id: str,
) -> tuple[object, object]:
    _admit(store, run_id)
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id=f"worker-{run_id}",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=15),
    )
    assert claim is not None
    preparing = store.mark_preparing(
        run_id=claim.run_id,
        attempt_id=claim.attempt_id,
        epoch=claim.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    return claim, preparing


def _transition_count(engine, *, run_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                select(func.count())
                .select_from(GlobalDiscoveryRecoveryTransition.__table__)
                .where(GlobalDiscoveryRecoveryTransition.run_id == run_id)
            )
            or 0
        )


def test_plain_adapter_runtime_error_is_terminal_and_releases_slot(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "terminal-runtime-error.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    _admit(store, "gdr_r5_terminal_runtime_error")

    def fail(**_kwargs):
        raise RuntimeError("sensitive adapter detail must not persist")

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=fail,
        wall_clock=clock,
        heartbeat_interval_ms=50,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        status = store.get_status(run_id="gdr_r5_terminal_runtime_error")
        assert status is not None
        assert status.state is RecoveryRunState.FAILED
        assert status.terminal_outcome is RecoveryTerminalOutcome.FAILED
        assert status.retryable is False
        assert status.reason_code == "global_discovery_recovery_preparation_failed"
        assert status.counts.errors == 1
        assert status.updated_at - status.started_at <= timedelta(seconds=30)
        slot_count, dispatch = _rows(engine)
        assert slot_count == 0
        assert dispatch["state"] == "done"
        assert "sensitive" not in str(dispatch["result_payload"])
    finally:
        poller.close()
        engine.dispose()


def test_preparation_poll_thread_uses_constructor_composition_context(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "preparation-constructor-context.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    _admit(store, "gdr_r5_preparation_constructor_context")
    active_composition = ContextVar[str | None](
        "test_preparation_constructor_composition",
        default=None,
    )
    operation_entered = Event()
    observed: list[str | None] = []

    def fail(**_kwargs):
        observed.append(active_composition.get())
        operation_entered.set()
        raise RuntimeError("terminal fixture after context observation")

    startup_token = active_composition.set("community-startup-composition")
    try:
        poller = CommunityRecoveryPreparationPoller(
            store=store,
            operation=fail,
            wall_clock=clock,
            poll_interval_seconds=0.01,
            heartbeat_interval_ms=50,
        )
    finally:
        active_composition.reset(startup_token)

    request_token = active_composition.set("request-local-dispatch")
    try:
        poller.start()
    finally:
        active_composition.reset(request_token)

    try:
        assert operation_entered.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        status = store.get_status(run_id="gdr_r5_preparation_constructor_context")
        while status is not None and not status.state.is_terminal:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            status = store.get_status(run_id="gdr_r5_preparation_constructor_context")
        assert status is not None
        assert status.state is RecoveryRunState.FAILED
        assert observed == ["community-startup-composition"]
    finally:
        poller.close(timeout_seconds=2.0)
        engine.dispose()


def test_raw_sqlalchemy_operation_error_is_not_an_implicit_retry(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "terminal-sqlalchemy-error.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    _admit(store, "gdr_r5_terminal_sqlalchemy_error")

    def fail(**_kwargs):
        raise SQLAlchemyError("deterministic adapter SQL failure")

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=fail,
        wall_clock=clock,
        heartbeat_interval_ms=50,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        status = store.get_status(run_id="gdr_r5_terminal_sqlalchemy_error")
        assert status is not None
        assert status.state is RecoveryRunState.FAILED
        slot_count, dispatch = _rows(engine)
        assert slot_count == 0
        assert dispatch["state"] == "done"
        assert dispatch["attempt_count"] == 1
    finally:
        poller.close()
        engine.dispose()


def test_retry_configuration_cannot_exceed_hard_three_attempt_ceiling(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "retry-ceiling.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    try:
        with pytest.raises(ValueError, match="1..3"):
            CommunityRecoveryPreparationPoller(
                store=store,
                operation=lambda **_kwargs: None,
                wall_clock=clock,
                max_retry_attempts=4,
            )
    finally:
        engine.dispose()


def test_explicit_retryable_failure_has_backoff_and_hard_exhaustion(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "retry-exhaustion.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    _admit(store, "gdr_r5_retry_exhaustion")
    calls = 0

    def fail_transient(*, fence_check, **_kwargs):
        nonlocal calls
        calls += 1
        fence_check()
        raise RecoveryPreparationRetryableError("preparation_dependency_busy")

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=fail_transient,
        wall_clock=clock,
        heartbeat_interval_ms=50,
        retry_backoff_seconds=1,
        max_retry_attempts=3,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        first = store.get_status(run_id="gdr_r5_retry_exhaustion")
        assert first is not None and first.state is RecoveryRunState.RUNNING
        assert first.counts.errors == 1
        _slot_count, dispatch = _rows(engine)
        assert dispatch["state"] == "ready"
        assert dispatch["attempt_count"] == 1
        assert dispatch["available_at"].replace(tzinfo=timezone.utc) == (
            NOW + timedelta(seconds=1)
        )

        clock.advance(1.1)
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        second = store.get_status(run_id="gdr_r5_retry_exhaustion")
        assert second is not None and second.counts.errors == 2
        _slot_count, dispatch = _rows(engine)
        assert dispatch["state"] == "ready"
        assert dispatch["attempt_count"] == 2

        clock.advance(2.1)
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        exhausted = store.get_status(run_id="gdr_r5_retry_exhaustion")
        assert exhausted is not None
        assert exhausted.state is RecoveryRunState.FAILED
        assert exhausted.reason_code.endswith("preparation_retry_exhausted")
        assert exhausted.counts.errors == 3
        assert calls == 3
        slot_count, dispatch = _rows(engine)
        assert slot_count == 0
        assert dispatch["state"] == "done"
        assert dispatch["attempt_count"] == 3
    finally:
        poller.close()
        engine.dispose()


def test_retry_success_completes_with_durable_high_water_counts(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "retry-success-high-water.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_retry_success_high_water"
    _admit(store, run_id)
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "retry-success-artifacts"
    )

    class _StableRecovery:
        def current_snapshot_fingerprint(self) -> str:
            return "sha256:retry-success-snapshot"

        def inspect_live_artifact(self) -> GlobalDiscoveryArtifactSnapshot:
            return GlobalDiscoveryArtifactSnapshot(
                exists=True,
                artifact_count=1,
                total_bytes=8,
                sha256="a" * 64,
            )

    recovery_service = GlobalDiscoveryRecoveryService(
        recovery=_StableRecovery(),  # type: ignore[arg-type]
        artifact_store=artifact_store,
    )
    calls = 0

    def retry_then_prepare(
        *,
        actor_id: str,
        epoch: int,
        checkpoint,
        fence_check,
        **_kwargs,
    ):
        nonlocal calls
        calls += 1
        physical_counts = RecoveryProgressCounts(
            boards_total=1,
            boards_scanned=1,
            sources_total=1,
            sources_processed=1,
            nodes_written=1,
        )
        durable_counts = checkpoint(physical_counts)
        fence_check()
        if calls == 1:
            raise RecoveryPreparationRetryableError("preparation_dependency_busy")
        assert isinstance(durable_counts, RecoveryProgressCounts)
        result = recovery_service.stage_prepared_inputs(
            run_id=run_id,
            epoch=epoch,
            actor_id=actor_id,
            boards=(
                GlobalDiscoveryBoardInventory(
                    board_id="board-preparation-failure",
                    board_name="Preparation failure",
                    source_count=1,
                    source_set_hash="retry-success-source-set",
                ),
            ),
            health_evidence=(
                {
                    "board_id": "board-preparation-failure",
                    "graph_state": "healthy",
                    "discovery_state": "recovery_needed",
                    "discovery_recovery_required": True,
                    "primary_health_cause": "discovery_recovery_required",
                },
            ),
            candidate_boards=(
                GlobalDiscoveryBoardSeed(
                    board_id="board-preparation-failure",
                    board_name="Preparation failure",
                    summary="",
                    summary_embedding=(),
                    digests=(),
                    source_inventory_hash="retry-success-source-set",
                ),
            ),
            expected_snapshot_fingerprint="sha256:retry-success-snapshot",
            fence_check=fence_check,
            prepared_at=clock(),
            terminal_counts=durable_counts,
        )
        fence_check(manifest_ref=result.manifest_ref)
        return result

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=retry_then_prepare,
        wall_clock=clock,
        heartbeat_interval_ms=50,
        retry_backoff_seconds=1,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        first = store.get_status(run_id=run_id)
        assert first is not None
        assert first.state is RecoveryRunState.RUNNING
        assert first.counts.errors == 1

        clock.advance(1.1)
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        prepared = store.get_status(run_id=run_id)
        assert prepared is not None
        assert prepared.state is RecoveryRunState.PENDING
        assert prepared.phase is RecoveryRunPhase.PREPARED
        assert prepared.confirmation_state is RecoveryConfirmationState.PREPARED
        assert prepared.reason_code == "recovery_prepared"
        assert prepared.binding.manifest_ref is not None
        assert prepared.counts == RecoveryProgressCounts(
            boards_total=1,
            boards_scanned=1,
            sources_total=1,
            sources_processed=1,
            nodes_written=1,
            errors=1,
        )
        staged = GlobalDiscoveryRecoveryPreparedInputStore(artifact_store).load(
            run_id,
            epoch=prepared.epoch,
        )
        assert staged is not None
        assert staged.terminal_counts == prepared.counts
        assert calls == 2
        slot_count, dispatch = _rows(engine)
        assert slot_count == 1
        assert dispatch["state"] == "done"
        assert dispatch["attempt_count"] == 2

        confirmed = recovery_service.confirm(
            actor_id="agent-confirmer",
            run_id=run_id,
            manifest_ref=prepared.binding.manifest_ref,
            preflight_hash=prepared.binding.preflight_hash or "",
            current_snapshot_fingerprint="sha256:retry-success-snapshot",
            now=clock.advance(0.1),
        )
        command = recovery_service.prepare_durable_start(
            actor_id="agent-confirmer",
            confirmation_id=str(confirmed["confirmation_id"]),
            manifest_ref=prepared.binding.manifest_ref,
            preflight_hash=prepared.binding.preflight_hash or "",
            reason="confirm retry with durable high-water counts",
            current_snapshot_fingerprint="sha256:retry-success-snapshot",
            started_at=clock.advance(0.1),
        )
        assert command.counts == prepared.counts
        worker_inputs = GlobalDiscoveryRecoveryWorkerInputStore(artifact_store).load(
            run_id, epoch=prepared.epoch
        )
        assert worker_inputs is not None
        assert worker_inputs.terminal_counts == prepared.counts

        confirmed_status, transitioned = store.admit_prepared_start(command)
        assert transitioned is True
        assert confirmed_status.phase is RecoveryRunPhase.CONFIRMED
        claim_at = clock.advance(0.1)
        recovery_claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.RECOVERY,
            worker_id="retry-success-worker",
            claimed_at=claim_at,
            claim_expires_at=claim_at + timedelta(seconds=15),
        )
        assert recovery_claim is not None
        running = store.get_status(run_id=run_id)
        assert running is not None
        assert running.phase is RecoveryRunPhase.CUTOVER
        terminal = store.complete_recovery(
            dispatch_id=recovery_claim.dispatch_id,
            claim_token=recovery_claim.claim_token,
            expected_progress_seq=running.progress_seq,
            completed_at=clock.advance(0.1),
            active_elapsed_ms=100,
            result=RecoveryWorkerResult(
                outcome=RecoveryTerminalOutcome.SUCCESS,
                reason_code="global_discovery_recovery_completed",
                retryable=False,
                counts=worker_inputs.terminal_counts,
            ),
        )
        assert terminal.state is RecoveryRunState.SUCCESS
        assert terminal.phase is RecoveryRunPhase.TERMINAL
        assert terminal.counts.errors == 1
    finally:
        poller.close()
        engine.dispose()


def test_cancel_during_retry_backoff_terminalizes_ready_dispatch_immediately(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "cancel-retry-backoff.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_cancel_retry_backoff"
    admitted = _admit(store, run_id)

    def fail_transient(*, fence_check, **_kwargs):
        fence_check()
        raise RecoveryPreparationRetryableError("preparation_dependency_busy")

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=fail_transient,
        wall_clock=clock,
        heartbeat_interval_ms=50,
        retry_backoff_seconds=30,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        _slot_count, ready = _rows(engine)
        assert ready["state"] == "ready"
        assert ready["available_at"].replace(tzinfo=timezone.utc) == (
            NOW + timedelta(seconds=30)
        )

        cancelled = store.request_cancel(
            run_id=run_id,
            expected_epoch=admitted.epoch,
            requested_at=NOW + timedelta(milliseconds=500),
            requested_by_actor_id="agent-canceller",
            reason="cancel while retry is waiting",
        )
        assert cancelled.state is RecoveryRunState.CANCELLED
        slot_count, dispatch = _rows(engine)
        assert slot_count == 0
        assert dispatch["state"] == "done"
    finally:
        poller.close()
        engine.dispose()


def test_ready_cancel_locks_slot_before_attempt_cas(tmp_path: Path) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "ready-cancel-lock-order.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_ready_cancel_lock_order"
    admitted = _admit(store, run_id)
    events: list[str] = []
    original_lock_slot = store._lock_slot  # noqa: SLF001
    original_write_cas = store._write_cas  # noqa: SLF001

    def record_slot_lock(connection, *, status, at):
        events.append("slot")
        return original_lock_slot(connection, status=status, at=at)

    def record_attempt_cas(connection, *, current, updated):
        events.append("attempt")
        return original_write_cas(
            connection,
            current=current,
            updated=updated,
        )

    store._lock_slot = record_slot_lock  # type: ignore[method-assign]  # noqa: SLF001
    store._write_cas = record_attempt_cas  # type: ignore[method-assign]  # noqa: SLF001
    try:
        cancelled = store.request_cancel(
            run_id=run_id,
            expected_epoch=admitted.epoch,
            requested_at=NOW + timedelta(milliseconds=1),
            requested_by_actor_id="agent-canceller",
            reason="verify PostgreSQL lock order",
        )
        assert cancelled.state is RecoveryRunState.CANCELLED
        # T1 must lock the global slot before reserving settlement; T2 then
        # performs the terminal attempt CAS after the external manifest action.
        assert events == ["slot", "attempt", "attempt"]
    finally:
        engine.dispose()


def test_atomic_heartbeat_renews_claim_and_stale_token_cannot_terminalize(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    path = tmp_path / "atomic-heartbeat.sqlite3"
    first, first_engine = _open_store(path, clock=clock, revoker=revoker)
    _admit(first, "gdr_r5_atomic_heartbeat")
    claim = first.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="worker-a",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=15),
    )
    assert claim is not None
    first.mark_preparing(
        run_id=claim.run_id,
        attempt_id=claim.attempt_id,
        epoch=claim.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    heartbeat = first.heartbeat_preparation(
        dispatch_id=claim.dispatch_id,
        claim_token=claim.claim_token,
        observed_at=NOW + timedelta(seconds=10),
        requested_expires_at=NOW + timedelta(seconds=25),
        active_elapsed_ms=10_000,
        counts=RecoveryProgressCounts(boards_total=1, sources_total=1),
    )
    assert heartbeat.active_elapsed_ms == 10_000
    second_engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    second = SQLAlchemyRecoveryRunStore(
        engine=second_engine,
        prepared_revoker=revoker,
        wall_clock=clock,
    )
    try:
        assert (
            second.claim_next_dispatch(
                stage=RecoveryDispatchStage.PREPARATION,
                worker_id="worker-b",
                claimed_at=NOW + timedelta(seconds=16),
                claim_expires_at=NOW + timedelta(seconds=31),
            )
            is None
        )
        with pytest.raises(RecoveryDispatchClaimConflict):
            first.record_preparation_failure(
                dispatch_id=claim.dispatch_id,
                claim_token=claim.claim_token,
                failed_at=NOW + timedelta(seconds=26),
                active_elapsed_ms=26_000,
                counts=RecoveryProgressCounts(
                    boards_total=1,
                    sources_total=1,
                    errors=1,
                ),
                reason_code="expired_worker_failure",
                retryable=False,
                retry_available_at=NOW + timedelta(seconds=27),
                max_attempts=3,
            )
        replacement = second.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="worker-b",
            claimed_at=NOW + timedelta(seconds=26),
            claim_expires_at=NOW + timedelta(seconds=41),
        )
        assert replacement is not None
        assert replacement.claim_token != claim.claim_token
        with pytest.raises(RecoveryDispatchClaimConflict):
            first.record_preparation_failure(
                dispatch_id=claim.dispatch_id,
                claim_token=claim.claim_token,
                failed_at=NOW + timedelta(seconds=26),
                active_elapsed_ms=26_000,
                counts=RecoveryProgressCounts(
                    boards_total=1,
                    sources_total=1,
                    errors=1,
                ),
                reason_code="stale_worker_failure",
                retryable=False,
                retry_available_at=NOW + timedelta(seconds=27),
                max_attempts=3,
            )
    finally:
        first_engine.dispose()
        second_engine.dispose()


def test_preparation_heartbeat_retries_real_short_sqlite_lock_in_fresh_uow(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    path = tmp_path / "heartbeat-short-lock.sqlite3"
    store, engine = _open_store(
        path,
        clock=clock,
        revoker=revoker,
        sqlite_timeout_seconds=0.05,
    )
    run_id = "gdr_r5_heartbeat_short_lock"
    claim, preparing = _claim_preparation(store, run_id)
    poller_started = time.monotonic()

    def advancing_wall_clock() -> datetime:
        return NOW + timedelta(seconds=time.monotonic() - poller_started)

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=lambda **_kwargs: None,
        wall_clock=advancing_wall_clock,
    )
    heartbeat_calls = 0
    original_heartbeat = store.heartbeat_preparation

    def tracked_heartbeat(**kwargs):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        return original_heartbeat(**kwargs)

    store.heartbeat_preparation = tracked_heartbeat  # type: ignore[method-assign]
    before_transition_count = _transition_count(engine, run_id=run_id)
    _slot_count, before_dispatch = _rows(engine)
    lock_owner = sqlite3.connect(
        path,
        timeout=0.05,
        isolation_level=None,
        check_same_thread=False,
    )
    lock_owner.execute("BEGIN IMMEDIATE")
    released = Event()

    def release_short_lock() -> None:
        # Admission deliberately installs a 750 ms busy timeout on the pooled
        # connection. Hold past one complete store attempt, then release while
        # the second fresh transaction is waiting.
        time.sleep(1.1)
        lock_owner.rollback()
        released.set()

    release_thread = Thread(target=release_short_lock, daemon=True)
    release_thread.start()
    started_monotonic = time.monotonic()
    try:
        heartbeat = poller._persist_preparation_heartbeat(  # noqa: SLF001
            claim=claim,
            started_monotonic=started_monotonic,
            baseline_elapsed_ms=preparing.active_elapsed_ms,
            deadline_at_monotonic=started_monotonic + 30,
            attempt_budget_ms=preparing.attempt_budget_ms,
            counts=preparing.counts,
        )
        release_thread.join(timeout=2)
        assert released.is_set()
        assert heartbeat_calls >= 2
        assert heartbeat.active_elapsed_ms > preparing.active_elapsed_ms
        assert heartbeat.progress_seq == preparing.progress_seq + 1
        assert _transition_count(engine, run_id=run_id) == (before_transition_count + 1)
        _slot_count, after_dispatch = _rows(engine)
        assert after_dispatch["claim_expires_at"] > before_dispatch["claim_expires_at"]
    finally:
        if not released.is_set():
            lock_owner.rollback()
        release_thread.join(timeout=2)
        lock_owner.close()
        poller.close()
        engine.dispose()


def test_preparation_heartbeat_persistent_sqlite_lock_is_bounded_and_atomic(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    path = tmp_path / "heartbeat-persistent-lock.sqlite3"
    store, engine = _open_store(
        path,
        clock=clock,
        revoker=revoker,
        sqlite_timeout_seconds=0.05,
    )
    run_id = "gdr_r5_heartbeat_persistent_lock"
    claim, preparing = _claim_preparation(store, run_id)
    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=lambda **_kwargs: None,
        wall_clock=clock,
    )
    heartbeat_calls = 0
    original_heartbeat = store.heartbeat_preparation

    def tracked_heartbeat(**kwargs):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        return original_heartbeat(**kwargs)

    store.heartbeat_preparation = tracked_heartbeat  # type: ignore[method-assign]
    before_status = store.get_status(run_id=run_id)
    before_transition_count = _transition_count(engine, run_id=run_id)
    _slot_count, before_dispatch = _rows(engine)
    lock_owner = sqlite3.connect(
        path,
        timeout=0.05,
        isolation_level=None,
        check_same_thread=False,
    )
    lock_owner.execute("BEGIN IMMEDIATE")
    started_monotonic = time.monotonic()
    try:
        with pytest.raises(OperationalError) as caught:
            poller._persist_preparation_heartbeat(  # noqa: SLF001
                claim=claim,
                started_monotonic=started_monotonic,
                baseline_elapsed_ms=preparing.active_elapsed_ms,
                deadline_at_monotonic=started_monotonic + 30,
                attempt_budget_ms=preparing.attempt_budget_ms,
                counts=preparing.counts,
            )
        assert isinstance(caught.value.orig, sqlite3.OperationalError)
        assert heartbeat_calls == 3
        assert time.monotonic() - started_monotonic < 4.0
        assert store.get_status(run_id=run_id) == before_status
        assert _transition_count(engine, run_id=run_id) == before_transition_count
        _slot_count, after_dispatch = _rows(engine)
        assert after_dispatch["claim_expires_at"] == before_dispatch["claim_expires_at"]
    finally:
        lock_owner.rollback()
        lock_owner.close()
        poller.close()
        engine.dispose()


def test_preparation_heartbeat_does_not_retry_text_only_non_sqlite_error(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "heartbeat-non-sqlite-error.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    claim, preparing = _claim_preparation(
        store,
        "gdr_r5_heartbeat_non_sqlite_error",
    )
    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=lambda **_kwargs: None,
        wall_clock=clock,
    )
    heartbeat_calls = 0

    def fail_with_text_only_error(**_kwargs):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        raise OperationalError(
            "opaque statement",
            {},
            RuntimeError("database is locked"),
        )

    store.heartbeat_preparation = fail_with_text_only_error  # type: ignore[method-assign]
    started_monotonic = time.monotonic()
    try:
        with pytest.raises(OperationalError):
            poller._persist_preparation_heartbeat(  # noqa: SLF001
                claim=claim,
                started_monotonic=started_monotonic,
                baseline_elapsed_ms=preparing.active_elapsed_ms,
                deadline_at_monotonic=started_monotonic + 30,
                attempt_budget_ms=preparing.attempt_budget_ms,
                counts=preparing.counts,
            )
        assert heartbeat_calls == 1
    finally:
        poller.close()
        engine.dispose()


def test_delayed_startup_terminalizes_expired_ready_dispatch(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "expired-ready.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    _admit(store, "gdr_r5_expired_ready", budget_ms=5_000)
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="late-worker",
        claimed_at=NOW + timedelta(seconds=6),
        claim_expires_at=NOW + timedelta(seconds=21),
    )
    assert claim is None
    status = store.get_status(run_id="gdr_r5_expired_ready")
    assert status is not None
    assert status.state is RecoveryRunState.TIMEOUT
    assert status.reason_code == "recovery_attempt_budget_exhausted"
    slot_count, dispatch = _rows(engine)
    assert slot_count == 0
    assert dispatch["state"] == "done"
    engine.dispose()


def test_published_manifest_survives_worker_crash_and_timeout_sweep(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "published-manifest-crash.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_published_manifest_crash"
    admitted = _admit(store, run_id, budget_ms=5_000)
    manifest_ref = f"manifest://{run_id}/{admitted.attempt_id}"
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="crashing-worker",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=30),
    )
    assert claim is not None
    store.mark_preparing(
        run_id=run_id,
        attempt_id=claim.attempt_id,
        epoch=claim.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    store.heartbeat_preparation(
        dispatch_id=claim.dispatch_id,
        claim_token=claim.claim_token,
        observed_at=NOW + timedelta(seconds=1),
        requested_expires_at=NOW + timedelta(seconds=20),
        active_elapsed_ms=1_000,
        counts=admitted.counts,
        manifest_ref=manifest_ref,
    )
    _slot_count, published_dispatch = _rows(engine)
    assert published_dispatch["result_payload"]["published_manifest_ref"] == (
        manifest_ref
    )

    assert (
        store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="sweeper",
            claimed_at=NOW + timedelta(seconds=6),
            claim_expires_at=NOW + timedelta(seconds=21),
        )
        is None
    )
    swept = store.get_status(run_id=run_id)
    assert swept is not None
    assert swept.state is RecoveryRunState.TIMEOUT
    assert revoker.revocations == [(run_id, admitted.epoch, manifest_ref)]
    slot_count, dispatch = _rows(engine)
    assert slot_count == 0
    assert dispatch["state"] == "done"
    assert dispatch["result_payload"]["manifest_revoked"] is True
    engine.dispose()


def test_attempt_exhaustion_preserves_pending_cancel_precedence(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "attempt-exhaustion-cancel.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_attempt_exhaustion_cancel"
    admitted = _admit(store, run_id)
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="third-worker",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=2),
    )
    assert claim is not None
    store.mark_preparing(
        run_id=run_id,
        attempt_id=claim.attempt_id,
        epoch=claim.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    store.request_cancel(
        run_id=run_id,
        expected_epoch=admitted.epoch,
        requested_at=NOW + timedelta(seconds=1),
        requested_by_actor_id="agent-canceller",
        reason="cancel before third worker reclaim",
    )
    with engine.begin() as connection:
        connection.execute(
            update(GlobalDiscoveryRecoveryDispatch.__table__).values(
                attempt_count=3,
            )
        )

    assert (
        store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="fourth-worker",
            claimed_at=NOW + timedelta(seconds=3),
            claim_expires_at=NOW + timedelta(seconds=18),
        )
        is None
    )
    cancelled = store.get_status(run_id=run_id)
    assert cancelled is not None
    assert cancelled.state is RecoveryRunState.CANCELLED
    slot_count, dispatch = _rows(engine)
    assert slot_count == 0
    assert dispatch["state"] == "done"
    engine.dispose()


def test_cancel_after_prepared_result_revokes_before_terminal_sql(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "cancel-after-result.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_cancel_after_result"
    _admit(store, run_id)
    manifest_ref = f"manifest://{run_id}/attempt-1"

    def prepare(*, epoch: int, fence_check, checkpoint, **_kwargs):
        fence_check()
        checkpoint(
            RecoveryProgressCounts(
                boards_total=1,
                boards_scanned=1,
                sources_total=1,
                sources_processed=1,
            )
        )
        fence_check()
        fence_check()
        fence_check(manifest_ref=manifest_ref)
        cancelled_at = clock.advance(1)
        store.request_cancel(
            run_id=run_id,
            expected_epoch=epoch,
            requested_at=cancelled_at,
            requested_by_actor_id="agent-canceller",
            reason="cancel after prepared stage return",
        )
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash="preflight-cancel-after-result",
            snapshot_fingerprint="sha256:cancel-after-result",
            prepared_at=cancelled_at,
            expires_at=cancelled_at + timedelta(seconds=300),
            counts=RecoveryProgressCounts(
                boards_total=1,
                boards_scanned=1,
                sources_total=1,
                sources_processed=1,
            ),
        )

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=prepare,
        wall_clock=clock,
        heartbeat_interval_ms=50,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        status = store.get_status(run_id=run_id)
        assert status is not None
        assert status.state is RecoveryRunState.CANCELLED
        assert revoker.revocations == [(run_id, 1, manifest_ref)]
        slot_count, dispatch = _rows(engine)
        assert slot_count == 0
        assert dispatch["state"] == "done"
        assert dispatch["result_payload"]["manifest_revoked"] is True
    finally:
        poller.close()
        engine.dispose()


def test_failure_after_manifest_publication_is_terminal_and_revoked(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "failure-after-publication.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_failure_after_publication"
    admitted = _admit(store, run_id)
    manifest_ref = f"manifest://{run_id}/{admitted.attempt_id}"

    def fail_after_publication(*, fence_check, **_kwargs):
        fence_check()
        fence_check()
        fence_check()
        fence_check(manifest_ref=manifest_ref)
        raise RuntimeError("adapter failed after publication")

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=fail_after_publication,
        wall_clock=clock,
        heartbeat_interval_ms=50,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        status = store.get_status(run_id=run_id)
        assert status is not None
        assert status.state is RecoveryRunState.FAILED
        assert status.retryable is False
        assert revoker.revocations == [(run_id, admitted.epoch, manifest_ref)]
        slot_count, dispatch = _rows(engine)
        assert slot_count == 0
        assert dispatch["state"] == "done"
        assert dispatch["result_payload"]["manifest_revoked"] is True
    finally:
        poller.close()
        engine.dispose()


def test_blocked_adapter_cancel_is_visible_within_heartbeat_window(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "blocked-cancel.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_blocked_cancel"
    _admit(store, run_id)
    entered = Event()
    release = Event()

    def block(*, fence_check, **_kwargs):
        fence_check()
        entered.set()
        assert release.wait(timeout=5)
        fence_check()
        raise AssertionError("cancel fence should stop blocked operation")

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=block,
        wall_clock=clock,
        heartbeat_interval_ms=50,
        poll_interval_seconds=0.01,
    )
    poller.start()
    try:
        poller.notify()
        assert entered.wait(timeout=2)
        cancelled_at = clock.advance(1)
        store.request_cancel(
            run_id=run_id,
            expected_epoch=1,
            requested_at=cancelled_at,
            requested_by_actor_id="agent-canceller",
            reason="cancel blocked preparation",
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = store.get_status(run_id=run_id)
            if status is not None and status.state is RecoveryRunState.CANCELLED:
                break
            time.sleep(0.01)
        else:
            pytest.fail("cancel did not become terminal within heartbeat window")
        assert time.monotonic() < deadline
        slot_count, dispatch = _rows(engine)
        assert slot_count == 0
        assert dispatch["state"] == "done"
    finally:
        release.set()
        poller.close()
        engine.dispose()


def test_cancellable_hung_seed_times_out_and_later_dispatch_uses_executor(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "hung-seed-later-dispatch.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    first_run_id = "gdr_r5_hung_seed_timeout"
    second_run_id = "gdr_r5_after_hung_seed"
    _admit(store, first_run_id, budget_ms=150)
    seed_entered = Event()
    seed_cancelled = Event()
    prepared_runs: list[str] = []

    def operation(
        *,
        run_id: str,
        attempt_id: str,
        deadline_at_monotonic: float,
        fence_check,
        checkpoint,
        **_kwargs,
    ):
        fence_check()
        if run_id == first_run_id:

            async def hung_seed() -> None:
                seed_entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    seed_cancelled.set()

            async def build_with_deadline() -> None:
                task = asyncio.create_task(hung_seed())
                try:
                    await asyncio.wait_for(
                        task,
                        timeout=max(
                            0.001,
                            deadline_at_monotonic - time.monotonic(),
                        ),
                    )
                except asyncio.TimeoutError:
                    fence_check()

            asyncio.run(build_with_deadline())
            raise AssertionError("attempt deadline fence must stop hung seed")

        counts = RecoveryProgressCounts(
            boards_total=1,
            boards_scanned=1,
            sources_total=1,
            sources_processed=1,
        )
        checkpoint(counts)
        fence_check()
        fence_check()
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check(manifest_ref=manifest_ref)
        fence_check(manifest_ref=manifest_ref)
        prepared_runs.append(run_id)
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=NOW,
            expires_at=NOW + timedelta(seconds=300),
            counts=counts,
        )

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=operation,
        wall_clock=clock,
        heartbeat_interval_ms=25,
        poll_interval_seconds=0.01,
    )
    try:
        started = time.monotonic()
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        timed_out = store.get_status(run_id=first_run_id)
        assert timed_out is not None
        assert timed_out.state is RecoveryRunState.TIMEOUT
        assert seed_entered.is_set()
        assert seed_cancelled.wait(timeout=0.5)

        _admit(store, second_run_id, budget_ms=10_000)
        deadline = time.monotonic() + 2
        while not poller._claim_and_prepare_one():  # noqa: SLF001
            if time.monotonic() >= deadline:
                pytest.fail("later preparation remained starved behind hung seed")
            time.sleep(0.01)
        prepared = store.get_status(run_id=second_run_id)
        assert prepared is not None
        assert prepared.phase.value == "prepared"
        assert prepared_runs == [second_run_id]
        assert time.monotonic() - started < 2
    finally:
        poller.close(timeout_seconds=2)
        engine.dispose()


def test_delayed_claim_passes_only_original_deadline_remainder(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "delayed-claim-deadline.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_delayed_claim_deadline"
    _admit(store, run_id, budget_ms=1_000)
    clock.advance(0.9)
    observed_remaining: list[float] = []

    def prepare(
        *,
        attempt_id: str,
        deadline_at_monotonic: float,
        fence_check,
        checkpoint,
        **_kwargs,
    ) -> RecoveryPreparedResult:
        observed_remaining.append(deadline_at_monotonic - time.monotonic())
        counts = RecoveryProgressCounts(
            boards_total=1,
            boards_scanned=1,
            sources_total=1,
            sources_processed=1,
        )
        fence_check()
        checkpoint(counts)
        fence_check()
        fence_check()
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check(manifest_ref=manifest_ref)
        fence_check(manifest_ref=manifest_ref)
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash="preflight-delayed-claim",
            snapshot_fingerprint="sha256:delayed-claim",
            prepared_at=clock(),
            expires_at=clock() + timedelta(seconds=300),
            counts=counts,
        )

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=prepare,
        wall_clock=clock,
        heartbeat_interval_ms=25,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        assert len(observed_remaining) == 1
        assert 0 < observed_remaining[0] < 0.2
        status = store.get_status(run_id=run_id)
        assert status is not None
        assert status.phase.value == "prepared"
    finally:
        poller.close(timeout_seconds=2)
        engine.dispose()


def test_mark_preparing_latency_is_not_added_back_to_operation_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "mark-preparing-latency.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_mark_preparing_latency"
    _admit(store, run_id, budget_ms=100)
    original_mark_preparing = store.mark_preparing

    def delayed_mark_preparing(**kwargs):
        status = original_mark_preparing(**kwargs)
        clock.advance(0.09)
        return status

    monkeypatch.setattr(store, "mark_preparing", delayed_mark_preparing)
    observed_remaining: list[float] = []
    monotonic_now = 1_000.0

    def prepare(
        *,
        attempt_id: str,
        deadline_at_monotonic: float,
        fence_check,
        checkpoint,
        **_kwargs,
    ) -> RecoveryPreparedResult:
        observed_remaining.append(deadline_at_monotonic - monotonic_now)
        counts = RecoveryProgressCounts(
            boards_total=1,
            boards_scanned=1,
            sources_total=1,
            sources_processed=1,
        )
        checkpoint(counts)
        fence_check()
        fence_check()
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check(manifest_ref=manifest_ref)
        fence_check(manifest_ref=manifest_ref)
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash="preflight-mark-latency",
            snapshot_fingerprint="sha256:mark-latency",
            prepared_at=clock(),
            expires_at=clock() + timedelta(seconds=300),
            counts=counts,
        )

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=prepare,
        wall_clock=clock,
        monotonic_clock=lambda: monotonic_now,
        heartbeat_interval_ms=25,
    )
    try:
        assert poller._claim_and_prepare_one() is True  # noqa: SLF001
        assert len(observed_remaining) == 1
        assert 0 <= observed_remaining[0] <= 0.01
        status = store.get_status(run_id=run_id)
        assert status is not None
        assert status.phase.value == "prepared"
    finally:
        poller.close(timeout_seconds=2)
        engine.dispose()


def test_operation_and_poller_heartbeats_are_serialized_across_claim(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "serialized-preparation-heartbeats.sqlite3",
        clock=clock,
        revoker=revoker,
    )

    def prepare(
        *,
        run_id: str,
        attempt_id: str,
        fence_check,
        checkpoint,
        **_kwargs,
    ) -> RecoveryPreparedResult:
        counts = RecoveryProgressCounts(
            boards_total=1,
            boards_scanned=1,
            sources_total=1,
            sources_processed=1,
        )
        checkpoint(counts)
        for _ in range(12):
            fence_check()
            time.sleep(0.002)
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check(manifest_ref=manifest_ref)
        fence_check(manifest_ref=manifest_ref)
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=clock(),
            expires_at=clock() + timedelta(seconds=300),
            counts=counts,
        )

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=prepare,
        wall_clock=clock,
        heartbeat_interval_ms=1,
    )
    try:
        for index in range(20):
            run_id = f"gdr_r5_serialized_heartbeat_{index}"
            admitted = _admit(store, run_id, budget_ms=10_000)
            assert poller._claim_and_prepare_one() is True  # noqa: SLF001
            prepared = store.get_status(run_id=run_id)
            assert prepared is not None
            assert prepared.phase.value == "prepared"
            cancelled = store.cancel_prepared(
                run_id=run_id,
                expected_epoch=admitted.epoch,
                requested_at=clock.advance(0.001),
                requested_by_actor_id="agent-canceller",
                reason="release serialized heartbeat test slot",
            )
            assert cancelled.state is RecoveryRunState.CANCELLED
    finally:
        poller.close(timeout_seconds=2)
        engine.dispose()


def test_expiry_recovers_attempt_bound_manifest_before_first_sql_fence(
    tmp_path: Path,
) -> None:
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "crash-gap-artifacts"
    )
    revoker = CommunityPreparedRecoveryRevoker(artifact_store=artifact_store)
    clock = _Clock()
    store, engine = _open_store(
        tmp_path / "crash-before-manifest-fence.sqlite3",
        clock=clock,
        revoker=revoker,  # type: ignore[arg-type]
    )
    run_id = "gdr_r5_crash_before_manifest_fence"
    admitted = _admit(store, run_id, budget_ms=5_000)
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="crashing-preparer",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=15),
    )
    assert claim is not None
    store.mark_preparing(
        run_id=run_id,
        attempt_id=admitted.attempt_id,
        epoch=admitted.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )

    class _StableRecovery:
        def current_snapshot_fingerprint(self) -> str:
            return "sha256:crash-gap-snapshot"

        def inspect_live_artifact(self) -> GlobalDiscoveryArtifactSnapshot:
            return GlobalDiscoveryArtifactSnapshot(
                exists=True,
                artifact_count=1,
                total_bytes=8,
                sha256="a" * 64,
            )

    preparation = GlobalDiscoveryRecoveryPreparationService(
        recovery=_StableRecovery(),  # type: ignore[arg-type]
        artifact_store=artifact_store,
    )
    prepared = preparation.stage_prepared_inputs(
        run_id=run_id,
        epoch=admitted.epoch,
        actor_id=admitted.actor_id,
        boards=(
            GlobalDiscoveryBoardInventory(
                board_id="board-preparation-failure",
                board_name="Preparation failure",
                source_count=0,
                source_set_hash="empty-source-set",
            ),
        ),
        health_evidence=(
            {
                "board_id": "board-preparation-failure",
                "graph_state": "healthy",
                "discovery_state": "recovery_needed",
                "discovery_recovery_required": True,
                "primary_health_cause": "discovery_recovery_required",
            },
        ),
        candidate_boards=(
            GlobalDiscoveryBoardSeed(
                board_id="board-preparation-failure",
                board_name="Preparation failure",
                summary="",
                summary_embedding=(),
                digests=(),
                source_inventory_hash="empty-source-set",
            ),
        ),
        expected_snapshot_fingerprint="sha256:crash-gap-snapshot",
        fence_check=lambda: None,
        prepared_at=NOW + timedelta(seconds=1),
    )
    with engine.connect() as connection:
        dispatch_payload = connection.scalar(
            select(GlobalDiscoveryRecoveryDispatch.__table__.c.result_payload)
        )
    assert dispatch_payload is None

    # Simulate restart/sweep after the process stopped before its outer
    # fence_check(manifest_ref=...).  SQL has no ref; Core's attempt binding
    # is the only durable discovery path.
    assert (
        store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="restart-sweeper",
            claimed_at=NOW + timedelta(seconds=6),
            claim_expires_at=NOW + timedelta(seconds=21),
        )
        is None
    )
    terminal = store.get_status(run_id=run_id)
    assert terminal is not None
    assert terminal.state is RecoveryRunState.TIMEOUT
    assert revoker.is_prepared_revoked(
        run_id=run_id,
        epoch=admitted.epoch,
        manifest_ref=prepared.manifest_ref,
    )
    with engine.connect() as connection:
        payload = connection.scalar(
            select(GlobalDiscoveryRecoveryDispatch.__table__.c.result_payload)
        )
    assert payload["manifest_revoked"] is True
    engine.dispose()


def test_blocked_post_publication_cancel_revokes_manifest_from_heartbeat(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    revoker = _Revoker()
    store, engine = _open_store(
        tmp_path / "blocked-post-publication-cancel.sqlite3",
        clock=clock,
        revoker=revoker,
    )
    run_id = "gdr_r5_blocked_post_publication_cancel"
    admitted = _admit(store, run_id)
    manifest_ref = f"manifest://{run_id}/{admitted.attempt_id}"
    published = Event()
    release = Event()

    def block_after_publication(*, fence_check, **_kwargs):
        fence_check()
        fence_check()
        fence_check()
        fence_check(manifest_ref=manifest_ref)
        published.set()
        assert release.wait(timeout=5)
        fence_check()
        raise AssertionError("cancel fence should stop published preparation")

    poller = CommunityRecoveryPreparationPoller(
        store=store,
        operation=block_after_publication,
        wall_clock=clock,
        heartbeat_interval_ms=50,
        poll_interval_seconds=0.01,
    )
    poller.start()
    try:
        poller.notify()
        assert published.wait(timeout=2)
        cancelled_at = clock.advance(1)
        store.request_cancel(
            run_id=run_id,
            expected_epoch=admitted.epoch,
            requested_at=cancelled_at,
            requested_by_actor_id="agent-canceller",
            reason="cancel blocked published preparation",
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = store.get_status(run_id=run_id)
            if status is not None and status.state is RecoveryRunState.CANCELLED:
                break
            time.sleep(0.01)
        else:
            pytest.fail("published cancel did not settle within heartbeat window")

        assert revoker.revocations == [(run_id, admitted.epoch, manifest_ref)]
        slot_count, dispatch = _rows(engine)
        assert slot_count == 0
        assert dispatch["state"] == "done"
        assert dispatch["result_payload"]["manifest_revoked"] is True
    finally:
        release.set()
        poller.close()
        engine.dispose()
