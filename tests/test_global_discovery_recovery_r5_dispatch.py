from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, get_ident
import time

import pytest
from sqlalchemy import create_engine, func, insert, select, update

import okto_pulse.community.adapters.global_discovery_recovery_worker as worker_module
import okto_pulse.community.adapters.sqlalchemy_models as models
import okto_pulse.core.ports.global_discovery_recovery_control as recovery_contract
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityDurableRecoveryInputProvider,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryDigestSeed,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    GlobalDiscoveryRecoveryWorkerInputStore,
    GlobalDiscoveryRecoveryWorkerInputs,
)


NOW = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)


class _PreparedRevoker:
    def revoke_prepared(self, **_kwargs) -> None:
        return None

    def is_prepared_revoked(self, **_kwargs) -> bool:
        return False


def _required(owner: object, name: str):
    value = getattr(owner, name, None)
    assert value is not None, f"R5 contract is missing {name}"
    return value


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _model_types():
    return (
        models.GlobalDiscoveryRecoveryAttempt,
        _required(models, "GlobalDiscoveryRecoverySlot"),
        _required(models, "GlobalDiscoveryRecoveryDispatch"),
    )


def _open_store(database_path: Path, *, resume_input_handoff=None):
    attempt, slot, dispatch = _model_types()
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    models.Base.metadata.create_all(
        engine,
        tables=[
            models.Board.__table__,
            attempt.__table__,
            slot.__table__,
            dispatch.__table__,
            models.GlobalDiscoveryRecoveryTransition.__table__,
        ],
    )
    with engine.begin() as connection:
        if not connection.scalar(
            select(func.count()).select_from(models.Board.__table__)
        ):
            connection.execute(
                insert(models.Board.__table__),
                [
                    {
                        "id": f"board-dispatch-{index}",
                        "name": f"Dispatch {index}",
                        "owner_id": "agent-dispatch-test",
                        "realm_id": "local",
                    }
                    for index in range(2)
                ],
            )
    store_type = _required(worker_module, "SQLAlchemyRecoveryRunStore")
    return (
        store_type(
            engine=engine,
            prepared_revoker=_PreparedRevoker(),
            wall_clock=lambda: NOW + timedelta(seconds=3),
            resume_input_handoff=resume_input_handoff,
        ),
        engine,
    )


def _counts(*, scanned: int):
    counts_type = _required(recovery_contract, "RecoveryProgressCounts")
    return counts_type(
        sources_total=2,
        sources_processed=scanned,
        boards_total=2,
        boards_scanned=scanned,
    )


def _admit(store, run_id: str, *, attempt_budget_ms: int = 60_000):
    command_type = _required(recovery_contract, "RecoveryPreparationCommand")
    binding_type = _required(recovery_contract, "RecoveryRunBinding")
    status, created = _required(store, "admit_preparation")(
        command_type(
            binding=binding_type(
                run_id=run_id,
                actor_id="agent-dispatch-test",
            ),
            admitted_at=NOW,
            counts=_counts(scanned=0),
            attempt_budget_ms=attempt_budget_ms,
        )
    )
    assert created is True
    return status


def _claim(store, *, stage: object, worker_id: str, at: datetime):
    return _required(store, "claim_next_dispatch")(
        stage=stage,
        worker_id=worker_id,
        claimed_at=at,
        claim_expires_at=at + timedelta(seconds=30),
    )


def _prepare_and_enqueue_execution(store, run_id: str):
    stages = _required(worker_module, "RecoveryDispatchStage")
    admitted = _admit(store, run_id)
    claim = _claim(
        store,
        stage=stages.PREPARATION,
        worker_id="preparation-worker",
        at=NOW,
    )
    assert claim is not None
    _required(store, "mark_preparing")(
        run_id=admitted.run_id,
        attempt_id=admitted.attempt_id,
        epoch=admitted.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    prepared_type = _required(recovery_contract, "RecoveryPreparedResult")
    prepared = _required(store, "complete_preparation")(
        run_id=admitted.run_id,
        attempt_id=admitted.attempt_id,
        epoch=admitted.epoch,
        claim_token=claim.claim_token,
        completed_at=NOW + timedelta(seconds=1),
        result=prepared_type(
            manifest_ref=f"manifest://{run_id}/{admitted.attempt_id}",
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=301),
            counts=_counts(scanned=2),
        ),
    )
    start_type = _required(recovery_contract, "RecoveryStartCommand")
    start = start_type(
        binding=replace(
            prepared.binding,
            confirmation_fingerprint="sha256:dispatch-confirmation",
            reason="consume prepared dispatch",
        ),
        started_at=NOW + timedelta(seconds=2),
        counts=prepared.counts,
        attempt_budget_ms=60_000,
        expected_epoch=prepared.epoch,
        confirmed_by_actor_id="agent-confirmer",
        confirmation_consumed_at=NOW + timedelta(seconds=2),
    )
    return _required(store, "enqueue_execution")(start)


@pytest.mark.parametrize("stage_name", ["PREPARATION", "RECOVERY"])
def test_committed_unnotified_dispatch_is_claimed_after_store_restart(
    tmp_path: Path,
    stage_name: str,
) -> None:
    database_path = tmp_path / f"restart-{stage_name.lower()}.sqlite3"
    store, engine = _open_store(database_path)
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        if stage_name == "PREPARATION":
            expected = _admit(store, "gdr_r5_restart_preparation")
        else:
            expected = _prepare_and_enqueue_execution(
                store,
                "gdr_r5_restart_recovery",
            )
    finally:
        engine.dispose()

    reopened, reopened_engine = _open_store(database_path)
    try:
        claim = _claim(
            reopened,
            stage=getattr(stages, stage_name),
            worker_id="worker-after-restart",
            at=NOW + timedelta(seconds=3),
        )
        assert claim is not None
        assert claim.run_id == expected.run_id
        assert claim.epoch == expected.epoch
        assert claim.attempt_id == expected.attempt_id
        assert claim.attempt_count == 1
    finally:
        reopened_engine.dispose()


def test_expired_claim_is_reclaimed_and_stale_token_cannot_acknowledge(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "expired-claim.sqlite3"
    first_store, first_engine = _open_store(database_path)
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        _admit(first_store, "gdr_r5_expired_claim")
        first = _claim(
            first_store,
            stage=stages.PREPARATION,
            worker_id="worker-before-crash",
            at=NOW,
        )
        assert first is not None
    finally:
        first_engine.dispose()

    second_store, second_engine = _open_store(database_path)
    try:
        reclaimed = _claim(
            second_store,
            stage=stages.PREPARATION,
            worker_id="worker-after-crash",
            at=NOW + timedelta(seconds=31),
        )
        assert reclaimed is not None
        assert reclaimed.dispatch_id == first.dispatch_id
        assert reclaimed.claim_token != first.claim_token
        assert reclaimed.attempt_count == 2

        conflict_type = _required(recovery_contract, "RecoveryDispatchClaimConflict")
        with pytest.raises(conflict_type) as stale:
            _required(second_store, "complete_dispatch")(
                dispatch_id=first.dispatch_id,
                claim_token=first.claim_token,
                completed_at=NOW + timedelta(seconds=32),
                result={"outcome": "stale-worker-must-not-publish"},
            )
        assert stale.value.code == "recovery_dispatch_claim_conflict"

        completed = _required(second_store, "complete_dispatch")(
            dispatch_id=reclaimed.dispatch_id,
            claim_token=reclaimed.claim_token,
            completed_at=NOW + timedelta(seconds=32),
            result={"outcome": "claimed"},
        )
        assert _value(completed.state) == "done"
    finally:
        second_engine.dispose()


def test_claimed_recovery_dispatch_rejects_tokenless_internal_settlement(
    tmp_path: Path,
) -> None:
    store, engine = _open_store(tmp_path / "tokenless-recovery-settlement.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        running = _prepare_and_enqueue_execution(
            store,
            "gdr_r5_tokenless_recovery",
        )
        claim = _claim(
            store,
            stage=stages.RECOVERY,
            worker_id="physical-worker",
            at=NOW + timedelta(seconds=3),
        )
        assert claim is not None
        conflict_type = _required(
            recovery_contract,
            "RecoveryDispatchClaimConflict",
        )
        result_type = _required(recovery_contract, "RecoveryWorkerResult")
        outcome_type = _required(recovery_contract, "RecoveryTerminalOutcome")
        with engine.begin() as connection, pytest.raises(conflict_type):
            dispatch = store._dispatch_row(  # noqa: SLF001
                connection,
                dispatch_id=claim.dispatch_id,
            )
            assert dispatch is not None
            current = store._require_latest(  # noqa: SLF001
                connection,
                running.run_id,
            )
            store._complete_recovery_in_transaction(  # noqa: SLF001
                connection,
                current=current,
                dispatch=dispatch,
                claim_token=None,
                completed_at=NOW + timedelta(seconds=4),
                active_elapsed_ms=current.active_elapsed_ms,
                result=result_type(
                    outcome=outcome_type.TIMEOUT,
                    reason_code="recovery_worker_claim_expired",
                    retryable=False,
                    counts=current.counts,
                ),
                require_claimed=False,
            )
    finally:
        engine.dispose()


def test_recovery_claim_restarts_same_epoch_and_stale_token_cannot_heartbeat_or_finish(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "recovery-claim-restart.sqlite3"
    first_store, first_engine = _open_store(database_path)
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        expected = _prepare_and_enqueue_execution(
            first_store,
            "gdr_r5_recovery_claim_restart",
        )
        first = _claim(
            first_store,
            stage=stages.RECOVERY,
            worker_id="physical-before-crash",
            at=NOW + timedelta(seconds=3),
        )
        assert first is not None
    finally:
        first_engine.dispose()

    reopened, reopened_engine = _open_store(database_path)
    try:
        reclaimed = _claim(
            reopened,
            stage=stages.RECOVERY,
            worker_id="physical-after-restart",
            at=NOW + timedelta(seconds=34),
        )
        assert reclaimed is not None
        assert reclaimed.dispatch_id == first.dispatch_id
        assert reclaimed.run_id == expected.run_id
        assert reclaimed.epoch == expected.epoch == 1
        assert reclaimed.attempt_id == expected.attempt_id
        assert reclaimed.claim_token != first.claim_token
        assert reclaimed.attempt_count == 2

        conflict_type = _required(
            recovery_contract,
            "RecoveryDispatchClaimConflict",
        )
        with pytest.raises(conflict_type):
            reopened.heartbeat_recovery(
                dispatch_id=first.dispatch_id,
                claim_token=first.claim_token,
                observed_at=NOW + timedelta(seconds=35),
                requested_expires_at=NOW + timedelta(seconds=45),
                active_elapsed_ms=1_000,
                counts=expected.counts,
            )

        current = reopened.get_status(run_id=expected.run_id)
        assert current is not None
        result_type = _required(recovery_contract, "RecoveryWorkerResult")
        outcome_type = _required(recovery_contract, "RecoveryTerminalOutcome")
        result = result_type(
            outcome=outcome_type.SUCCESS,
            reason_code="global_discovery_recovery_completed",
            retryable=False,
            counts=current.counts,
        )
        with pytest.raises(conflict_type):
            reopened.complete_recovery(
                dispatch_id=first.dispatch_id,
                claim_token=first.claim_token,
                expected_progress_seq=current.progress_seq,
                completed_at=NOW + timedelta(seconds=35),
                active_elapsed_ms=1_000,
                result=result,
            )

        terminal = reopened.complete_recovery(
            dispatch_id=reclaimed.dispatch_id,
            claim_token=reclaimed.claim_token,
            expected_progress_seq=current.progress_seq,
            completed_at=NOW + timedelta(seconds=35),
            active_elapsed_ms=1_000,
            result=result,
        )
        assert terminal.state.value == "success"
        with reopened_engine.connect() as connection:
            assert connection.scalar(
                select(func.count()).select_from(
                    models.GlobalDiscoveryRecoverySlot.__table__
                )
            ) == 0
            dispatch_state = connection.scalar(
                select(models.GlobalDiscoveryRecoveryDispatch.state).where(
                    models.GlobalDiscoveryRecoveryDispatch.dispatch_id
                    == reclaimed.dispatch_id
                )
            )
        assert dispatch_state == "done"
    finally:
        reopened_engine.dispose()


def test_deadline_reconciliation_claim_preserves_completed_journal_over_late_cancel(
    tmp_path: Path,
) -> None:
    store, engine = _open_store(tmp_path / "deadline-journal-truth.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        admitted = _prepare_and_enqueue_execution(
            store,
            "gdr_r5_deadline_journal_truth",
        )
        crashed = _claim(
            store,
            stage=stages.RECOVERY,
            worker_id="physical-before-sql-crash",
            at=NOW + timedelta(seconds=3),
        )
        assert crashed is not None
        cancelled = store.request_cancel(
            run_id=admitted.run_id,
            expected_epoch=admitted.epoch,
            requested_at=NOW + timedelta(seconds=40),
            requested_by_actor_id="operator-late-cancel",
            reason="cancel arrived after the physical journal may have committed",
        )
        assert cancelled.cancel_requested_at is not None

        reconciliation = _claim(
            store,
            stage=stages.RECOVERY,
            worker_id="physical-journal-reconciler",
            at=NOW + timedelta(seconds=63),
        )
        assert reconciliation is not None
        assert reconciliation.epoch == crashed.epoch == 1
        assert reconciliation.attempt_id == crashed.attempt_id
        assert reconciliation.claim_token != crashed.claim_token
        assert reconciliation.reconciliation_only is True
        assert reconciliation.claim_expires_at > cancelled.active_deadline_at

        current = store.get_status(run_id=admitted.run_id)
        assert current is not None
        physical_type = _required(recovery_contract, "RecoveryPhysicalTruth")
        result_type = _required(recovery_contract, "RecoveryWorkerResult")
        outcome_type = _required(recovery_contract, "RecoveryTerminalOutcome")
        terminal = store.complete_recovery(
            dispatch_id=reconciliation.dispatch_id,
            claim_token=reconciliation.claim_token,
            expected_progress_seq=current.progress_seq,
            completed_at=NOW + timedelta(seconds=64),
            active_elapsed_ms=current.attempt_budget_ms,
            result=result_type(
                outcome=outcome_type.SUCCESS,
                reason_code="global_discovery_recovery_completed",
                retryable=False,
                counts=current.counts,
                physical_truth=physical_type(
                    attempt_id=current.attempt_id,
                    journal_phase="completed",
                    pointer_replaced=True,
                    rollback_performed=False,
                    evidence_ref="journal://completed-after-crash",
                ),
            ),
        )
        assert terminal.state.value == "success"
        assert terminal.physical_truth is not None
        assert terminal.physical_truth.journal_phase == "completed"
        assert terminal.cancel_requested_at is not None
        assert terminal.active_elapsed_ms == terminal.attempt_budget_ms
    finally:
        engine.dispose()


def test_terminal_resume_create_only_handoffs_epoch_inputs_and_transfers_slot(
    tmp_path: Path,
) -> None:
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "artifacts"
    )
    provider = CommunityDurableRecoveryInputProvider(
        artifact_store=artifact_store
    )
    store, engine = _open_store(
        tmp_path / "resume-input-handoff.sqlite3",
        resume_input_handoff=provider.handoff_resume_inputs,
    )
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        epoch_one = _prepare_and_enqueue_execution(
            store,
            "gdr_r5_resume_input_handoff",
        )
        source_command = _required(recovery_contract, "RecoveryStartCommand")(
            binding=epoch_one.binding,
            started_at=epoch_one.started_at,
            counts=epoch_one.counts,
            attempt_budget_ms=epoch_one.attempt_budget_ms,
            expected_epoch=epoch_one.epoch,
            confirmed_by_actor_id=epoch_one.confirmed_by_actor_id,
            confirmation_consumed_at=epoch_one.confirmation_consumed_at,
        )
        boards = (
            GlobalDiscoveryBoardSeed(
                board_id="board-resume-handoff",
                board_name="Resume handoff",
                summary="same immutable physical inventory",
                summary_embedding=(0.1, 0.2),
                digests=(
                    GlobalDiscoveryDigestSeed(
                        original_node_id="decision-resume-handoff",
                        title="Resume",
                        summary="create-only epoch rebinding",
                        node_type="Decision",
                        graph_layer="canonical",
                        source_artifact_ref="artifact://resume-handoff",
                        embedding=(0.3, 0.4),
                    ),
                ),
                source_inventory_hash="sha256:resume-handoff-inventory",
            ),
        )
        source_inputs = GlobalDiscoveryRecoveryWorkerInputs(
            command=source_command,
            expected_live_sha256="a" * 64,
            boards=boards,
            terminal_counts=epoch_one.counts,
        )
        input_store = GlobalDiscoveryRecoveryWorkerInputStore(artifact_store)
        input_store.put(source_inputs)

        claim = _claim(
            store,
            stage=stages.RECOVERY,
            worker_id="failed-physical-attempt",
            at=NOW + timedelta(seconds=3),
        )
        assert claim is not None
        running = store.get_status(run_id=epoch_one.run_id)
        assert running is not None
        failed = store.complete_recovery(
            dispatch_id=claim.dispatch_id,
            claim_token=claim.claim_token,
            expected_progress_seq=running.progress_seq,
            completed_at=NOW + timedelta(seconds=4),
            active_elapsed_ms=1_000,
            result=_required(recovery_contract, "RecoveryWorkerResult")(
                outcome=_required(
                    recovery_contract,
                    "RecoveryTerminalOutcome",
                ).FAILED,
                reason_code="native_operation_failed",
                retryable=True,
                counts=running.counts,
            ),
        )
        assert failed.state.value == "failed"

        resumed, transitioned = store.admit_explicit_resume(
            run_id=failed.run_id,
            expected_epoch=failed.epoch,
            requested_at=NOW + timedelta(seconds=5),
            requested_by_actor_id="operator-resume-handoff",
            reason="retry exact inventory in a new physical epoch",
        )
        assert transitioned is True
        assert resumed.epoch == 2
        assert resumed.supersedes_epoch == 1
        loaded = input_store.load(resumed.run_id, epoch=2)
        assert loaded is not None
        assert loaded.command.expected_epoch == 2
        assert loaded.command.binding == resumed.binding
        assert loaded.expected_live_sha256 == source_inputs.expected_live_sha256
        assert loaded.boards == source_inputs.boards
        assert loaded.terminal_counts == source_inputs.terminal_counts
        assert provider(run_id=resumed.run_id, epoch=2).boards == boards

        replay, replay_transitioned = store.admit_explicit_resume(
            run_id=failed.run_id,
            expected_epoch=failed.epoch,
            requested_at=NOW + timedelta(seconds=5),
            requested_by_actor_id="operator-resume-handoff",
            reason="retry exact inventory in a new physical epoch",
        )
        assert replay == resumed
        assert replay_transitioned is False
    finally:
        engine.dispose()


def test_two_store_instances_claim_one_ready_dispatch_once(tmp_path: Path) -> None:
    database_path = tmp_path / "claim-race.sqlite3"
    seed, seed_engine = _open_store(database_path)
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        _admit(seed, "gdr_r5_claim_race")
    finally:
        seed_engine.dispose()

    first_store, first_engine = _open_store(database_path)
    second_store, second_engine = _open_store(database_path)
    ready = Barrier(3)

    def contend(store, worker_id: str):
        ready.wait(timeout=2)
        return _claim(
            store,
            stage=stages.PREPARATION,
            worker_id=worker_id,
            at=NOW,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(contend, first_store, "worker-a"),
                pool.submit(contend, second_store, "worker-b"),
            ]
            ready.wait(timeout=2)
            results = [future.result(timeout=5) for future in futures]
        claims = [result for result in results if result is not None]
        assert len(claims) == 1
        assert claims[0].attempt_count == 1
    finally:
        first_engine.dispose()
        second_engine.dispose()


def test_control_returns_after_durable_admission_and_preparation_is_off_thread(
    tmp_path: Path,
) -> None:
    store, engine = _open_store(tmp_path / "off-request-preparation.sqlite3")
    poller_type = _required(worker_module, "CommunityRecoveryPreparationPoller")
    dispatcher_type = _required(worker_module, "CommunityDurableRecoveryDispatcher")
    control_type = _required(recovery_contract, "RecoveryControlPlane")
    prepared_type = _required(recovery_contract, "RecoveryPreparedResult")
    caller_thread = get_ident()
    operation_threads: list[int] = []
    native_calls: list[str] = []
    entered = Event()
    release = Event()

    def preparation_operation(
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        actor_id: str,
        deadline_at_monotonic: float,
        fence_check,
        checkpoint,
    ):
        assert actor_id == "agent-off-request"
        assert deadline_at_monotonic > time.monotonic()
        operation_threads.append(get_ident())
        fence_check()  # before scan
        checkpoint(_counts(scanned=1))
        fence_check()  # after scan
        entered.set()
        assert release.wait(timeout=2)
        fence_check()  # before artifact publication
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check(manifest_ref=manifest_ref)  # immediately after publication
        fence_check()  # before return
        return prepared_type(
            manifest_ref=manifest_ref,
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=301),
            counts=_counts(scanned=2),
        )

    poller = poller_type(
        store=store,
        operation=preparation_operation,
        poll_interval_seconds=0.01,
        wall_clock=lambda: NOW,
    )
    dispatcher = dispatcher_type(store=store, preparation_poller=poller)
    control = control_type(store=store, dispatcher=dispatcher)
    poller.start()
    try:
        began = time.monotonic()
        accepted = control.prepare(
            _required(recovery_contract, "RecoveryPreparationCommand")(
                binding=_required(recovery_contract, "RecoveryRunBinding")(
                    run_id="gdr_r5_off_request",
                    actor_id="agent-off-request",
                ),
                admitted_at=NOW,
                counts=_counts(scanned=0),
                attempt_budget_ms=60_000,
            )
        )
        elapsed = time.monotonic() - began

        assert elapsed < 0.5
        assert accepted.phase.value in {"queued", "preparing"}
        assert entered.wait(timeout=2)
        assert operation_threads == [operation_threads[0]]
        assert operation_threads[0] != caller_thread
        assert native_calls == []

        release.set()
        deadline = time.monotonic() + 2
        while control.status(accepted.run_id).phase.value != "prepared":
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        release.set()
        poller.close(timeout_seconds=2)
        engine.dispose()


def test_delayed_claim_is_capped_by_admission_budget_without_reset(
    tmp_path: Path,
) -> None:
    store, engine = _open_store(tmp_path / "claim-budget-cap.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        status, created = store.admit_preparation(
            _required(recovery_contract, "RecoveryPreparationCommand")(
                binding=_required(recovery_contract, "RecoveryRunBinding")(
                    run_id="gdr_r5_claim_budget",
                    actor_id="agent-budget",
                ),
                admitted_at=NOW,
                counts=_counts(scanned=0),
                attempt_budget_ms=1_000,
            )
        )
        assert created is True
        claimed_at = NOW + timedelta(milliseconds=999)
        claim = store.claim_next_dispatch(
            stage=stages.PREPARATION,
            worker_id="late-preparer",
            claimed_at=claimed_at,
            claim_expires_at=claimed_at + timedelta(minutes=10),
        )
        assert claim is not None
        assert claim.claim_expires_at == status.active_deadline_at
        assert claim.claim_expires_at - claim.claimed_at == timedelta(milliseconds=1)

        assert (
            store.claim_next_dispatch(
                stage=stages.PREPARATION,
                worker_id="too-late-preparer",
                claimed_at=status.active_deadline_at,
                claim_expires_at=status.active_deadline_at
                + timedelta(minutes=10),
            )
            is None
        )
    finally:
        engine.dispose()


# --- A5R2: exactly-once crash charge on automatic same-epoch reclaim --------


def _a5r2_dispatch_row(engine, dispatch_id: str) -> dict:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(models.GlobalDiscoveryRecoveryDispatch.__table__).where(
                    models.GlobalDiscoveryRecoveryDispatch.dispatch_id
                    == dispatch_id
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


def _a5r2_transition_count(engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                select(func.count()).select_from(
                    models.GlobalDiscoveryRecoveryTransition.__table__
                )
            )
        )


def _a5r2_slot_count(engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                select(func.count()).select_from(
                    models.GlobalDiscoveryRecoverySlot.__table__
                )
            )
        )


def _ms(delta) -> int:
    return int(delta.total_seconds() * 1_000)


def test_a5r2_expired_reclaim_charges_exact_gap_and_rebases_liveness(
    tmp_path: Path,
) -> None:
    """T1: the crashed owner's window [heartbeat, old expiry] is charged
    exactly once and liveness rebases on the NEW claimed_at; the READY first
    claim itself never charges."""

    store, engine = _open_store(tmp_path / "a5r2-t1.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_t1")
        n0 = NOW + timedelta(seconds=3)
        expiry = n0 + timedelta(seconds=10)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=expiry,
        )
        assert first is not None
        assert first.reconciliation_only is False
        pre = store.get_status(run_id="gdr_a5r2_t1")
        assert pre is not None
        # READY first claim: mark_running only, zero charge.
        assert pre.state.value == "running"
        assert pre.phase.value == "cutover"
        assert pre.heartbeat_at == n0
        assert pre.active_elapsed_ms == 0
        assert pre.cumulative_active_ms == 0

        n1 = n0 + timedelta(seconds=25)
        reclaimed = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-new",
            claimed_at=n1,
            claim_expires_at=n1 + timedelta(seconds=30),
        )
        assert reclaimed is not None
        assert reclaimed.dispatch_id == first.dispatch_id
        assert reclaimed.attempt_count == first.attempt_count + 1
        assert reclaimed.claim_token != first.claim_token
        assert reclaimed.worker_id == "w-new"
        assert reclaimed.reconciliation_only is False

        post = store.get_status(run_id="gdr_a5r2_t1")
        assert post is not None
        gap_ms = _ms(expiry - pre.heartbeat_at)
        assert gap_ms == 10_000
        assert post.active_elapsed_ms == pre.active_elapsed_ms + gap_ms
        assert post.cumulative_active_ms == pre.cumulative_active_ms + gap_ms
        assert post.heartbeat_at == n1
        assert post.updated_at == n1
        assert post.progress_seq == pre.progress_seq + 1
        assert post.state.value == "running"
    finally:
        engine.dispose()


def test_a5r2_two_crashes_with_adoption_delay_never_charge_unowned_downtime(
    tmp_path: Path,
) -> None:
    """T5: with deliberate delays between expiry and adoption, only the lease
    windows are charged; unowned downtime (E1->N1, E2->N2) never appears."""

    store, engine = _open_store(tmp_path / "a5r2-t5.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_t5")
        n0 = NOW + timedelta(seconds=3)
        e1 = n0 + timedelta(seconds=8)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-crash-1",
            claimed_at=n0,
            claim_expires_at=e1,
        )
        assert first is not None

        n1 = e1 + timedelta(seconds=15)  # 15s unowned downtime
        e2 = n1 + timedelta(seconds=8)
        second = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-crash-2",
            claimed_at=n1,
            claim_expires_at=e2,
        )
        assert second is not None
        mid = store.get_status(run_id="gdr_a5r2_t5")
        assert mid is not None
        assert mid.active_elapsed_ms == 8_000  # E1-N0 only
        assert mid.heartbeat_at == n1

        # Second worker dies before its first heartbeat.
        n2 = e2 + timedelta(seconds=5)  # 5s more unowned downtime
        third = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-crash-3",
            claimed_at=n2,
            claim_expires_at=n2 + timedelta(seconds=8),
        )
        assert third is not None
        assert third.attempt_count == first.attempt_count + 2

        post = store.get_status(run_id="gdr_a5r2_t5")
        assert post is not None
        # (E1-N0) + (E2-N1) with N1-E1 and N2-E2 excluded.
        assert post.active_elapsed_ms == 8_000 + 8_000
        assert post.cumulative_active_ms == 16_000
        assert post.heartbeat_at == n2
        assert post.updated_at == n2
    finally:
        engine.dispose()


def test_a5r2_wall_deadline_reclaim_charges_to_deadline_and_reconciles(
    tmp_path: Path,
) -> None:
    """T4: past the wall deadline the charge is capped at the deadline and the
    claim is reconciliation_only; liveness still rebases on claimed_at."""

    store, engine = _open_store(tmp_path / "a5r2-t4.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_t4")
        n0 = NOW + timedelta(seconds=3)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=n0 + timedelta(seconds=120),
        )
        assert first is not None
        pre = store.get_status(run_id="gdr_a5r2_t4")
        assert pre is not None
        deadline = pre.active_deadline_at
        # Productive claim expiry is capped at the wall deadline.
        stored = _a5r2_dispatch_row(engine, first.dispatch_id)
        assert (
            worker_module._aware_datetime(stored["claim_expires_at"]) == deadline
        )

        n1 = deadline + timedelta(seconds=5)
        reclaimed = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-reconciler",
            claimed_at=n1,
            claim_expires_at=n1 + timedelta(seconds=30),
        )
        assert reclaimed is not None
        assert reclaimed.reconciliation_only is True
        post = store.get_status(run_id="gdr_a5r2_t4")
        assert post is not None
        expected_gap = _ms(deadline - pre.heartbeat_at)
        assert post.active_elapsed_ms == expected_gap
        assert post.active_elapsed_ms <= post.attempt_budget_ms
        assert post.heartbeat_at == n1
        assert post.updated_at == n1
        assert post.state.value == "running"
    finally:
        engine.dispose()


def test_a5r2_attempt_cap_hit_forces_reconciliation_only_and_repeat_crash_never_exceeds(
    tmp_path: Path,
) -> None:
    """T6b + repeated crash at the cap: a charged window that exhausts the
    effective active cap forces reconciliation_only BEFORE the wall deadline,
    and further reclaims at the cap add exactly zero."""

    store, engine = _open_store(tmp_path / "a5r2-t6b.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_t6b")
        n0 = NOW + timedelta(seconds=3)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=n0 + timedelta(seconds=20),
        )
        assert first is not None
        pre = store.get_status(run_id="gdr_a5r2_t6b")
        assert pre is not None
        # Real heartbeat drives active close to the attempt budget.
        h = n0 + timedelta(seconds=5)
        e = n0 + timedelta(seconds=15)
        store.heartbeat_recovery(
            dispatch_id=first.dispatch_id,
            claim_token=first.claim_token,
            observed_at=h,
            requested_expires_at=e,
            active_elapsed_ms=55_000,
            counts=pre.counts,
        )
        n1 = n0 + timedelta(seconds=18)  # e < n1, wall deadline still future
        reclaimed = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-capped",
            claimed_at=n1,
            claim_expires_at=n1 + timedelta(seconds=8),
        )
        assert reclaimed is not None
        assert reclaimed.reconciliation_only is True
        capped = store.get_status(run_id="gdr_a5r2_t6b")
        assert capped is not None
        assert n1 < capped.active_deadline_at
        # 55_000 + (e-h)=10_000 exceeds the 60_000 budget: charge caps exact.
        assert capped.active_elapsed_ms == capped.attempt_budget_ms == 60_000
        assert capped.cumulative_active_ms == 60_000
        assert capped.heartbeat_at == n1

        # The reconciliation claim itself crashes; a further reclaim at the
        # cap charges exactly zero and never exceeds attempt/cumulative caps.
        e3 = n1 + timedelta(seconds=8)
        n2 = e3 + timedelta(seconds=2)
        again = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-capped-2",
            claimed_at=n2,
            claim_expires_at=n2 + timedelta(seconds=8),
        )
        assert again is not None
        assert again.reconciliation_only is True
        stable = store.get_status(run_id="gdr_a5r2_t6b")
        assert stable is not None
        assert stable.active_elapsed_ms == 60_000
        assert stable.cumulative_active_ms == 60_000
        assert stable.heartbeat_at == n2
    finally:
        engine.dispose()


def test_a5r2_two_contenders_single_charge_and_stale_second_claim(
    tmp_path: Path,
) -> None:
    """T2/T3: two concurrent contenders for the SAME expired claim produce
    exactly one winner and exactly one charge; a later claim attempt sees the
    live claim and changes nothing."""

    store, engine = _open_store(tmp_path / "a5r2-t2.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_t2")
        n0 = NOW + timedelta(seconds=3)
        e1 = n0 + timedelta(seconds=10)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=e1,
        )
        assert first is not None
        n1 = n0 + timedelta(seconds=20)

        # Both contenders target the SAME single expired CLAIMED dispatch:
        # prove the precondition before racing (Barrier alignment alone is
        # not the proof).
        pre_race = _a5r2_dispatch_row(engine, first.dispatch_id)
        assert str(pre_race["state"]) == "claimed"
        assert worker_module._aware_datetime(pre_race["claim_expires_at"]) <= n1

        checkpoint_conflict = _required(
            recovery_contract, "RecoveryCheckpointConflict"
        )
        dispatch_conflict = _required(
            recovery_contract, "RecoveryDispatchClaimConflict"
        )
        allowed_loser_types = (checkpoint_conflict, dispatch_conflict)
        barrier = Barrier(2)

        def contend(worker_id: str):
            barrier.wait(timeout=10)
            try:
                return store.claim_next_dispatch(
                    stage=stages.RECOVERY,
                    worker_id=worker_id,
                    claimed_at=n1,
                    claim_expires_at=n1 + timedelta(seconds=30),
                )
            except allowed_loser_types as exc:
                # The ONLY acceptable loser signals under the linearized
                # contract.  Anything else (OperationalError, raw
                # "database is locked", ...) propagates and fails the
                # future.result below explicitly — never masked.
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(contend, "w-contender-a"),
                pool.submit(contend, "w-contender-b"),
            ]
            outcomes = [future.result(timeout=30) for future in futures]

        # Zero raw lock errors: every outcome is a claim, None, or one of the
        # two typed conflicts (enforced by construction above).
        for outcome in outcomes:
            assert (
                outcome is None
                or isinstance(outcome, allowed_loser_types)
                or hasattr(outcome, "claim_token")
            ), outcome
        claims = [
            outcome
            for outcome in outcomes
            if outcome is not None and hasattr(outcome, "claim_token")
        ]
        assert len(claims) == 1, outcomes
        winner = claims[0]
        assert winner.dispatch_id == first.dispatch_id
        assert winner.attempt_count == first.attempt_count + 1

        post = store.get_status(run_id="gdr_a5r2_t2")
        assert post is not None
        assert post.active_elapsed_ms == _ms(e1 - n0)  # exactly ONE window
        assert post.heartbeat_at == n1

        # T3: a later sequential claim sees the live claim and does nothing.
        third = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-late",
            claimed_at=n1 + timedelta(seconds=1),
            claim_expires_at=n1 + timedelta(seconds=31),
        )
        assert third is None
        unchanged = store.get_status(run_id="gdr_a5r2_t2")
        assert unchanged == post
    finally:
        engine.dispose()


def test_a5r2_forced_status_cas_conflict_rolls_back_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced settle-CAS failure raises typed conflict and leaves dispatch,
    status and transition log byte-equivalent (status CAS runs FIRST, so
    nothing else was written)."""

    store, engine = _open_store(tmp_path / "a5r2-cas.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    conflict_type = _required(recovery_contract, "RecoveryCheckpointConflict")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_cas")
        n0 = NOW + timedelta(seconds=3)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=n0 + timedelta(seconds=10),
        )
        assert first is not None
        pre_status = store.get_status(run_id="gdr_a5r2_cas")
        pre_dispatch = _a5r2_dispatch_row(engine, first.dispatch_id)
        pre_transitions = _a5r2_transition_count(engine)

        cas_calls: list[str] = []

        def failing_write_cas(connection, *, current, updated):
            cas_calls.append("status-cas")
            return False

        monkeypatch.setattr(store, "_write_cas", failing_write_cas)
        with pytest.raises(conflict_type):
            store.claim_next_dispatch(
                stage=stages.RECOVERY,
                worker_id="w-new",
                claimed_at=n0 + timedelta(seconds=25),
                claim_expires_at=n0 + timedelta(seconds=55),
            )
        assert cas_calls == ["status-cas"]
        monkeypatch.undo()

        assert store.get_status(run_id="gdr_a5r2_cas") == pre_status
        assert _a5r2_dispatch_row(engine, first.dispatch_id) == pre_dispatch
        assert _a5r2_transition_count(engine) == pre_transitions
    finally:
        engine.dispose()


def test_a5r2_dispatch_cas_loser_after_status_rolls_back_charge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural lock-order proof: the settle status CAS runs BEFORE the
    dispatch CAS (the hook poisons the dispatch row from INSIDE the status
    CAS and the dispatch CAS then loses), and the loser rolls back the
    charge, transition, token, worker, expiry and attempt_count entirely."""

    store, engine = _open_store(tmp_path / "a5r2-loser.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    conflict_type = _required(recovery_contract, "RecoveryDispatchClaimConflict")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_loser")
        n0 = NOW + timedelta(seconds=3)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=n0 + timedelta(seconds=10),
        )
        assert first is not None
        pre_status = store.get_status(run_id="gdr_a5r2_loser")
        pre_dispatch = _a5r2_dispatch_row(engine, first.dispatch_id)
        pre_transitions = _a5r2_transition_count(engine)

        real_write_cas = store._write_cas

        def sabotaging_write_cas(connection, *, current, updated):
            written = real_write_cas(
                connection, current=current, updated=updated
            )
            connection.execute(
                update(models.GlobalDiscoveryRecoveryDispatch.__table__)
                .where(
                    models.GlobalDiscoveryRecoveryDispatch.dispatch_id
                    == first.dispatch_id
                )
                .values(claim_token="gdrclaim_sabotage")
            )
            return written

        monkeypatch.setattr(store, "_write_cas", sabotaging_write_cas)
        with pytest.raises(conflict_type):
            store.claim_next_dispatch(
                stage=stages.RECOVERY,
                worker_id="w-new",
                claimed_at=n0 + timedelta(seconds=25),
                claim_expires_at=n0 + timedelta(seconds=55),
            )
        monkeypatch.undo()

        assert store.get_status(run_id="gdr_a5r2_loser") == pre_status
        assert _a5r2_dispatch_row(engine, first.dispatch_id) == pre_dispatch
        assert _a5r2_transition_count(engine) == pre_transitions
    finally:
        engine.dispose()


def test_a5r2_heartbeat_versus_reclaim_both_orders_single_winner(
    tmp_path: Path,
) -> None:
    """Old-owner heartbeat versus reclaim, both orders, Event-sequenced
    threads with bounded joins: exactly one persisted state each time, stale
    token heartbeat AND completion never touch the charged status."""

    stages = _required(worker_module, "RecoveryDispatchStage")
    conflict_type = _required(recovery_contract, "RecoveryDispatchClaimConflict")
    result_type = _required(recovery_contract, "RecoveryWorkerResult")
    outcome_type = _required(recovery_contract, "RecoveryTerminalOutcome")

    # Order A: heartbeat commits first; the reclaim then sees a live claim.
    store, engine = _open_store(tmp_path / "a5r2-race-a.sqlite3")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_race_a")
        n0 = NOW + timedelta(seconds=3)
        e = n0 + timedelta(seconds=10)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=e,
        )
        assert first is not None
        pre = store.get_status(run_id="gdr_a5r2_race_a")
        heartbeat_done = Event()
        outcomes: dict[str, object] = {}

        def old_owner_heartbeat():
            try:
                outcomes["heartbeat"] = store.heartbeat_recovery(
                    dispatch_id=first.dispatch_id,
                    claim_token=first.claim_token,
                    observed_at=e - timedelta(milliseconds=1),
                    requested_expires_at=e + timedelta(seconds=20),
                    active_elapsed_ms=1_000,
                    counts=pre.counts,
                )
            except Exception as exc:
                outcomes["heartbeat"] = exc
            finally:
                heartbeat_done.set()

        def reclaimer():
            assert heartbeat_done.wait(timeout=10)
            try:
                outcomes["reclaim"] = store.claim_next_dispatch(
                    stage=stages.RECOVERY,
                    worker_id="w-new",
                    claimed_at=e + timedelta(milliseconds=1),
                    claim_expires_at=e + timedelta(seconds=31),
                )
            except Exception as exc:
                outcomes["reclaim"] = exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(old_owner_heartbeat), pool.submit(reclaimer)]
            for future in futures:
                future.result(timeout=30)

        assert not isinstance(outcomes["heartbeat"], Exception)
        assert outcomes["reclaim"] is None  # renewed claim is live again
        renewed = store.get_status(run_id="gdr_a5r2_race_a")
        assert renewed is not None
        assert renewed.active_elapsed_ms == 1_000  # heartbeat truth, no charge
    finally:
        engine.dispose()

    # Order B: reclaim commits first; stale heartbeat and completion fail
    # typed and never touch the charged status.
    store, engine = _open_store(tmp_path / "a5r2-race-b.sqlite3")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_race_b")
        n0 = NOW + timedelta(seconds=3)
        e = n0 + timedelta(seconds=10)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=e,
        )
        assert first is not None
        pre = store.get_status(run_id="gdr_a5r2_race_b")
        reclaim_done = Event()
        outcomes = {}

        def reclaimer_first():
            try:
                outcomes["reclaim"] = store.claim_next_dispatch(
                    stage=stages.RECOVERY,
                    worker_id="w-new",
                    claimed_at=e + timedelta(milliseconds=1),
                    claim_expires_at=e + timedelta(seconds=31),
                )
            except Exception as exc:
                outcomes["reclaim"] = exc
            finally:
                reclaim_done.set()

        def stale_owner():
            assert reclaim_done.wait(timeout=10)
            try:
                outcomes["heartbeat"] = store.heartbeat_recovery(
                    dispatch_id=first.dispatch_id,
                    claim_token=first.claim_token,
                    observed_at=e - timedelta(milliseconds=1),
                    requested_expires_at=e + timedelta(seconds=20),
                    active_elapsed_ms=1_000,
                    counts=pre.counts,
                )
            except Exception as exc:
                outcomes["heartbeat"] = exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reclaimer_first), pool.submit(stale_owner)]
            for future in futures:
                future.result(timeout=30)

        assert not isinstance(outcomes["reclaim"], Exception)
        assert outcomes["reclaim"] is not None
        assert isinstance(outcomes["heartbeat"], conflict_type)

        charged = store.get_status(run_id="gdr_a5r2_race_b")
        assert charged is not None
        assert charged.active_elapsed_ms == _ms(e - n0)
        # Stale completion with the old token also fails without touching it.
        with pytest.raises(conflict_type):
            store.complete_recovery(
                dispatch_id=first.dispatch_id,
                claim_token=first.claim_token,
                expected_progress_seq=charged.progress_seq,
                completed_at=e + timedelta(seconds=2),
                active_elapsed_ms=charged.active_elapsed_ms,
                result=result_type(
                    outcome=outcome_type.SUCCESS,
                    reason_code="global_discovery_recovery_completed",
                    retryable=False,
                    counts=charged.counts,
                ),
            )
        assert store.get_status(run_id="gdr_a5r2_race_b") == charged
    finally:
        engine.dispose()


def test_a5r2_complete_dispatch_refuses_recovery_stage_with_zero_mutation(
    tmp_path: Path,
) -> None:
    """EMENDA: complete_dispatch only serves PREPARATION; a RECOVERY row with
    a LIVE token fails typed with dispatch/status/transition/slot untouched,
    while the PREPARATION path keeps working."""

    stages = _required(worker_module, "RecoveryDispatchStage")
    conflict_type = _required(recovery_contract, "RecoveryDispatchClaimConflict")

    # PREPARATION regression in its own store (the preparation singleton slot
    # stays held by this run, so the recovery half uses a fresh database).
    prep_store, prep_engine = _open_store(tmp_path / "a5r2-cd-prep.sqlite3")
    try:
        _admit(prep_store, "gdr_a5r2_cd_prep")
        prep_claim = _claim(
            prep_store,
            stage=stages.PREPARATION,
            worker_id="prep-worker",
            at=NOW,
        )
        assert prep_claim is not None
        receipt = prep_store.complete_dispatch(
            dispatch_id=prep_claim.dispatch_id,
            claim_token=prep_claim.claim_token,
            completed_at=NOW + timedelta(seconds=1),
            result={"outcome": "prepared"},
        )
        assert receipt is not None
        assert _value(receipt.state) == "done"
        assert receipt.dispatch_id == prep_claim.dispatch_id
    finally:
        prep_engine.dispose()

    store, engine = _open_store(tmp_path / "a5r2-cd.sqlite3")
    try:
        # RECOVERY refusal with zero mutation.
        _prepare_and_enqueue_execution(store, "gdr_a5r2_cd")
        n0 = NOW + timedelta(seconds=3)
        live = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-live",
            claimed_at=n0,
            claim_expires_at=n0 + timedelta(seconds=30),
        )
        assert live is not None
        pre_status = store.get_status(run_id="gdr_a5r2_cd")
        pre_dispatch = _a5r2_dispatch_row(engine, live.dispatch_id)
        pre_transitions = _a5r2_transition_count(engine)
        pre_slots = _a5r2_slot_count(engine)

        with pytest.raises(conflict_type):
            store.complete_dispatch(
                dispatch_id=live.dispatch_id,
                claim_token=live.claim_token,
                completed_at=n0 + timedelta(seconds=1),
                result={"outcome": "bogus"},
            )

        assert store.get_status(run_id="gdr_a5r2_cd") == pre_status
        assert _a5r2_dispatch_row(engine, live.dispatch_id) == pre_dispatch
        assert _a5r2_transition_count(engine) == pre_transitions
        assert _a5r2_slot_count(engine) == pre_slots
    finally:
        engine.dispose()


def test_a5r2_reclaim_and_complete_recovery_linearizations_no_hybrid(
    tmp_path: Path,
) -> None:
    """DETERMINISTIC LINEARIZATIONS (not an in-flight race — SQLite holds a
    database-level write lock, so overlapping write transactions cannot
    interleave without raw busy errors) of reclaim and complete_recovery in
    BOTH commit orders, with the COMPLETE end-state matrix asserted each
    time: either (terminal + dispatch DONE + slot released) or (RUNNING
    charged/rebased + new token/worker/attempt_count/expiry + slot still
    bound to the attempt + exactly one new transition) — never a hybrid."""

    stages = _required(worker_module, "RecoveryDispatchStage")
    conflict_type = _required(recovery_contract, "RecoveryDispatchClaimConflict")
    result_type = _required(recovery_contract, "RecoveryWorkerResult")
    outcome_type = _required(recovery_contract, "RecoveryTerminalOutcome")

    def build(path):
        store, engine = _open_store(path)
        _prepare_and_enqueue_execution(store, "gdr_a5r2_rc")
        n0 = NOW + timedelta(seconds=3)
        e = n0 + timedelta(seconds=10)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=e,
        )
        assert first is not None
        return store, engine, first, n0, e

    def completion(store, first, at, counts, progress_seq):
        return store.complete_recovery(
            dispatch_id=first.dispatch_id,
            claim_token=first.claim_token,
            expected_progress_seq=progress_seq,
            completed_at=at,
            active_elapsed_ms=5_000,
            result=result_type(
                outcome=outcome_type.SUCCESS,
                reason_code="global_discovery_recovery_completed",
                retryable=False,
                counts=counts,
            ),
        )

    # Linearization A: the old owner completes BEFORE any expiry handling;
    # the later reclaim finds no eligible candidate.  End state: terminal +
    # dispatch DONE + slot released, and nothing else.
    store, engine, first, n0, e = build(tmp_path / "a5r2-rc-a.sqlite3")
    try:
        current = store.get_status(run_id="gdr_a5r2_rc")
        terminal = completion(
            store,
            first,
            e - timedelta(seconds=1),
            current.counts,
            current.progress_seq,
        )
        assert terminal.state.value == "success"
        assert terminal.terminal_outcome is not None
        reclaim = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-new",
            claimed_at=e + timedelta(seconds=1),
            claim_expires_at=e + timedelta(seconds=31),
        )
        assert reclaim is None
        row = _a5r2_dispatch_row(engine, first.dispatch_id)
        assert str(row["state"]) == "done"
        assert str(row["claim_token"]) == first.claim_token
        assert int(row["attempt_count"]) == first.attempt_count
        assert _a5r2_slot_count(engine) == 0
        settled = store.get_status(run_id="gdr_a5r2_rc")
        assert settled == terminal  # reclaim attempt changed nothing
    finally:
        engine.dispose()

    # Linearization B: the reclaim commits first; the stale completion fails
    # typed.  End state: RUNNING charged/rebased + new token/worker/
    # attempt_count/expiry + slot still bound to the attempt + exactly one
    # new transition — and the failed completion changes none of it.
    store, engine, first, n0, e = build(tmp_path / "a5r2-rc-b.sqlite3")
    try:
        pre_transitions = _a5r2_transition_count(engine)
        n1 = e + timedelta(seconds=1)
        reclaim = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-new",
            claimed_at=n1,
            claim_expires_at=e + timedelta(seconds=31),
        )
        assert reclaim is not None
        assert _a5r2_transition_count(engine) == pre_transitions + 1
        charged = store.get_status(run_id="gdr_a5r2_rc")
        assert charged.state.value == "running"
        assert charged.active_elapsed_ms == _ms(e - n0)  # charged window
        assert charged.heartbeat_at == n1  # rebased proof of life
        row = _a5r2_dispatch_row(engine, first.dispatch_id)
        assert str(row["state"]) == "claimed"
        assert str(row["claim_token"]) == reclaim.claim_token != first.claim_token
        assert str(row["worker_id"]) == "w-new" != first.worker_id
        assert int(row["attempt_count"]) == first.attempt_count + 1
        assert worker_module._aware_datetime(row["claim_expires_at"]) == (
            e + timedelta(seconds=31)
        )
        with engine.connect() as connection:
            slot = (
                connection.execute(
                    select(models.GlobalDiscoveryRecoverySlot.__table__)
                )
                .mappings()
                .one()
            )
        assert str(slot["run_id"]) == "gdr_a5r2_rc"
        assert str(slot["attempt_id"]) == reclaim.attempt_id
        assert int(slot["epoch"]) == reclaim.epoch

        with pytest.raises(conflict_type):
            completion(
                store,
                first,
                e + timedelta(seconds=2),
                charged.counts,
                charged.progress_seq,
            )
        after = store.get_status(run_id="gdr_a5r2_rc")
        assert after == charged
        assert _a5r2_dispatch_row(engine, first.dispatch_id) == row
        assert _a5r2_transition_count(engine) == pre_transitions + 1
        assert _a5r2_slot_count(engine) == 1
    finally:
        engine.dispose()


def test_a5r2_reclaim_versus_concurrent_status_write_no_lost_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL, previously COMMITTED public status write (request_cancel on the
    running attempt) is never lost to a reclaim that read a stale snapshot:
    the stale settle CAS fails typed, the reclaim rolls back entirely, and
    the committed cancel remains.  The stale snapshot is injected through an
    explicit one-shot ``_latest`` seam: it deterministically reproduces a
    reclaimer whose read linearized BEFORE the committed writer, without
    manufacturing raw SQLite lock contention inside overlapping
    transactions."""

    store, engine = _open_store(tmp_path / "a5r2-lost.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    conflict_type = _required(recovery_contract, "RecoveryCheckpointConflict")
    try:
        _prepare_and_enqueue_execution(store, "gdr_a5r2_lost")
        n0 = NOW + timedelta(seconds=3)
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=n0 + timedelta(seconds=10),
        )
        assert first is not None
        stale_snapshot = store.get_status(run_id="gdr_a5r2_lost")
        assert stale_snapshot is not None
        assert stale_snapshot.cancel_requested_at is None

        # REAL committed concurrent write: public cancel on the running
        # attempt, fully committed before the reclaim runs.
        cancelled = store.request_cancel(
            run_id="gdr_a5r2_lost",
            expected_epoch=1,
            requested_at=n0 + timedelta(seconds=12),
            requested_by_actor_id="agent-canceller",
            reason="a5r2 no-lost-update proof",
        )
        assert cancelled.cancel_requested_at is not None
        assert cancelled.progress_seq > stale_snapshot.progress_seq
        post_cancel_status = store.get_status(run_id="gdr_a5r2_lost")
        post_cancel_dispatch = _a5r2_dispatch_row(engine, first.dispatch_id)
        post_cancel_transitions = _a5r2_transition_count(engine)

        real_latest = store._latest
        served: list[bool] = []

        def stale_latest_once(connection, run_id):
            if run_id == "gdr_a5r2_lost" and not served:
                served.append(True)
                return stale_snapshot
            return real_latest(connection, run_id)

        monkeypatch.setattr(store, "_latest", stale_latest_once)
        with pytest.raises(conflict_type):
            store.claim_next_dispatch(
                stage=stages.RECOVERY,
                worker_id="w-new",
                claimed_at=n0 + timedelta(seconds=25),
                claim_expires_at=n0 + timedelta(seconds=55),
            )
        monkeypatch.undo()
        assert served == [True]

        # The COMMITTED cancel survives untouched; the stale reclaim left no
        # charge, no token/worker/attempt_count change and no transition.
        preserved = store.get_status(run_id="gdr_a5r2_lost")
        assert preserved == post_cancel_status
        assert preserved.cancel_requested_at is not None
        assert _a5r2_dispatch_row(engine, first.dispatch_id) == (
            post_cancel_dispatch
        )
        assert _a5r2_transition_count(engine) == post_cancel_transitions
    finally:
        engine.dispose()


def test_a5r2_settle_expired_reclaim_core_contract() -> None:
    """Core-direct battery: T6 cumulative allowance binding at MAX, T7 gap
    crossing MAX before the wall deadline, zero-gap rebase, expiry boundary,
    and the fail-closed validations."""

    binding_type = _required(recovery_contract, "RecoveryRunBinding")
    status_type = _required(recovery_contract, "RecoveryRunStatus")
    state_type = _required(recovery_contract, "RecoveryRunState")
    phase_type = _required(recovery_contract, "RecoveryRunPhase")
    confirmation_type = _required(recovery_contract, "RecoveryConfirmationState")

    def running_status(**overrides):
        base = dict(
            binding=binding_type(
                run_id="gdr_a5r2_core",
                actor_id="agent-core",
                confirmation_fingerprint="sha256:a5r2-core-confirmation",
                manifest_ref="manifest://gdr_a5r2_core/attempt-1",
                preflight_hash="preflight-gdr_a5r2_core",
                reason="a5r2 core contract",
            ),
            epoch=1,
            state=state_type.RUNNING,
            progress_seq=7,
            phase=phase_type.CUTOVER,
            counts=_counts(scanned=2),
            heartbeat_at=NOW + timedelta(seconds=10),
            started_at=NOW,
            updated_at=NOW + timedelta(seconds=10),
            active_elapsed_ms=10_000,
            active_deadline_at=NOW + timedelta(seconds=600),
            cumulative_active_ms=10_000,
            attempt_budget_ms=60_000,
            reason_code="recovery_cutover_running",
            confirmation_state=confirmation_type.CONSUMED,
            prepared_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=301),
            snapshot_fingerprint="sha256:a5r2-core-snapshot",
            confirmed_by_actor_id="agent-confirmer",
            confirmation_consumed_at=NOW + timedelta(seconds=2),
        )
        base.update(overrides)
        return status_type(**base)

    heartbeat = NOW + timedelta(seconds=10)

    # T6: cumulative allowance (MAX - previous) binds below the attempt cap.
    high_previous = running_status(
        active_elapsed_ms=10_000,
        cumulative_active_ms=880_000,  # previous_attempts = 870_000
    )
    assert high_previous.effective_active_cap_ms == 30_000
    settled = high_previous.settle_expired_reclaim(
        old_claim_expires_at=heartbeat + timedelta(seconds=25),
        claimed_at=heartbeat + timedelta(seconds=40),
    )
    assert settled.active_elapsed_ms == 30_000  # capped by allowance
    assert settled.cumulative_active_ms == 900_000  # exactly MAX
    assert settled.heartbeat_at == heartbeat + timedelta(seconds=40)
    assert settled.updated_at == heartbeat + timedelta(seconds=40)
    assert settled.progress_seq == high_previous.progress_seq + 1

    # T7: a raw gap crossing MAX before the wall deadline caps identically.
    long_gap = running_status(
        active_elapsed_ms=10_000,
        cumulative_active_ms=880_000,
        active_deadline_at=NOW + timedelta(seconds=600),
    )
    crossed = long_gap.settle_expired_reclaim(
        old_claim_expires_at=heartbeat + timedelta(seconds=500),
        claimed_at=heartbeat + timedelta(seconds=520),
    )
    assert crossed.active_elapsed_ms == 30_000
    assert crossed.cumulative_active_ms == 900_000

    # Zero-gap reclaim still rebases liveness (mandate item 4).
    zero_gap = running_status()
    rebased = zero_gap.settle_expired_reclaim(
        old_claim_expires_at=heartbeat,  # boundary: expiry == heartbeat
        claimed_at=heartbeat + timedelta(seconds=5),
    )
    assert rebased.active_elapsed_ms == zero_gap.active_elapsed_ms
    assert rebased.cumulative_active_ms == zero_gap.cumulative_active_ms
    assert rebased.heartbeat_at == heartbeat + timedelta(seconds=5)
    assert rebased.progress_seq == zero_gap.progress_seq + 1

    # Boundary: expiry == claimed_at is an expired reclaim and is accepted.
    boundary = running_status()
    at_boundary = boundary.settle_expired_reclaim(
        old_claim_expires_at=heartbeat + timedelta(seconds=20),
        claimed_at=heartbeat + timedelta(seconds=20),
    )
    assert at_boundary.heartbeat_at == heartbeat + timedelta(seconds=20)

    # Future expiry is NOT an expired reclaim.
    with pytest.raises(ValueError):
        running_status().settle_expired_reclaim(
            old_claim_expires_at=heartbeat + timedelta(seconds=30),
            claimed_at=heartbeat + timedelta(seconds=20),
        )

    # PREPARING phase is refused even while RUNNING (coherently constructed
    # preparation status: no prepared/confirmation surface yet).
    preparing = status_type(
        binding=binding_type(run_id="gdr_a5r2_core", actor_id="agent-core"),
        epoch=1,
        state=state_type.RUNNING,
        progress_seq=3,
        phase=phase_type.PREPARING,
        counts=_counts(scanned=0),
        heartbeat_at=heartbeat,
        started_at=NOW,
        updated_at=heartbeat,
        active_elapsed_ms=1_000,
        active_deadline_at=NOW + timedelta(seconds=600),
        cumulative_active_ms=1_000,
        attempt_budget_ms=60_000,
        reason_code="recovery_preparing",
    )
    with pytest.raises(ValueError):
        preparing.settle_expired_reclaim(
            old_claim_expires_at=heartbeat + timedelta(seconds=1),
            claimed_at=heartbeat + timedelta(seconds=2),
        )

    # Backwards claimed_at is refused.
    with pytest.raises(ValueError):
        running_status().settle_expired_reclaim(
            old_claim_expires_at=heartbeat - timedelta(seconds=6),
            claimed_at=heartbeat - timedelta(seconds=5),
        )

    # A non-RUNNING state is refused even in the cutover-confirmed surface
    # (coherent PENDING+CONFIRMED status, i.e. enqueued but unclaimed).
    pending_confirmed = running_status(
        state=state_type.PENDING,
        phase=phase_type.CONFIRMED,
        reason_code="recovery_confirmed",
    )
    with pytest.raises(ValueError):
        pending_confirmed.settle_expired_reclaim(
            old_claim_expires_at=heartbeat + timedelta(seconds=1),
            claimed_at=heartbeat + timedelta(seconds=2),
        )


def test_a5r2_cumulative_allowance_binds_reclaim_at_store_level(
    tmp_path: Path,
) -> None:
    """T6 at the STORE level: cumulative time genuinely accrued during the
    PREPARATION attempt is carried into the confirmed recovery attempt, so
    allowance = MAX - previous binds BELOW the attempt budget.  A reclaim
    whose old-lease gap crosses the allowance charges EXACTLY to the cap,
    pins cumulative at MAX, is forced reconciliation_only BEFORE the wall
    deadline, rebases liveness, and a later crash at the cap adds zero."""

    max_cumulative = int(
        getattr(recovery_contract, "MAX_RECOVERY_CUMULATIVE_BUDGET_MS", 900_000)
    )
    store, engine = _open_store(tmp_path / "a5r2-t6-store.sqlite3")
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        run_id = "gdr_a5r2_t6_store"
        admitted = _admit(store, run_id, attempt_budget_ms=600_000)
        prep_claim = _claim(
            store,
            stage=stages.PREPARATION,
            worker_id="prep-worker",
            at=NOW,
        )
        assert prep_claim is not None
        store.mark_preparing(
            run_id=admitted.run_id,
            attempt_id=admitted.attempt_id,
            epoch=admitted.epoch,
            claim_token=prep_claim.claim_token,
            at=NOW,
        )
        # REAL preparation active time accrues through the public heartbeat.
        store.heartbeat_preparation(
            dispatch_id=prep_claim.dispatch_id,
            claim_token=prep_claim.claim_token,
            observed_at=NOW + timedelta(seconds=1),
            requested_expires_at=NOW + timedelta(seconds=90),
            active_elapsed_ms=500_000,
            counts=_counts(scanned=1),
        )
        prepared_type = _required(recovery_contract, "RecoveryPreparedResult")
        prepared = store.complete_preparation(
            run_id=admitted.run_id,
            attempt_id=admitted.attempt_id,
            epoch=admitted.epoch,
            claim_token=prep_claim.claim_token,
            completed_at=NOW + timedelta(seconds=2),
            result=prepared_type(
                manifest_ref=f"manifest://{run_id}/{admitted.attempt_id}",
                preflight_hash=f"preflight-{run_id}",
                snapshot_fingerprint=f"sha256:{run_id}",
                prepared_at=NOW + timedelta(seconds=2),
                expires_at=NOW + timedelta(seconds=302),
                counts=_counts(scanned=2),
            ),
        )
        start_type = _required(recovery_contract, "RecoveryStartCommand")
        start = start_type(
            binding=replace(
                prepared.binding,
                confirmation_fingerprint="sha256:t6-store-confirmation",
                reason="a5r2 cumulative allowance store proof",
            ),
            started_at=NOW + timedelta(seconds=3),
            counts=prepared.counts,
            attempt_budget_ms=600_000,
            expected_epoch=prepared.epoch,
            confirmed_by_actor_id="agent-confirmer",
            confirmation_consumed_at=NOW + timedelta(seconds=3),
        )
        store.enqueue_execution(start)
        confirmed = store.get_status(run_id=run_id)
        assert confirmed is not None
        assert confirmed.active_elapsed_ms == 0
        previous = confirmed.cumulative_active_ms
        # Preparation cumulative genuinely carried across mark_confirmed.
        assert previous == 500_000
        allowance = max_cumulative - previous
        assert allowance == 400_000
        assert allowance < confirmed.attempt_budget_ms == 600_000

        n0 = NOW + timedelta(seconds=4)
        gap_beyond_allowance = timedelta(milliseconds=allowance + 30_000)
        e = n0 + gap_beyond_allowance
        first = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-old",
            claimed_at=n0,
            claim_expires_at=e,
        )
        assert first is not None
        pre = store.get_status(run_id=run_id)
        assert pre is not None
        assert pre.heartbeat_at == n0
        # The lease expiry sits BEFORE the wall deadline: the coming cap-hit
        # is purely the cumulative allowance, not the deadline branch.
        assert e < pre.active_deadline_at

        n1 = e + timedelta(seconds=1)
        assert n1 < pre.active_deadline_at
        reclaimed = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-allowance-capped",
            claimed_at=n1,
            claim_expires_at=n1 + timedelta(seconds=30),
        )
        assert reclaimed is not None
        assert reclaimed.reconciliation_only is True
        capped = store.get_status(run_id=run_id)
        assert capped is not None
        # Charged EXACTLY to the cumulative allowance, never past MAX.
        assert capped.active_elapsed_ms == allowance
        assert capped.cumulative_active_ms == max_cumulative
        assert capped.heartbeat_at == n1
        assert capped.updated_at == n1
        assert n1 < capped.active_deadline_at

        # A later crash of the reconciliation claim adds exactly zero.
        e2 = n1 + timedelta(seconds=30)
        n2 = e2 + timedelta(seconds=2)
        again = store.claim_next_dispatch(
            stage=stages.RECOVERY,
            worker_id="w-allowance-capped-2",
            claimed_at=n2,
            claim_expires_at=n2 + timedelta(seconds=30),
        )
        assert again is not None
        assert again.reconciliation_only is True
        stable = store.get_status(run_id=run_id)
        assert stable is not None
        assert stable.active_elapsed_ms == allowance
        assert stable.cumulative_active_ms == max_cumulative
        assert stable.heartbeat_at == n2
    finally:
        engine.dispose()
