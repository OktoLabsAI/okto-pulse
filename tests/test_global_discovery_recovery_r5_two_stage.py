from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, insert, select, update

import okto_pulse.community.adapters.global_discovery_recovery_worker as worker_module
import okto_pulse.community.adapters.sqlalchemy_models as models
import okto_pulse.core.ports.global_discovery_recovery_control as recovery_contract


NOW = datetime(2026, 7, 17, 13, 0, tzinfo=timezone.utc)


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


def _models():
    return (
        models.GlobalDiscoveryRecoveryAttempt,
        _required(models, "GlobalDiscoveryRecoverySlot"),
        _required(models, "GlobalDiscoveryRecoveryDispatch"),
    )


def _store(database_path: Path):
    attempt, slot, dispatch = _models()
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
                        "id": f"board-two-stage-{index}",
                        "name": f"Two-stage {index}",
                        "owner_id": "agent-preparer",
                        "realm_id": "local",
                    }
                    for index in range(3)
                ],
            )
    store_type = _required(worker_module, "SQLAlchemyRecoveryRunStore")
    return (
        store_type(
            engine=engine,
            prepared_revoker=_PreparedRevoker(),
            wall_clock=lambda: NOW + timedelta(seconds=2),
        ),
        engine,
    )


def _counts(*, scanned: int):
    counts_type = _required(recovery_contract, "RecoveryProgressCounts")
    return counts_type(
        sources_total=3,
        sources_processed=scanned,
        boards_total=3,
        boards_scanned=scanned,
    )


def _preparation_command(run_id: str):
    command_type = _required(recovery_contract, "RecoveryPreparationCommand")
    binding_type = _required(recovery_contract, "RecoveryRunBinding")
    return command_type(
        binding=binding_type(
            run_id=run_id,
            actor_id="agent-preparer",
        ),
        admitted_at=NOW,
        counts=_counts(scanned=0),
        attempt_budget_ms=60_000,
    )


def _admit(store, run_id: str):
    status, created = _required(store, "admit_preparation")(
        _preparation_command(run_id)
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


def _prepare(store, run_id: str):
    stages = _required(worker_module, "RecoveryDispatchStage")
    status = _admit(store, run_id)
    claim = _claim(
        store,
        stage=stages.PREPARATION,
        worker_id="preparer-1",
        at=NOW,
    )
    assert claim is not None
    assert claim.run_id == run_id
    assert claim.attempt_id == status.attempt_id

    preparing = _required(store, "mark_preparing")(
        run_id=run_id,
        attempt_id=status.attempt_id,
        epoch=status.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    prepared_type = _required(recovery_contract, "RecoveryPreparedResult")
    prepared_result = prepared_type(
        manifest_ref=f"manifest://{run_id}/{status.attempt_id}",
        preflight_hash=f"preflight-{run_id}",
        snapshot_fingerprint=f"sha256:{run_id}",
        prepared_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=301),
        counts=_counts(scanned=3),
    )
    prepared = _required(store, "complete_preparation")(
        run_id=run_id,
        attempt_id=status.attempt_id,
        epoch=status.epoch,
        claim_token=claim.claim_token,
        completed_at=NOW + timedelta(seconds=1),
        result=prepared_result,
    )
    return status, claim, preparing, prepared


def _start_command(prepared):
    command_type = _required(recovery_contract, "RecoveryStartCommand")
    binding = replace(
        prepared.binding,
        confirmation_fingerprint="sha256:consumed-confirmation",
        manifest_ref=prepared.binding.manifest_ref,
        preflight_hash=prepared.binding.preflight_hash,
        reason="operator confirmed prepared recovery",
    )
    return command_type(
        binding=binding,
        started_at=NOW + timedelta(seconds=2),
        counts=prepared.counts,
        attempt_budget_ms=60_000,
        expected_epoch=prepared.epoch,
        confirmed_by_actor_id="agent-confirmer",
        confirmation_consumed_at=NOW + timedelta(seconds=2),
    )


def test_two_stage_uses_only_the_frozen_legal_state_phase_pairs(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path / "legal-pairs.sqlite3")
    try:
        queued, _claim_row, preparing, prepared = _prepare(
            store,
            "gdr_r5_legal_pairs",
        )

        assert (_value(queued.state), _value(queued.phase)) == (
            "pending",
            "queued",
        )
        assert (_value(preparing.state), _value(preparing.phase)) == (
            "running",
            "preparing",
        )
        assert (_value(prepared.state), _value(prepared.phase)) == (
            "pending",
            "prepared",
        )
        assert prepared.counts.boards_total == 3
        assert prepared.counts.boards_scanned == 3
        assert prepared.expires_at - prepared.prepared_at == timedelta(seconds=300)
        assert _value(prepared.confirmation_state) == "prepared"
    finally:
        engine.dispose()


def test_prepared_attempt_is_non_adoptable_even_after_store_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "prepared-non-adoption.sqlite3"
    store, engine = _store(database_path)
    try:
        _queued, _claim_row, _preparing, prepared = _prepare(
            store,
            "gdr_r5_non_adoptable",
        )
    finally:
        engine.dispose()

    reopened, reopened_engine = _store(database_path)
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        assert (
            _claim(
                reopened,
                stage=stages.RECOVERY,
                worker_id="recovery-should-not-adopt",
                at=NOW + timedelta(seconds=2),
            )
            is None
        )
        persisted = reopened.get_status(run_id=prepared.run_id)
        assert persisted is not None
        assert (_value(persisted.state), _value(persisted.phase)) == (
            "pending",
            "prepared",
        )
    finally:
        reopened_engine.dispose()


def test_confirmation_enqueues_exactly_one_recovery_dispatch_without_rescan(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path / "confirmed-dispatch.sqlite3")
    _attempt, _slot, dispatch = _models()
    stages = _required(worker_module, "RecoveryDispatchStage")
    try:
        _queued, _claim_row, _preparing, prepared = _prepare(
            store,
            "gdr_r5_confirmed",
        )
        before_counts = prepared.counts
        confirmed = _required(store, "enqueue_execution")(
            _start_command(prepared)
        )

        assert (_value(confirmed.state), _value(confirmed.phase)) == (
            "pending",
            "confirmed",
        )
        assert _value(confirmed.confirmation_state) == "consumed"
        assert confirmed.confirmed_by_actor_id == "agent-confirmer"
        assert confirmed.counts == before_counts

        replay = _required(store, "enqueue_execution")(_start_command(prepared))
        assert replay == confirmed
        with engine.connect() as connection:
            rows = connection.execute(
                select(dispatch.__table__).where(
                    dispatch.__table__.c.stage == "recovery"
                )
            ).mappings().all()
        assert len(rows) == 1

        claim = _claim(
            store,
            stage=stages.RECOVERY,
            worker_id="native-1",
            at=NOW + timedelta(seconds=3),
        )
        assert claim is not None
        running = store.get_status(run_id=prepared.run_id)
        assert running is not None
        assert (_value(running.state), _value(running.phase)) == (
            "running",
            "cutover",
        )
    finally:
        engine.dispose()


def test_storage_sentinel_strings_cannot_make_prepared_work_adoptable(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path / "sentinel-not-authority.sqlite3")
    attempt, _slot, dispatch = _models()
    stages = _required(worker_module, "RecoveryDispatchStage")
    states = _required(worker_module, "RecoveryDispatchState")
    try:
        _queued, _claim_row, _preparing, prepared = _prepare(
            store,
            "gdr_r5_sentinel_authority",
        )
        with engine.begin() as connection:
            connection.execute(
                update(attempt.__table__)
                .where(attempt.__table__.c.run_id == prepared.run_id)
                .values(confirmation_fingerprint="sha256:looks-real-but-unconsumed")
            )
            connection.execute(
                update(dispatch.__table__)
                .where(dispatch.__table__.c.run_id == prepared.run_id)
                .values(
                    stage=_value(stages.RECOVERY),
                    state=_value(states.READY),
                    claim_token=None,
                    worker_id=None,
                    claimed_at=None,
                    claim_expires_at=None,
                    available_at=NOW + timedelta(seconds=2),
                    completed_at=None,
                )
            )

        assert (
            _claim(
                store,
                stage=stages.RECOVERY,
                worker_id="native-must-refuse",
                at=NOW + timedelta(seconds=2),
            )
            is None
        )
        persisted = store.get_status(run_id=prepared.run_id)
        assert persisted is not None
        assert _value(persisted.confirmation_state) == "prepared"
        assert _value(persisted.phase) == "prepared"
    finally:
        engine.dispose()
