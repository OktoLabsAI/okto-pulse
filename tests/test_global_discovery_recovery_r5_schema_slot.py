from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.exc import IntegrityError

import okto_pulse.community.adapters.relational_schema_steps as schema_steps
import okto_pulse.community.adapters.sqlalchemy_models as models
import okto_pulse.core.infra.database as database_module
import okto_pulse.core.ports.global_discovery_recovery_control as recovery_contract
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    SQLAlchemyRecoveryRunStore,
)
from okto_pulse.community.adapters.relational_schema_migrator import (
    CREATE_ALL_BOUNDARY_STEP_ID,
    build_community_migration_ledger,
)


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def _required(owner: object, name: str):
    value = getattr(owner, name, None)
    assert value is not None, f"R5 contract is missing {name}"
    return value


def _r5_models():
    return (
        models.GlobalDiscoveryRecoveryAttempt,
        _required(models, "GlobalDiscoveryRecoverySlot"),
        _required(models, "GlobalDiscoveryRecoveryDispatch"),
    )


def _build_store(database_path: Path):
    attempt, slot, dispatch = _r5_models()
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
            insert(models.Board.__table__),
            [
                {
                    "id": f"board-schema-{index}",
                    "name": f"Schema {index}",
                    "owner_id": "agent-schema",
                    "realm_id": "local",
                }
                for index in range(2)
            ],
        )
        connection.execute(
            insert(models.Board.__table__).values(
                id="board-schema-foreign",
                name="Foreign realm board",
                owner_id="agent-foreign",
                realm_id="tenant-foreign",
            )
        )
    return SQLAlchemyRecoveryRunStore(engine=engine), engine


def _preparation_command(*, run_id: str, actor_id: str):
    binding_type = _required(recovery_contract, "RecoveryRunBinding")
    counts_type = _required(recovery_contract, "RecoveryProgressCounts")
    command_type = _required(recovery_contract, "RecoveryPreparationCommand")
    return command_type(
        binding=binding_type(
            run_id=run_id,
            actor_id=actor_id,
        ),
        admitted_at=NOW,
        counts=counts_type(
            sources_total=2,
            boards_total=2,
            boards_scanned=0,
        ),
        attempt_budget_ms=60_000,
    )


def _admit(store: SQLAlchemyRecoveryRunStore, *, run_id: str, actor_id: str):
    method = _required(store, "admit_preparation")
    result = method(_preparation_command(run_id=run_id, actor_id=actor_id))
    assert isinstance(result, tuple) and len(result) == 2
    return result


def test_store_rejects_non_local_realm_authority(tmp_path: Path) -> None:
    _store, engine = _build_store(tmp_path / "fixed-local-realm.sqlite3")
    try:
        with pytest.raises(ValueError, match="LOCAL_REALM_ID"):
            SQLAlchemyRecoveryRunStore(
                engine=engine,
                realm_id="tenant-foreign",
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("replay", [False, True])
def test_admission_and_replay_refuse_sqlite_exclusive_lock_within_two_seconds(
    tmp_path: Path,
    replay: bool,
) -> None:
    database_path = tmp_path / f"bounded-lock-{replay}.sqlite3"
    store, engine = _build_store(database_path)
    if replay:
        _admit(store, run_id="gdr_r5_lock_incumbent", actor_id="agent-first")
    locker = sqlite3.connect(database_path, timeout=0, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(_required(recovery_contract, "RecoveryInProgress")):
            _admit(
                store,
                run_id=(
                    "gdr_r5_lock_replay" if replay else "gdr_r5_lock_admission"
                ),
                actor_id="agent-contender",
            )
        assert time.monotonic() - started < 2.0
    finally:
        locker.rollback()
        locker.close()
        engine.dispose()


def _count(connection, table) -> int:
    return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def test_r5_metadata_owns_attempt_slot_and_dispatch_schema() -> None:
    attempt, slot, dispatch = _r5_models()

    assert attempt.__tablename__ == "global_discovery_recovery_attempts"
    assert slot.__tablename__ == "global_discovery_recovery_slots"
    assert dispatch.__tablename__ == "global_discovery_recovery_dispatches"
    assert set(models.Base.metadata.tables).issuperset(
        {
            attempt.__tablename__,
            slot.__tablename__,
            dispatch.__tablename__,
        }
    )

    attempt_columns = attempt.__table__.columns
    assert {
        "attempt_id",
        "requester_actor_ids_json",
        "request_count",
        "replay_count",
        "requester_actor_overflow_count",
        "first_requested_at",
        "last_requested_at",
        "confirmation_state",
        "boards_total",
        "boards_scanned",
        "attempt_budget_ms",
        "prepared_at",
        "expires_at",
        "snapshot_fingerprint",
        "confirmed_by_actor_id",
        "confirmation_consumed_at",
        "cancel_reason",
        "cancel_requested_by_actor_id",
        "physical_journal_phase",
        "physical_pointer_replaced",
        "physical_rollback_performed",
        "physical_evidence_ref",
    }.issubset(attempt_columns.keys())
    assert attempt_columns.confirmation_fingerprint.nullable is False
    assert attempt_columns.binding_reason.nullable is False
    assert str(attempt_columns.confirmation_state.server_default.arg) == "'unconfirmed'"
    assert str(attempt_columns.requester_actor_ids_json.server_default.arg) == "'[]'"

    revision = models.GlobalDiscoverySourceRevision
    assert revision.__tablename__ == "global_discovery_source_revision"
    assert tuple(column.name for column in revision.__table__.primary_key.columns) == (
        "scope_id",
    )
    assert set(revision.__table__.columns.keys()) == {
        "scope_id",
        "fence_version",
        "trigger_manifest_version",
        "incarnation_id",
        "revision",
        "mutation_nonce",
        "updated_at",
    }

    assert tuple(column.name for column in slot.__table__.primary_key.columns) == (
        "slot_id",
    )
    assert {
        "slot_id",
        "run_id",
        "attempt_id",
        "epoch",
        "actor_id",
        "version",
        "acquired_at",
        "updated_at",
    }.issubset(slot.__table__.columns.keys())
    check_names = {
        constraint.name
        for constraint in slot.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert {
        "ck_global_discovery_recovery_slot_global_scope",
        "ck_global_discovery_recovery_slot_epoch_positive",
        "ck_global_discovery_recovery_slot_version_positive",
    }.issubset(check_names)

    assert {
        "dispatch_id",
        "run_id",
        "attempt_id",
        "epoch",
        "stage",
        "state",
        "claim_token",
        "worker_id",
        "claimed_at",
        "claim_expires_at",
        "attempt_count",
        "available_at",
        "completed_at",
        "transition_event_id",
        "transition_observed_at",
    }.issubset(dispatch.__table__.columns.keys())
    unique_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in dispatch.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("run_id", "attempt_id", "epoch", "stage") in unique_sets


def test_recovery_slot_database_rejects_non_global_scope(tmp_path: Path) -> None:
    _attempt, slot, _dispatch = _r5_models()
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'slot-constraint.sqlite3').as_posix()}",
        future=True,
    )
    models.Base.metadata.create_all(engine, tables=[slot.__table__])
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert(slot.__table__).values(
                        slot_id="parallel-authority",
                        run_id="run-invalid-slot",
                        attempt_id="run-invalid-slot/attempt-1",
                        epoch=1,
                        actor_id="agent-invalid-slot",
                        version=1,
                        acquired_at=NOW,
                        updated_at=NOW,
                    )
                )
    finally:
        engine.dispose()


def test_r5_schema_migration_is_in_the_canonical_lifecycle_ledger() -> None:
    migration_name = "_migrate_global_discovery_recovery_control_plane"
    assert hasattr(schema_steps, migration_name)

    ledger = build_community_migration_ledger()
    ids = [step.step_id for step in sorted(ledger, key=lambda step: step.order)]
    assert migration_name in ids
    assert ids.index(migration_name) > ids.index(CREATE_ALL_BOUNDARY_STEP_ID)
    # 60 = the historical 46-step SK-A ratchet plus the nine additive
    # migrations registered while introducing SK-B policy governance, plus
    # the five SK-B3 closure repairs for migrated databases (guideline-
    # binding unique authority index backfill, the import-candidate
    # semantic-shape rebuild, the guideline v1 family semantic alignment,
    # the retired v1 trigger drop, and the legacy-binding semantic
    # configuration seeding).
    assert len([step for step in ledger if step.step_id.startswith("_migrate_")]) == 60


def test_preparation_persists_not_null_sentinels_but_authorizes_by_state(
    tmp_path: Path,
) -> None:
    store, engine = _build_store(tmp_path / "sentinels.sqlite3")
    try:
        status, created = _admit(
            store,
            run_id="gdr_r5_sentinel",
            actor_id="agent-sentinel",
        )
        assert created is True
        assert status.confirmation_state.value == "unconfirmed"
        assert status.preparation_state == "queued"
        assert status.binding.confirmation_fingerprint is None
        assert status.binding.reason is None

        with engine.connect() as connection:
            row = connection.execute(
                select(models.GlobalDiscoveryRecoveryAttempt.__table__)
            ).mappings().one()
        assert row["confirmation_fingerprint"] == "unconfirmed"
        assert row["binding_reason"] == "reason_pending"
        assert row["confirmation_state"] == "unconfirmed"
    finally:
        engine.dispose()


def test_cross_actor_preflight_replays_one_global_slot_and_audits_requester(
    tmp_path: Path,
) -> None:
    store, engine = _build_store(tmp_path / "global-slot.sqlite3")
    attempt, slot, dispatch = _r5_models()
    try:
        first, created = _admit(
            store,
            run_id="gdr_r5_actor_a",
            actor_id="agent-a",
        )
        assert created is True
        assert first.attempt_id == "gdr_r5_actor_a/attempt-1"
        assert first.counts.boards_total == 2

        replayed, replay_created = _admit(
            store,
            run_id="gdr_r5_actor_b",
            actor_id="agent-b",
        )
        assert replay_created is False
        assert replayed.run_id == first.run_id
        assert replayed.actor_id == "agent-a"

        with engine.connect() as connection:
            assert _count(connection, slot.__table__) == 1
            assert _count(connection, attempt.__table__) == 1
            assert _count(connection, dispatch.__table__) == 1
            owner = connection.execute(select(slot.__table__)).mappings().one()
            attempt_row = connection.execute(
                select(attempt.__table__)
            ).mappings().one()
            queued = connection.execute(select(dispatch.__table__)).mappings().one()
        assert owner["slot_id"] == "_global"
        assert owner["run_id"] == "gdr_r5_actor_a"
        assert owner["attempt_id"] == first.attempt_id
        assert queued["run_id"] == "gdr_r5_actor_a"
        assert queued["stage"] == "preparation"
        assert queued["state"] == "ready"
        assert attempt_row["actor_id"] == "agent-a"
        assert attempt_row["requester_actor_ids_json"] == '["agent-a","agent-b"]'
        assert attempt_row["request_count"] == 2
        assert attempt_row["replay_count"] == 1
        assert attempt_row["requester_actor_overflow_count"] == 0
        durable_audit = store.get_requester_audit(run_id=first.run_id)
        assert durable_audit is not None
        assert durable_audit.actor_ids == ("agent-a", "agent-b")
        assert durable_audit.request_count == 2
        assert durable_audit.replay_count == 1
        audit = queued["transition_payload"]
        assert audit["preflight_request_count"] == 2
        assert {row["actor_id"] for row in audit["preflight_requesters"]} == {
            "agent-a",
            "agent-b",
        }
    finally:
        engine.dispose()


def test_same_preparation_replay_returns_incumbent_without_duplicate_dispatch(
    tmp_path: Path,
) -> None:
    store, engine = _build_store(tmp_path / "slot-replay.sqlite3")
    _attempt, slot, dispatch = _r5_models()
    try:
        first, first_created = _admit(
            store,
            run_id="gdr_r5_replay",
            actor_id="agent-replay",
        )
        replayed, replay_created = _admit(
            store,
            run_id="gdr_r5_replay",
            actor_id="agent-replay",
        )

        assert first_created is True
        assert replay_created is False
        assert replayed == first
        with engine.connect() as connection:
            assert _count(connection, slot.__table__) == 1
            assert _count(connection, dispatch.__table__) == 1
    finally:
        engine.dispose()


def test_recovery_migration_repairs_noncanonical_attempt_defaults(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "attempt-default-repair.sqlite3"

    async def exercise() -> tuple[str | None, str | None]:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        engine = database_module.get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(models.Base.metadata.create_all)
            create_sql = (
                await connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='global_discovery_recovery_attempts'"
                    )
                )
            ).scalar_one()
            malformed_sql = str(create_sql).replace(
                "attempt_id VARCHAR(512) NOT NULL",
                "attempt_id VARCHAR(512) DEFAULT '' NOT NULL",
            ).replace(
                "confirmation_state VARCHAR(32) DEFAULT 'unconfirmed' NOT NULL",
                "confirmation_state VARCHAR(32) DEFAULT 'consumed' NOT NULL",
            )
            assert malformed_sql != create_sql
            await connection.execute(
                text(
                    'DROP INDEX "uq_global_discovery_recovery_attempt_identity"'
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE global_discovery_recovery_attempts "
                    "RENAME TO global_discovery_recovery_attempts_bad"
                )
            )
            await connection.execute(text(malformed_sql))
            await connection.execute(
                text("DROP TABLE global_discovery_recovery_attempts_bad")
            )

        first = await schema_steps._migrate_global_discovery_recovery_control_plane()
        second = await schema_steps._migrate_global_discovery_recovery_control_plane()
        async with engine.connect() as connection:
            columns = (
                await connection.execute(
                    text("PRAGMA table_info(global_discovery_recovery_attempts)")
                )
            ).mappings().all()
        await engine.dispose()
        defaults = {str(row["name"]): row["dflt_value"] for row in columns}
        assert defaults["attempt_id"] is None
        assert defaults["confirmation_state"] == "'unconfirmed'"
        return first, second

    first, second = __import__("asyncio").run(exercise())
    assert first is None
    assert second == "skipped"


def test_recovery_migration_fails_closed_on_partial_slot_contract(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "partial-slot.sqlite3"

    async def exercise() -> None:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        engine = database_module.get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(models.Base.metadata.create_all)
            create_sql = (
                await connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='global_discovery_recovery_slots'"
                    )
                )
            ).scalar_one()
            malformed_sql = str(create_sql).replace(
                "version INTEGER DEFAULT 1 NOT NULL",
                "version INTEGER DEFAULT 1",
            )
            assert malformed_sql != create_sql
            await connection.execute(
                text(
                    "ALTER TABLE global_discovery_recovery_slots "
                    "RENAME TO global_discovery_recovery_slots_bad"
                )
            )
            await connection.execute(text(malformed_sql))
            await connection.execute(
                text("DROP TABLE global_discovery_recovery_slots_bad")
            )
        with pytest.raises(RuntimeError, match="non-canonical"):
            await schema_steps._migrate_global_discovery_recovery_control_plane()
        await engine.dispose()

    __import__("asyncio").run(exercise())


def test_requester_actor_audit_is_bounded_and_rejects_oversized_actor(
    tmp_path: Path,
) -> None:
    store, engine = _build_store(tmp_path / "bounded-requesters.sqlite3")
    try:
        first, created = _admit(
            store,
            run_id="gdr_r5_bounded_requesters",
            actor_id="admitting-actor",
        )
        assert created is True
        for index in range(40):
            replayed, replay_created = _admit(
                store,
                run_id=f"gdr_r5_bounded_requester_{index}",
                actor_id=f"requester-{index:02d}",
            )
            assert replay_created is False
            assert replayed.run_id == first.run_id
        audit = store.get_requester_audit(run_id=first.run_id)
        assert audit is not None
        assert len(audit.actor_ids) == 32
        assert audit.actor_ids == tuple(sorted(audit.actor_ids))
        assert audit.request_count == 41
        assert audit.replay_count == 40
        assert audit.overflow_count == 9

        with pytest.raises(ValueError, match="actor_id"):
            _admit(
                store,
                run_id="gdr_r5_oversized_actor",
                actor_id="x" * 256,
            )
        with pytest.raises(ValueError, match="run_id"):
            _admit(
                store,
                run_id="r" * 256,
                actor_id="bounded-actor",
            )
        unchanged = store.get_requester_audit(run_id=first.run_id)
        assert unchanged == audit
    finally:
        engine.dispose()
