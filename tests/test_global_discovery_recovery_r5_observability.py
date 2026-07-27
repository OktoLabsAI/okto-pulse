from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, insert, inspect, select

import okto_pulse.community.adapters.global_discovery_recovery_worker as worker_module
import okto_pulse.community.adapters.sqlalchemy_models as models
import okto_pulse.core.ports.global_discovery_recovery_control as recovery_contract


NOW = datetime(2026, 7, 17, 17, 0, tzinfo=timezone.utc)


def _required(owner: object, name: str):
    value = getattr(owner, name, None)
    assert value is not None, f"R5 contract is missing {name}"
    return value


def _transition_identity(event) -> tuple[object, ...]:
    return (
        event.run_id,
        event.attempt_id,
        event.epoch,
        event.progress_seq,
        event.operation,
        event.outcome,
        event.phase.value,
        event.reason_code,
    )


def _model_types():
    return (
        models.GlobalDiscoveryRecoveryAttempt,
        _required(models, "GlobalDiscoveryRecoverySlot"),
        _required(models, "GlobalDiscoveryRecoveryDispatch"),
    )


def _store(database_path: Path):
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
        connection.execute(
            insert(models.Board.__table__).values(
                id="board-observability",
                name="Observability",
                owner_id="agent-observer",
                realm_id="local",
            )
        )
    store_type = _required(worker_module, "SQLAlchemyRecoveryRunStore")
    store = store_type(engine=engine, wall_clock=lambda: NOW)
    return store, engine


def _admit(store, run_id: str):
    binding_type = _required(recovery_contract, "RecoveryRunBinding")
    counts_type = _required(recovery_contract, "RecoveryProgressCounts")
    command_type = _required(recovery_contract, "RecoveryPreparationCommand")
    status, created = store.admit_preparation(
        command_type(
            binding=binding_type(run_id=run_id, actor_id="agent-observer"),
            admitted_at=NOW,
            counts=counts_type(
                boards_total=1,
                boards_scanned=0,
                sources_total=1,
                sources_processed=0,
            ),
            attempt_budget_ms=60_000,
        )
    )
    assert created is True
    return status


def _terminal_event(status):
    event_type = _required(recovery_contract, "RecoveryTransitionEvent")
    phases = _required(recovery_contract, "RecoveryRunPhase")
    states = _required(recovery_contract, "RecoveryRunState")
    return event_type(
        operation="recovery_terminal",
        outcome="success",
        phase=phases.TERMINAL,
        reason_code="global_discovery_recovery_completed",
        state=states.SUCCESS,
        run_id=status.run_id,
        attempt_id=status.attempt_id,
        epoch=status.epoch,
        progress_seq=status.progress_seq + 1,
    )


def test_transition_identity_is_exact_and_metric_labels_are_bounded() -> None:
    binding_type = _required(recovery_contract, "RecoveryRunBinding")
    counts_type = _required(recovery_contract, "RecoveryProgressCounts")
    command_type = _required(recovery_contract, "RecoveryPreparationCommand")
    status_type = _required(recovery_contract, "RecoveryRunStatus")
    command = command_type(
        binding=binding_type(
            run_id="gdr_high_cardinality_run",
            actor_id="agent-high-cardinality",
        ),
        admitted_at=NOW,
        counts=counts_type(boards_total=1, sources_total=1),
        attempt_budget_ms=60_000,
    )
    status = status_type.initial_preparation(command)
    event = _terminal_event(status)

    assert _transition_identity(event) == (
        "gdr_high_cardinality_run",
        "gdr_high_cardinality_run/attempt-1",
        1,
        1,
        "recovery_terminal",
        "success",
        "terminal",
        "global_discovery_recovery_completed",
    )
    assert set(event.metric_labels) == {
        "operation",
        "outcome",
        "phase",
        "reason_code",
    }
    assert event.metric_labels == {
        "operation": "recovery_terminal",
        "outcome": "success",
        "phase": "terminal",
        "reason_code": "global_discovery_recovery_completed",
    }
    rendered_labels = " ".join(event.metric_labels.values())
    assert event.run_id not in rendered_labels
    assert event.attempt_id not in rendered_labels
    assert command.binding.actor_id not in rendered_labels


def test_unknown_transition_values_collapse_to_bounded_metric_buckets() -> None:
    binding_type = _required(recovery_contract, "RecoveryRunBinding")
    counts_type = _required(recovery_contract, "RecoveryProgressCounts")
    command_type = _required(recovery_contract, "RecoveryPreparationCommand")
    status_type = _required(recovery_contract, "RecoveryRunStatus")
    event_type = _required(recovery_contract, "RecoveryTransitionEvent")
    phases = _required(recovery_contract, "RecoveryRunPhase")
    states = _required(recovery_contract, "RecoveryRunState")
    command = command_type(
        binding=binding_type(
            run_id="gdr_metric_cardinality_probe",
            actor_id="agent-metric-cardinality-probe",
        ),
        admitted_at=NOW,
        counts=counts_type(boards_total=1, sources_total=1),
        attempt_budget_ms=60_000,
    )
    status = status_type.initial_preparation(command)
    free_form_reason = "operator supplied reason with unbounded values 123456"
    event = event_type(
        operation="dynamic-operation-for-one-run",
        outcome="dynamic-outcome-for-one-run",
        phase=phases.TERMINAL,
        reason_code=free_form_reason,
        state=states.FAILED,
        run_id=status.run_id,
        attempt_id=status.attempt_id,
        epoch=status.epoch,
        progress_seq=status.progress_seq + 1,
    )

    assert event.reason_code == free_form_reason
    assert event.metric_labels == {
        "operation": "other",
        "outcome": "other",
        "phase": "terminal",
        "reason_code": "other",
    }
    rendered_labels = " ".join(event.metric_labels.values())
    assert free_form_reason not in rendered_labels
    assert event.run_id not in rendered_labels
    assert event.attempt_id not in rendered_labels
    assert command.binding.actor_id not in rendered_labels


def test_preflight_request_transition_is_durable_and_bounded(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path / "durable-request-audit.sqlite3")
    _attempt, _slot, dispatch = _model_types()
    try:
        status = _admit(store, "gdr_r5_durable_request_audit")
        with engine.connect() as connection:
            row = connection.execute(
                select(dispatch.__table__).where(
                    dispatch.__table__.c.run_id == status.run_id
                )
            ).mappings().one()

        assert row["transition_event_id"] == (
            f"preflight-audit:{row['dispatch_id']}"
        )
        assert row["transition_observed_at"] is not None
        assert row["transition_payload"]["preflight_request_count"] == 1
        assert row["transition_payload"]["preflight_requesters"] == [
            {
                "actor_id": "agent-observer",
                "first_requested_at": NOW.isoformat(),
                "last_requested_at": NOW.isoformat(),
                "request_count": 1,
                "replay_count": 0,
            }
        ]
    finally:
        engine.dispose()


def test_requester_audit_survives_reopen_and_counts_replay_once(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path / "request-audit-reopen.sqlite3")
    try:
        status = _admit(store, "gdr_r5_request_audit_reopen")
        reopened = _required(worker_module, "SQLAlchemyRecoveryRunStore")(
            engine=engine,
            wall_clock=lambda: NOW + timedelta(seconds=1),
        )
        binding_type = _required(recovery_contract, "RecoveryRunBinding")
        counts_type = _required(recovery_contract, "RecoveryProgressCounts")
        command_type = _required(recovery_contract, "RecoveryPreparationCommand")
        replayed, created = reopened.admit_preparation(
            command_type(
                binding=binding_type(
                    run_id="gdr_r5_request_audit_replay",
                    actor_id="agent-replay",
                ),
                admitted_at=NOW,
                counts=counts_type(boards_total=1, sources_total=1),
                attempt_budget_ms=60_000,
            )
        )

        assert created is False
        assert replayed.run_id == status.run_id
        audit = reopened.get_requester_audit(run_id=status.run_id)
        assert audit is not None
        assert audit.actor_ids == ("agent-observer", "agent-replay")
        assert audit.request_count == 2
        assert audit.replay_count == 1
    finally:
        engine.dispose()


def test_status_reads_do_not_mutate_durable_transition_audit(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path / "status-read.sqlite3")
    _attempt, _slot, dispatch = _model_types()
    try:
        status = _admit(store, "gdr_r5_status_read")
        with engine.connect() as connection:
            transition_before = connection.execute(
                select(
                    dispatch.__table__.c.transition_event_id,
                    dispatch.__table__.c.transition_observed_at,
                    dispatch.__table__.c.transition_payload,
                )
            ).one()

        before = store.get_status(run_id=status.run_id)
        after = store.get_status(run_id=status.run_id)

        assert before == after == status
        with engine.connect() as connection:
            transition_after = connection.execute(
                select(
                    dispatch.__table__.c.transition_event_id,
                    dispatch.__table__.c.transition_observed_at,
                    dispatch.__table__.c.transition_payload,
                )
            ).one()
        assert transition_after == transition_before
    finally:
        engine.dispose()


def test_transition_ledger_schema_has_durable_identity_and_bounded_labels(
    tmp_path: Path,
) -> None:
    _store_instance, engine = _store(tmp_path / "transition-schema.sqlite3")
    table = models.GlobalDiscoveryRecoveryTransition.__table__
    try:
        inspector = inspect(engine)
        assert inspector.has_table(table.name)
        assert {column["name"] for column in inspector.get_columns(table.name)} == {
            "event_id",
            "run_id",
            "attempt_id",
            "epoch",
            "progress_seq",
            "operation",
            "outcome",
            "phase",
            "reason_code",
            "state",
            "metric_labels",
            "observed_at",
        }
        assert {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table.name)
        } == {("run_id", "attempt_id", "epoch", "progress_seq")}
        assert {
            tuple(index["column_names"])
            for index in inspector.get_indexes(table.name)
        } == {("operation", "outcome", "phase", "reason_code")}
    finally:
        engine.dispose()


def test_admission_replay_cancel_and_reopen_emit_each_transition_once(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path / "transition-exactly-once.sqlite3")
    transition = models.GlobalDiscoveryRecoveryTransition.__table__
    try:
        admitted = _admit(store, "gdr_r5_transition_exactly_once")
        with engine.connect() as connection:
            initial_rows = connection.execute(
                select(transition).order_by(transition.c.progress_seq)
            ).mappings().all()
        assert len(initial_rows) == 1
        assert initial_rows[0]["progress_seq"] == admitted.progress_seq == 0
        assert initial_rows[0]["metric_labels"] == {
            "operation": "preflight",
            "outcome": "queued",
            "phase": "queued",
            "reason_code": "recovery_preparation_queued",
        }

        binding_type = _required(recovery_contract, "RecoveryRunBinding")
        counts_type = _required(recovery_contract, "RecoveryProgressCounts")
        command_type = _required(recovery_contract, "RecoveryPreparationCommand")
        replayed, created = store.admit_preparation(
            command_type(
                binding=binding_type(
                    run_id="ignored-by-singleton-replay",
                    actor_id="agent-replay",
                ),
                admitted_at=NOW + timedelta(seconds=1),
                counts=counts_type(boards_total=1, sources_total=1),
                attempt_budget_ms=60_000,
            )
        )
        assert created is False
        assert replayed.run_id == admitted.run_id
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(transition)) == 1

        cancelled = store.request_cancel(
            run_id=admitted.run_id,
            expected_epoch=admitted.epoch,
            requested_at=NOW + timedelta(seconds=2),
            requested_by_actor_id="agent-canceller",
            reason="operator requested stop with high-cardinality detail",
        )
        assert cancelled.state.value == "cancelled"
        repeated = store.request_cancel(
            run_id=admitted.run_id,
            expected_epoch=admitted.epoch,
            requested_at=NOW + timedelta(seconds=3),
            requested_by_actor_id="agent-canceller",
            reason="operator requested stop with high-cardinality detail",
        )
        assert repeated == cancelled

        reopened = _required(worker_module, "SQLAlchemyRecoveryRunStore")(
            engine=engine,
            wall_clock=lambda: NOW + timedelta(seconds=4),
        )
        assert reopened.get_status(run_id=admitted.run_id) == cancelled
        with engine.connect() as connection:
            rows = connection.execute(
                select(transition).order_by(transition.c.progress_seq)
            ).mappings().all()
        # Two-stage settlement durably records T1 intent before T2 records the
        # terminal transition; each progress identity must still appear once.
        assert [row["progress_seq"] for row in rows] == [
            0,
            cancelled.progress_seq - 1,
            cancelled.progress_seq,
        ]
        terminal = rows[-1]
        assert terminal["operation"] == "recovery_terminal"
        assert terminal["outcome"] == "cancelled"
        assert terminal["reason_code"] == "global_discovery_recovery_preparation_cancelled"
        assert set(terminal["metric_labels"]) == {
            "operation",
            "outcome",
            "phase",
            "reason_code",
        }
        rendered_labels = " ".join(terminal["metric_labels"].values())
        for high_cardinality_value in (
            admitted.run_id,
            admitted.attempt_id,
            "agent-canceller",
            "operator requested stop with high-cardinality detail",
        ):
            assert high_cardinality_value not in rendered_labels

        observer_type = _required(
            worker_module,
            "SQLAlchemyRecoveryTransitionObserver",
        )
        metrics = observer_type(engine=engine).metric_snapshot(run_id=admitted.run_id)
        assert sum(metrics.values()) == 3
        assert metrics[
            "dispatch:running:preparing:recovery_preparing"
        ] == 1
        assert metrics[
            "recovery_terminal:cancelled:terminal:"
            "global_discovery_recovery_preparation_cancelled"
        ] == 1
    finally:
        engine.dispose()


def test_failed_stale_cas_cannot_emit_a_transition(tmp_path: Path) -> None:
    store, engine = _store(tmp_path / "transition-failed-cas.sqlite3")
    transition = models.GlobalDiscoveryRecoveryTransition.__table__
    try:
        admitted = _admit(store, "gdr_r5_transition_failed_cas")
        updated = admitted.request_cancel(
            requested_at=NOW + timedelta(seconds=1),
            requested_by_actor_id="agent-observer",
            reason="bounded-test-reason",
        )
        with engine.begin() as connection:
            assert store._write_cas(
                connection,
                current=admitted,
                updated=updated,
            )
            assert not store._write_cas(
                connection,
                current=admitted,
                updated=updated,
            )
        with engine.connect() as connection:
            rows = connection.execute(
                select(transition.c.progress_seq).order_by(transition.c.progress_seq)
            ).scalars().all()
        assert rows == [0, updated.progress_seq]
    finally:
        engine.dispose()
