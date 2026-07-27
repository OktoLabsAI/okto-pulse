from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select

import okto_pulse.community.adapters.sqlalchemy_models as models
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    RecoveryDispatchStage,
    RecoveryPreparedRevokerUnavailable,
    SQLAlchemyRecoveryRunStore,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    RecoveryPreparationCommand,
    RecoveryControlPlane,
    RecoveryPreparedResult,
    RecoveryProgressCounts,
    RecoveryResumeRejected,
    RecoveryRunBinding,
    RecoveryStartCommand,
)


NOW = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)


class RecordingPreparedRevoker:
    """First-writer semantic revocation fake matching the artifact contract."""

    def __init__(self) -> None:
        self.calls = 0
        self.evidence: dict[tuple[str, int], dict[str, object]] = {}

    def revoke_prepared(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
        revoked_at: datetime,
        requested_by_actor_id: str,
        reason: str | None,
    ) -> object:
        self.calls += 1
        key = (run_id, epoch)
        semantic = {
            "run_id": run_id,
            "epoch": epoch,
            "manifest_ref": manifest_ref,
            "requested_by_actor_id": requested_by_actor_id,
            "reason": reason,
        }
        incumbent = self.evidence.get(key)
        if incumbent is None:
            incumbent = {**semantic, "revoked_at": revoked_at}
            self.evidence[key] = incumbent
        else:
            assert {
                name: incumbent[name] for name in semantic
            } == semantic
        return incumbent

    def is_prepared_revoked(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
    ) -> bool:
        evidence = self.evidence.get((run_id, epoch))
        return evidence is not None and evidence["manifest_ref"] == manifest_ref


class SlowPreparedRevoker(RecordingPreparedRevoker):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def revoke_prepared(self, **kwargs) -> object:
        self.entered.set()
        assert self.release.wait(timeout=6)
        return super().revoke_prepared(**kwargs)


def _store(path: Path, revoker: RecordingPreparedRevoker):
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    models.Base.metadata.create_all(
        engine,
        tables=[
            models.Board.__table__,
            models.GlobalDiscoveryRecoveryAttempt.__table__,
            models.GlobalDiscoveryRecoverySlot.__table__,
            models.GlobalDiscoveryRecoveryDispatch.__table__,
            models.GlobalDiscoveryRecoveryTransition.__table__,
        ],
    )
    with engine.begin() as connection:
        connection.execute(
            models.Board.__table__.insert().values(
                id="board-cancel-order",
                name="Cancel order",
                owner_id="agent-preparer",
                realm_id="local",
            )
        )
    return (
        SQLAlchemyRecoveryRunStore(
            engine=engine,
            prepared_revoker=revoker,
            wall_clock=lambda: NOW + timedelta(seconds=2),
        ),
        engine,
    )


def _prepared(store: SQLAlchemyRecoveryRunStore):
    run_id = "gdr_r5_cancel_order"
    counts = RecoveryProgressCounts(
        boards_total=1,
        sources_total=1,
    )
    admitted, created = store.admit_preparation(
        RecoveryPreparationCommand(
            binding=RecoveryRunBinding(run_id=run_id, actor_id="agent-preparer"),
            admitted_at=NOW,
            counts=counts,
            attempt_budget_ms=60_000,
        )
    )
    assert created is True
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="preparer",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=30),
    )
    assert claim is not None
    store.mark_preparing(
        run_id=run_id,
        attempt_id=admitted.attempt_id,
        epoch=admitted.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    prepared_counts = replace(
        counts,
        boards_scanned=1,
        sources_processed=1,
    )
    return store.complete_preparation(
        run_id=run_id,
        attempt_id=admitted.attempt_id,
        epoch=admitted.epoch,
        claim_token=claim.claim_token,
        completed_at=NOW + timedelta(seconds=1),
        result=RecoveryPreparedResult(
            manifest_ref=f"manifest://{run_id}/{admitted.attempt_id}",
            preflight_hash="preflight-cancel-order",
            snapshot_fingerprint="sha256:cancel-order",
            prepared_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=301),
            counts=prepared_counts,
        ),
    )


def _start(prepared) -> RecoveryStartCommand:
    return RecoveryStartCommand(
        binding=replace(
            prepared.binding,
            confirmation_fingerprint="sha256:prebuilt-confirmation",
            reason="prebuilt confirmation must fail closed",
        ),
        started_at=NOW + timedelta(seconds=2),
        counts=prepared.counts,
        attempt_budget_ms=60_000,
        expected_epoch=prepared.epoch,
        confirmed_by_actor_id="agent-confirmer",
        confirmation_consumed_at=NOW + timedelta(seconds=2),
    )


def test_revocation_precedes_terminal_cas_and_retry_is_fail_closed(
    tmp_path: Path,
) -> None:
    revoker = RecordingPreparedRevoker()
    store, engine = _store(tmp_path / "prepared-cancel.sqlite3", revoker)
    prepared = _prepared(store)
    original_write_cas = store._write_cas  # noqa: SLF001
    fail_once = True

    def crash_after_revocation(connection, *, current, updated):
        nonlocal fail_once
        if updated.state.value == "cancelled" and fail_once:
            fail_once = False
            return False
        return original_write_cas(
            connection,
            current=current,
            updated=updated,
        )

    store._write_cas = crash_after_revocation  # type: ignore[method-assign]  # noqa: SLF001
    try:
        intent = store.cancel_prepared(
            run_id=prepared.run_id,
            expected_epoch=prepared.epoch,
            requested_at=NOW + timedelta(seconds=3),
            requested_by_actor_id="agent-canceller",
            reason="operator cancelled prepared recovery",
        )
        assert intent.cancel_requested_at == NOW + timedelta(seconds=3)

        still_prepared = store.get_status(run_id=prepared.run_id)
        assert still_prepared is not None
        assert still_prepared.phase.value == "prepared"
        assert len(revoker.evidence) == 1

        with pytest.raises(RecoveryResumeRejected) as revoked:
            store.enqueue_execution(_start(prepared))
        assert revoked.value.code == "recovery_cancel_pending"

        cancelled = store.cancel_prepared(
            run_id=prepared.run_id,
            expected_epoch=prepared.epoch,
            requested_at=NOW + timedelta(seconds=4),
            requested_by_actor_id="agent-canceller",
            reason="operator cancelled prepared recovery",
        )
        assert cancelled.state.value == "cancelled"
        assert cancelled.cancel_requested_by_actor_id == "agent-canceller"
        assert revoker.calls == 1
        assert len(revoker.evidence) == 1

        with engine.connect() as connection:
            slot_count = connection.scalar(
                select(func.count()).select_from(
                    models.GlobalDiscoveryRecoverySlot.__table__
                )
            )
        assert slot_count == 0
    finally:
        engine.dispose()


def test_confirmation_fails_closed_when_revocation_evidence_port_is_missing(
    tmp_path: Path,
) -> None:
    revoker = RecordingPreparedRevoker()
    store, engine = _store(tmp_path / "missing-revoker.sqlite3", revoker)
    prepared = _prepared(store)
    without_revoker = SQLAlchemyRecoveryRunStore(
        engine=engine,
        wall_clock=lambda: NOW + timedelta(seconds=2),
    )
    try:
        with pytest.raises(RecoveryPreparedRevokerUnavailable):
            without_revoker.enqueue_execution(_start(prepared))
        unchanged = store.get_status(run_id=prepared.run_id)
        assert unchanged == prepared
        with engine.connect() as connection:
            recovery_dispatches = connection.scalar(
                select(func.count())
                .select_from(models.GlobalDiscoveryRecoveryDispatch.__table__)
                .where(
                    models.GlobalDiscoveryRecoveryDispatch.__table__.c.stage
                    == "recovery"
                )
            )
        assert recovery_dispatches == 0
    finally:
        engine.dispose()


def test_slow_revoker_never_delays_durable_cancel_or_confirmation_fence(
    tmp_path: Path,
) -> None:
    revoker = SlowPreparedRevoker()
    store, engine = _store(tmp_path / "slow-prepared-cancel.sqlite3", revoker)
    prepared = _prepared(store)
    control = RecoveryControlPlane(
        store=store,
        dispatcher=SimpleNamespace(dispatch=lambda **_kwargs: None),
    )
    try:
        started = time.monotonic()
        intent = control.cancel(
            run_id=prepared.run_id,
            expected_epoch=prepared.epoch,
            requested_at=NOW + timedelta(seconds=3),
            requested_by_actor_id="agent-canceller",
            reason="slow revoker cancellation",
        )
        elapsed = time.monotonic() - started

        assert elapsed < 1
        assert revoker.entered.wait(timeout=0.5)
        assert intent.state.value == "pending"
        assert intent.cancel_requested_by_actor_id == "agent-canceller"
        durable = store.get_status(run_id=prepared.run_id)
        assert durable is not None
        assert durable.cancel_requested_at == NOW + timedelta(seconds=3)

        with pytest.raises(RecoveryResumeRejected) as rejected:
            store.enqueue_execution(_start(prepared))
        assert rejected.value.code == "recovery_cancel_pending"

        revoker.release.set()
        deadline = time.monotonic() + 2
        terminal = store.get_status(run_id=prepared.run_id)
        while terminal is not None and not terminal.state.is_terminal:
            if time.monotonic() >= deadline:
                pytest.fail("prepared cancellation did not reconcile")
            time.sleep(0.01)
            terminal = store.get_status(run_id=prepared.run_id)
        assert terminal is not None
        assert terminal.state.value == "cancelled"
        assert len(revoker.evidence) == 1
    finally:
        revoker.release.set()
        engine.dispose()


def test_expired_prepared_incumbent_is_revoked_and_released_on_new_admission(
    tmp_path: Path,
) -> None:
    revoker = RecordingPreparedRevoker()
    store, engine = _store(tmp_path / "expired-prepared.sqlite3", revoker)
    prepared = _prepared(store)
    observed_at = NOW + timedelta(seconds=302)
    after_expiry = SQLAlchemyRecoveryRunStore(
        engine=engine,
        prepared_revoker=revoker,
        wall_clock=lambda: observed_at,
    )
    try:
        replacement, created = after_expiry.admit_preparation(
            RecoveryPreparationCommand(
                binding=RecoveryRunBinding(
                    run_id="gdr_r5_after_expired_prepared",
                    actor_id="agent-replacement",
                ),
                admitted_at=observed_at,
                counts=RecoveryProgressCounts(sources_total=1),
                attempt_budget_ms=60_000,
            )
        )
        assert created is True
        assert replacement.run_id == "gdr_r5_after_expired_prepared"
        expired = store.get_status(run_id=prepared.run_id)
        assert expired is not None
        assert expired.state.value == "cancelled"
        assert revoker.is_prepared_revoked(
            run_id=prepared.run_id,
            epoch=prepared.epoch,
            manifest_ref=prepared.binding.manifest_ref,
        )
        with engine.connect() as connection:
            slot = connection.execute(
                select(models.GlobalDiscoveryRecoverySlot.__table__)
            ).mappings().one()
        assert slot["run_id"] == replacement.run_id
    finally:
        engine.dispose()


def test_fresh_but_externally_revoked_prepared_incumbent_is_released(
    tmp_path: Path,
) -> None:
    revoker = RecordingPreparedRevoker()
    store, engine = _store(tmp_path / "revoked-prepared.sqlite3", revoker)
    prepared = _prepared(store)
    manifest_ref = prepared.binding.manifest_ref
    assert manifest_ref is not None
    revoker.revoke_prepared(
        run_id=prepared.run_id,
        epoch=prepared.epoch,
        manifest_ref=manifest_ref,
        revoked_at=NOW + timedelta(seconds=2),
        requested_by_actor_id="agent-revoker",
        reason="external revocation evidence",
    )

    try:
        replacement, created = store.admit_preparation(
            RecoveryPreparationCommand(
                binding=RecoveryRunBinding(
                    run_id="gdr_r5_after_revoked_prepared",
                    actor_id="agent-replacement",
                ),
                admitted_at=NOW + timedelta(seconds=3),
                counts=RecoveryProgressCounts(sources_total=1),
                attempt_budget_ms=60_000,
            )
        )

        assert created is True
        assert replacement.run_id == "gdr_r5_after_revoked_prepared"
        revoked = store.get_status(run_id=prepared.run_id)
        assert revoked is not None
        assert revoked.state.value == "cancelled"
        assert revoked.audit_reason == "prepared_manifest_revoked"
        with engine.connect() as connection:
            slot = connection.execute(
                select(models.GlobalDiscoveryRecoverySlot.__table__)
            ).mappings().one()
        assert slot["run_id"] == replacement.run_id
    finally:
        engine.dispose()


def test_generic_cancel_observes_prepared_state_and_revokes_atomically(
    tmp_path: Path,
) -> None:
    revoker = RecordingPreparedRevoker()
    store, engine = _store(tmp_path / "generic-prepared-cancel.sqlite3", revoker)
    prepared = _prepared(store)
    manifest_ref = prepared.binding.manifest_ref
    assert manifest_ref is not None

    try:
        cancelled = store.request_cancel(
            run_id=prepared.run_id,
            expected_epoch=prepared.epoch,
            requested_at=NOW + timedelta(seconds=2),
            requested_by_actor_id="agent-canceller",
            reason="control read preparing before preparation committed",
        )

        assert cancelled.state.value == "cancelled"
        assert cancelled.phase.value == "terminal"
        assert revoker.is_prepared_revoked(
            run_id=prepared.run_id,
            epoch=prepared.epoch,
            manifest_ref=manifest_ref,
        )
        with engine.connect() as connection:
            slot_count = connection.scalar(
                select(func.count()).select_from(
                    models.GlobalDiscoveryRecoverySlot.__table__
                )
            )
        assert slot_count == 0

        with pytest.raises(ValueError, match="requested_by_actor_id"):
            store.request_cancel(
                run_id=prepared.run_id,
                expected_epoch=prepared.epoch,
                requested_at=NOW + timedelta(seconds=3),
                requested_by_actor_id="x" * 256,
                reason="oversized actor must fail before persistence",
            )
    finally:
        engine.dispose()
