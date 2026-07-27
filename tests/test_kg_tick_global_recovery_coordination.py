from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import pytest
from sqlalchemy import create_engine, delete, event, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    RecoveryDispatchStage,
    RecoveryRuntimeConfigurationError,
    RecoveryStoreSchemaError,
    SQLAlchemyRecoveryRunStore,
)
from okto_pulse.community.adapters.relational_effects import (
    CommunitySqlAlchemyRelationalEffects,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    ConsolidationQueue,
    DomainEventHandlerExecution,
    DomainEventRow,
    GlobalDiscoveryRecoveryAttempt,
    GlobalDiscoveryRecoveryDispatch,
    GlobalDiscoveryRecoverySlot,
    GlobalDiscoverySourceRevision,
    GlobalUpdateOutbox,
    KGTickRun,
)
from okto_pulse.core.application.kg_tick import KGTickAdmissionDeferred
from okto_pulse.core.events.handlers.kg_decay_tick import (
    KGDailyTickHandler,
    publish_tick_events,
)
from okto_pulse.core.events.registry import register_handler
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryInProgress,
    RecoveryPreparationCommand,
    RecoveryPreparedResult,
    RecoveryProgressCounts,
    RecoveryResumeRejected,
    RecoveryRunBinding,
    RecoveryStartCommand,
    RecoveryTerminalOutcome,
    RecoveryWorkerResult,
)
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
)
from okto_pulse.core.ports.relational_effects import (
    register_relational_effects_port,
)


NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
BOARD_ID = "board-tick-recovery-fence"


class _PreparedRevoker:
    def __init__(self) -> None:
        self.revoked: set[tuple[str, int, str]] = set()

    def revoke_prepared(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
        **_kwargs: object,
    ) -> object:
        self.revoked.add((run_id, epoch, manifest_ref))
        return object()

    def is_prepared_revoked(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
    ) -> bool:
        return (run_id, epoch, manifest_ref) in self.revoked


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    dbapi_connection.execute("PRAGMA foreign_keys=ON")
    dbapi_connection.execute("PRAGMA busy_timeout=250")


async def _database(
    path: Path,
    *,
    prepared_revoker: object | None = None,
    resume_input_handoff: object | None = None,
    wall_clock=None,
) -> tuple[
    object,
    object,
    async_sessionmaker[AsyncSession],
    SQLAlchemyRecoveryRunStore,
]:
    sync_engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 0.25},
    )
    event.listen(sync_engine, "connect", _configure_sqlite_connection)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as connection:
        connection.execute(
            Board.__table__.insert().values(
                id=BOARD_ID,
                name="Tick/recovery coordination",
                owner_id="test-owner",
                realm_id="local",
            )
        )
        connection.execute(
            GlobalDiscoverySourceRevision.__table__.insert().values(
                scope_id="_global",
                fence_version="test-v1",
                trigger_manifest_version="test-v1",
                incarnation_id="incarnation-test",
                revision=41,
                mutation_nonce="nonce-test",
                updated_at=NOW,
            )
        )

    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{path.as_posix()}",
        connect_args={"timeout": 0.25},
    )
    event.listen(async_engine.sync_engine, "connect", _configure_sqlite_connection)
    factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    store = SQLAlchemyRecoveryRunStore(
        engine=sync_engine,
        prepared_revoker=prepared_revoker or _PreparedRevoker(),
        resume_input_handoff=(
            resume_input_handoff
            if resume_input_handoff is not None
            else lambda _previous, _resumed: None
        ),
        wall_clock=wall_clock or (lambda: NOW),
    )
    return sync_engine, async_engine, factory, store


def _command(run_id: str) -> RecoveryPreparationCommand:
    return RecoveryPreparationCommand(
        binding=RecoveryRunBinding(run_id=run_id, actor_id="recovery-operator"),
        admitted_at=NOW,
        counts=RecoveryProgressCounts(boards_total=1, sources_total=1),
        attempt_budget_ms=60_000,
    )


def _prepare_recovery(
    store: SQLAlchemyRecoveryRunStore,
    run_id: str,
    *,
    prepared_at: datetime = NOW + timedelta(seconds=1),
    expires_at: datetime = NOW + timedelta(seconds=301),
):
    queued, created = store.admit_preparation(_command(run_id))
    assert created is True
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id=f"{run_id}-preparation",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=30),
    )
    assert claim is not None
    store.mark_preparing(
        run_id=queued.run_id,
        attempt_id=queued.attempt_id,
        epoch=queued.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    return store.complete_preparation(
        run_id=queued.run_id,
        attempt_id=queued.attempt_id,
        epoch=queued.epoch,
        claim_token=claim.claim_token,
        completed_at=prepared_at,
        result=RecoveryPreparedResult(
            manifest_ref=f"manifest://{run_id}",
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=prepared_at,
            expires_at=expires_at,
            counts=replace(
                queued.counts,
                boards_scanned=1,
                sources_processed=1,
            ),
        ),
    )


def _prepare_and_fail_recovery(
    store: SQLAlchemyRecoveryRunStore,
    run_id: str,
):
    queued, created = store.admit_preparation(_command(run_id))
    assert created is True
    preparation_claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id=f"{run_id}-preparation",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=30),
    )
    assert preparation_claim is not None
    store.mark_preparing(
        run_id=queued.run_id,
        attempt_id=queued.attempt_id,
        epoch=queued.epoch,
        claim_token=preparation_claim.claim_token,
        at=NOW,
    )
    prepared = store.complete_preparation(
        run_id=queued.run_id,
        attempt_id=queued.attempt_id,
        epoch=queued.epoch,
        claim_token=preparation_claim.claim_token,
        completed_at=NOW + timedelta(seconds=1),
        result=RecoveryPreparedResult(
            manifest_ref=f"manifest://{run_id}",
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=301),
            counts=replace(
                queued.counts,
                boards_scanned=1,
                sources_processed=1,
            ),
        ),
    )
    confirmed = store.enqueue_execution(
        RecoveryStartCommand(
            binding=replace(
                prepared.binding,
                confirmation_fingerprint=f"sha256:confirmation:{run_id}",
                reason="test recovery execution",
            ),
            started_at=NOW + timedelta(seconds=2),
            counts=prepared.counts,
            attempt_budget_ms=60_000,
            expected_epoch=prepared.epoch,
            confirmed_by_actor_id="recovery-operator",
            confirmation_consumed_at=NOW + timedelta(seconds=2),
        )
    )
    recovery_claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.RECOVERY,
        worker_id=f"{run_id}-recovery",
        claimed_at=NOW + timedelta(seconds=3),
        claim_expires_at=NOW + timedelta(seconds=33),
    )
    assert recovery_claim is not None
    running = store.get_status(run_id=confirmed.run_id)
    assert running is not None
    failed = store.complete_recovery(
        dispatch_id=recovery_claim.dispatch_id,
        claim_token=recovery_claim.claim_token,
        expected_progress_seq=running.progress_seq,
        completed_at=NOW + timedelta(seconds=4),
        active_elapsed_ms=1_000,
        result=RecoveryWorkerResult(
            outcome=RecoveryTerminalOutcome.FAILED,
            reason_code="native_operation_failed",
            retryable=True,
            counts=running.counts,
        ),
    )
    assert failed.state.value == "failed"
    return failed


def _register_tick_runtime(
    adapter: CommunitySqlAlchemyRelationalEffects,
) -> None:
    register_relational_effects_port(adapter)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_handler("kg.tick.daily", "kg.tick.full_rebuild")(KGDailyTickHandler)


async def _is_active(
    factory: async_sessionmaker[AsyncSession],
    adapter: CommunitySqlAlchemyRelationalEffects,
) -> bool:
    async with factory() as session:
        return await adapter.is_global_recovery_active(session)


def _remove_and_restore_slot(sync_engine: object) -> dict[str, object]:
    with sync_engine.begin() as connection:
        row = (
            connection.execute(select(GlobalDiscoveryRecoverySlot.__table__))
            .mappings()
            .one()
        )
        payload = dict(row)
        connection.execute(delete(GlobalDiscoveryRecoverySlot))
    return payload


def _restore_slot(sync_engine: object, payload: dict[str, object]) -> None:
    with sync_engine.begin() as connection:
        connection.execute(
            GlobalDiscoveryRecoverySlot.__table__.insert().values(**payload)
        )


async def _assert_zero_tick_effects(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        counts = {
            model.__tablename__: int(
                await session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in (
                DomainEventRow,
                KGTickRun,
                GlobalUpdateOutbox,
            )
        }
        revision = await session.scalar(
            select(GlobalDiscoverySourceRevision.revision).where(
                GlobalDiscoverySourceRevision.scope_id == "_global"
            )
        )
    assert counts == {
        "domain_events": 0,
        "kg_tick_runs": 0,
        "global_update_outbox": 0,
    }
    assert revision == 41


@pytest.mark.asyncio
async def test_all_active_phases_defer_tick_and_terminal_release_resumes(
    tmp_path: Path,
) -> None:
    sync_engine, async_engine, factory, store = await _database(
        tmp_path / "phase-gate.db"
    )
    adapter = CommunitySqlAlchemyRelationalEffects()
    _register_tick_runtime(adapter)
    try:
        queued, created = store.admit_preparation(_command("recovery-phase-gate"))
        assert created is True
        assert (queued.state.value, queued.phase.value) == ("pending", "queued")
        slot = _remove_and_restore_slot(sync_engine)
        assert await _is_active(factory, adapter) is True
        _restore_slot(sync_engine, slot)

        async with factory() as session:
            with pytest.raises(KGTickAdmissionDeferred) as deferred:
                await publish_tick_events(
                    session,
                    board_id=BOARD_ID,
                    force_full_rebuild=True,
                )
            assert deferred.value.reason_code == "global_recovery_active"
            await session.rollback()
        await _assert_zero_tick_effects(factory)

        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="phase-gate-worker",
            claimed_at=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        assert claim is not None
        preparing = store.mark_preparing(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            at=NOW,
        )
        assert (preparing.state.value, preparing.phase.value) == (
            "running",
            "preparing",
        )
        slot = _remove_and_restore_slot(sync_engine)
        assert await _is_active(factory, adapter) is True
        _restore_slot(sync_engine, slot)

        prepared_counts = replace(
            queued.counts,
            boards_scanned=1,
            sources_processed=1,
        )
        prepared = store.complete_preparation(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            completed_at=NOW + timedelta(seconds=1),
            result=RecoveryPreparedResult(
                manifest_ref="manifest://phase-gate",
                preflight_hash="preflight-phase-gate",
                snapshot_fingerprint="sha256:phase-gate",
                prepared_at=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(seconds=301),
                counts=prepared_counts,
            ),
        )
        assert (prepared.state.value, prepared.phase.value) == (
            "pending",
            "prepared",
        )
        slot = _remove_and_restore_slot(sync_engine)
        assert await _is_active(factory, adapter) is True
        _restore_slot(sync_engine, slot)

        store.cancel_prepared(
            run_id=prepared.run_id,
            expected_epoch=prepared.epoch,
            requested_at=NOW + timedelta(seconds=2),
            requested_by_actor_id="recovery-operator",
            reason="phase gate terminal release",
        )
        deadline = time.monotonic() + 3
        while True:
            terminal = store.get_status(run_id=prepared.run_id)
            assert terminal is not None
            if terminal.state.is_terminal:
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        assert terminal.state.value == "cancelled"
        with sync_engine.connect() as connection:
            slot_count = int(
                connection.scalar(
                    select(func.count()).select_from(GlobalDiscoveryRecoverySlot)
                )
                or 0
            )
        assert slot_count == 0
        assert await _is_active(factory, adapter) is False

        async with factory() as session:
            await publish_tick_events(
                session,
                board_id=BOARD_ID,
                force_full_rebuild=True,
            )
            await session.commit()
        async with factory() as session:
            event_row = (
                await session.execute(
                    select(DomainEventRow).where(
                        DomainEventRow.event_type == "kg.tick.full_rebuild"
                    )
                )
            ).scalar_one()
            execution_count = int(
                await session.scalar(
                    select(func.count()).select_from(DomainEventHandlerExecution)
                )
                or 0
            )
        assert event_row.payload_json["force_full_rebuild"] is True
        assert execution_count == 1
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_recovery_admission_defers_for_tick_event_and_downstream_queue(
    tmp_path: Path,
) -> None:
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "recovery-admission.db"
    )
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                DomainEventRow.__table__.insert().values(
                    id="tick-event-active",
                    event_type="kg.tick.full_rebuild",
                    board_id=BOARD_ID,
                    actor_type="system",
                    payload_json={
                        "tick_id": "tick-active",
                        "scheduled_at": NOW.isoformat(),
                    },
                    occurred_at=NOW,
                )
            )
            connection.execute(
                DomainEventHandlerExecution.__table__.insert().values(
                    id="tick-execution-active",
                    event_id="tick-event-active",
                    handler_name="KGDailyTickHandler",
                    status="pending",
                    attempts=0,
                )
            )

        with pytest.raises(RecoveryInProgress):
            store.admit_preparation(_command("recovery-blocked-by-event"))

        with sync_engine.begin() as connection:
            connection.execute(
                update(DomainEventHandlerExecution)
                .where(DomainEventHandlerExecution.id == "tick-execution-active")
                .values(status="done", processed_at=NOW)
            )
            connection.execute(
                ConsolidationQueue.__table__.insert().values(
                    id="tick-sweep-active",
                    board_id=BOARD_ID,
                    artifact_type="board",
                    artifact_id=BOARD_ID,
                    work_kind="stale_sweep",
                    generation=0,
                    priority="high",
                    source="kg_tick",
                    status="pending",
                    triggered_at=NOW,
                    triggered_by_event="kg.tick.daily",
                    attempts=0,
                )
            )

        with pytest.raises(RecoveryInProgress):
            store.admit_preparation(_command("recovery-blocked-by-queue"))

        with sync_engine.begin() as connection:
            connection.execute(
                update(ConsolidationQueue)
                .where(ConsolidationQueue.id == "tick-sweep-active")
                .values(status="done")
            )

        admitted, created = store.admit_preparation(
            _command("recovery-after-tick-terminal")
        )
        assert created is True
        assert admitted.phase.value == "queued"
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_tick_publication_writer_fence_wins_before_recovery_admission(
    tmp_path: Path,
) -> None:
    sync_engine, async_engine, factory, store = await _database(
        tmp_path / "tick-wins.db"
    )
    adapter = CommunitySqlAlchemyRelationalEffects()
    _register_tick_runtime(adapter)
    try:
        async with factory() as session:
            await publish_tick_events(session, board_id=BOARD_ID)
            admission = asyncio.create_task(
                asyncio.to_thread(
                    store.admit_preparation,
                    _command("recovery-racing-tick"),
                )
            )
            await asyncio.sleep(0.1)
            assert admission.done() is False
            await session.commit()
            with pytest.raises(RecoveryInProgress):
                await admission

        with sync_engine.connect() as connection:
            event_count = int(
                connection.scalar(select(func.count()).select_from(DomainEventRow)) or 0
            )
            recovery_count = int(
                connection.scalar(
                    select(func.count()).select_from(GlobalDiscoveryRecoveryAttempt)
                )
                or 0
            )
            slot_count = int(
                connection.scalar(
                    select(func.count()).select_from(GlobalDiscoveryRecoverySlot)
                )
                or 0
            )
        assert (event_count, recovery_count, slot_count) == (1, 0, 0)
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_recovery_winning_after_stale_read_forces_tick_rollback(
    tmp_path: Path,
) -> None:
    sync_engine, async_engine, factory, store = await _database(
        tmp_path / "recovery-wins.db"
    )
    adapter = CommunitySqlAlchemyRelationalEffects()
    _register_tick_runtime(adapter)
    try:
        async with factory() as session:
            # Explicit BEGIN is intentional: Python's legacy sqlite3 mode does
            # not necessarily begin a read transaction for SELECT. This fixes
            # the old negative observation into a real WAL snapshot.
            await session.execute(text("BEGIN"))
            await session.execute(select(func.count()).select_from(Board))
            assert await adapter.is_global_recovery_active(session) is False

            admitted, created = await asyncio.to_thread(
                store.admit_preparation,
                _command("recovery-wins-stale-snapshot"),
            )
            assert created is True
            assert admitted.phase.value == "queued"

            with pytest.raises(KGTickAdmissionDeferred) as deferred:
                await publish_tick_events(session, board_id=BOARD_ID)
            assert deferred.value.reason_code == "recovery_guard_unavailable"
            assert isinstance(deferred.value.__cause__, OperationalError)
            await session.rollback()

        with sync_engine.connect() as connection:
            event_count = int(
                connection.scalar(select(func.count()).select_from(DomainEventRow)) or 0
            )
            execution_count = int(
                connection.scalar(
                    select(func.count()).select_from(DomainEventHandlerExecution)
                )
                or 0
            )
            slot_count = int(
                connection.scalar(
                    select(func.count()).select_from(GlobalDiscoveryRecoverySlot)
                )
                or 0
            )
        assert (event_count, execution_count, slot_count) == (0, 0, 1)
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_tick_descendant_provenance_blocks_without_false_positive(
    tmp_path: Path,
) -> None:
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "tick-descendant-provenance.db"
    )
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                ConsolidationQueue.__table__.insert().values(
                    id="tick-descendant-active",
                    board_id=BOARD_ID,
                    artifact_type="decision",
                    artifact_id="tick-descendant",
                    work_kind="stale_reconcile",
                    generation=7,
                    delete_event_id=f"catchup:{BOARD_ID}:decision:tick-descendant:7",
                    priority="high",
                    source="governed_delete",
                    status="claimed",
                    triggered_at=NOW,
                    triggered_by_event=(
                        f"catchup:{BOARD_ID}:decision:tick-descendant:7"
                    ),
                    attempts=0,
                )
            )

        with pytest.raises(RecoveryInProgress):
            store.admit_preparation(_command("recovery-blocked-by-descendant"))

        with sync_engine.begin() as connection:
            connection.execute(
                update(ConsolidationQueue)
                .where(ConsolidationQueue.id == "tick-descendant-active")
                .values(status="done")
            )
            connection.execute(
                ConsolidationQueue.__table__.insert().values(
                    id="ordinary-governed-delete",
                    board_id=BOARD_ID,
                    artifact_type="decision",
                    artifact_id="ordinary-deletion",
                    work_kind="stale_reconcile",
                    generation=8,
                    delete_event_id="delete:ordinary-governed-event",
                    priority="high",
                    source="governed_delete",
                    status="pending",
                    triggered_at=NOW,
                    triggered_by_event="artifact.deleted",
                    attempts=0,
                )
            )

        admitted, created = store.admit_preparation(
            _command("recovery-not-blocked-by-ordinary-delete")
        )
        assert created is True
        assert admitted.phase.value == "queued"
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_busy_admission_writer_maps_to_bounded_retryable_refusal(
    tmp_path: Path,
) -> None:
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "busy-admission.db"
    )
    writer_started = Event()

    def hold_writer() -> None:
        with sync_engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.execute(
                update(Board).where(Board.id == BOARD_ID).values(name="writer-held")
            )
            writer_started.set()
            time.sleep(1.1)
            connection.rollback()

    holder = Thread(target=hold_writer, daemon=True)
    holder.start()
    try:
        assert writer_started.wait(timeout=2)
        started = time.monotonic()
        with pytest.raises(RecoveryInProgress):
            store.admit_preparation(_command("recovery-busy-refusal"))
        elapsed = time.monotonic() - started
        assert 0.5 <= elapsed < 1.1

        with sync_engine.connect() as connection:
            attempt_count = int(
                connection.scalar(
                    select(func.count()).select_from(
                        GlobalDiscoveryRecoveryAttempt
                    )
                )
                or 0
            )
            slot_count = int(
                connection.scalar(
                    select(func.count()).select_from(GlobalDiscoveryRecoverySlot)
                )
                or 0
            )
        assert (attempt_count, slot_count) == (0, 0)
    finally:
        holder.join(timeout=2)
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_tick_writer_fence_wins_against_explicit_resume(
    tmp_path: Path,
) -> None:
    sync_engine, async_engine, factory, store = await _database(
        tmp_path / "tick-wins-resume.db"
    )
    adapter = CommunitySqlAlchemyRelationalEffects()
    _register_tick_runtime(adapter)
    try:
        failed = _prepare_and_fail_recovery(store, "resume-racing-tick")
        async with factory() as session:
            await publish_tick_events(session, board_id=BOARD_ID)
            resume = asyncio.create_task(
                asyncio.to_thread(
                    store.admit_explicit_resume,
                    run_id=failed.run_id,
                    expected_epoch=failed.epoch,
                    requested_at=NOW + timedelta(seconds=5),
                    requested_by_actor_id="resume-operator",
                    reason="tick-first race",
                )
            )
            await asyncio.sleep(0.1)
            assert resume.done() is False
            await session.commit()
            with pytest.raises(RecoveryInProgress):
                await resume

        with sync_engine.connect() as connection:
            attempt_count = int(
                connection.scalar(
                    select(func.count()).select_from(
                        GlobalDiscoveryRecoveryAttempt
                    )
                )
                or 0
            )
            slot_count = int(
                connection.scalar(
                    select(func.count()).select_from(GlobalDiscoveryRecoverySlot)
                )
                or 0
            )
        assert (attempt_count, slot_count) == (1, 0)
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_explicit_resume_reservation_wins_and_defers_tick_publication(
    tmp_path: Path,
) -> None:
    handoff_entered = Event()
    release_handoff = Event()

    def blocking_handoff(_previous, _resumed) -> None:
        handoff_entered.set()
        assert release_handoff.wait(timeout=3)

    sync_engine, async_engine, factory, store = await _database(
        tmp_path / "resume-wins-tick.db",
        resume_input_handoff=blocking_handoff,
    )
    adapter = CommunitySqlAlchemyRelationalEffects()
    _register_tick_runtime(adapter)
    try:
        failed = _prepare_and_fail_recovery(store, "resume-wins-tick")
        resume = asyncio.create_task(
            asyncio.to_thread(
                store.admit_explicit_resume,
                run_id=failed.run_id,
                expected_epoch=failed.epoch,
                requested_at=NOW + timedelta(seconds=5),
                requested_by_actor_id="resume-operator",
                reason="resume-first race",
            )
        )
        assert await asyncio.to_thread(handoff_entered.wait, 2)

        async with factory() as session:
            publication = asyncio.create_task(
                publish_tick_events(session, board_id=BOARD_ID)
            )
            await asyncio.sleep(0.05)
            release_handoff.set()
            with pytest.raises(KGTickAdmissionDeferred) as deferred:
                await publication
            assert deferred.value.reason_code == "global_recovery_active"
            await session.rollback()
            resumed, transitioned = await resume
            assert transitioned is True
            assert resumed.epoch == 2

        await _assert_zero_tick_effects(factory)
    finally:
        release_handoff.set()
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_blocked_resume_handoff_does_not_hold_relational_writer(
    tmp_path: Path,
) -> None:
    handoff_entered = Event()
    release_handoff = Event()

    def blocking_handoff(_previous, _resumed) -> None:
        handoff_entered.set()
        assert release_handoff.wait(timeout=3)

    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "resume-handoff-short-writer.db",
        resume_input_handoff=blocking_handoff,
    )
    try:
        failed = _prepare_and_fail_recovery(store, "blocked-resume-handoff")
        resume = asyncio.create_task(
            asyncio.to_thread(
                store.admit_explicit_resume,
                run_id=failed.run_id,
                expected_epoch=failed.epoch,
                requested_at=NOW + timedelta(seconds=5),
                requested_by_actor_id="resume-operator",
                reason="prove short SQL window",
            )
        )
        assert await asyncio.to_thread(handoff_entered.wait, 2)

        started = time.monotonic()
        with sync_engine.begin() as connection:
            connection.execute(
                update(Board).where(Board.id == BOARD_ID).values(name="not-blocked")
            )
        assert time.monotonic() - started < 0.5

        release_handoff.set()
        resumed, transitioned = await asyncio.wait_for(resume, timeout=2)
        assert transitioned is True
        assert resumed.epoch == 2
        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.RECOVERY,
            worker_id="post-handoff-worker",
            claimed_at=NOW + timedelta(seconds=6),
            claim_expires_at=NOW + timedelta(seconds=36),
        )
        assert claim is not None
        assert claim.epoch == 2
    finally:
        release_handoff.set()
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_blocked_revoker_probe_does_not_hold_relational_writer(
    tmp_path: Path,
) -> None:
    class BlockingRevoker(_PreparedRevoker):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()
            self.block = False

        def is_prepared_revoked(self, **kwargs) -> bool:
            if self.block:
                self.entered.set()
                assert self.release.wait(timeout=3)
            return super().is_prepared_revoked(**kwargs)

    revoker = BlockingRevoker()
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "revoker-short-writer.db",
        prepared_revoker=revoker,
    )
    try:
        queued, _ = store.admit_preparation(_command("prepared-incumbent"))
        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="prepare-incumbent",
            claimed_at=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        assert claim is not None
        store.mark_preparing(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            at=NOW,
        )
        store.complete_preparation(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            completed_at=NOW + timedelta(seconds=1),
            result=RecoveryPreparedResult(
                manifest_ref="manifest://prepared-incumbent",
                preflight_hash="preflight-prepared-incumbent",
                snapshot_fingerprint="sha256:prepared-incumbent",
                prepared_at=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(seconds=301),
                counts=replace(
                    queued.counts,
                    boards_scanned=1,
                    sources_processed=1,
                ),
            ),
        )
        revoker.block = True
        replay = asyncio.create_task(
            asyncio.to_thread(
                store.admit_preparation,
                _command("second-preflight-request"),
            )
        )
        assert await asyncio.to_thread(revoker.entered.wait, 2)

        started = time.monotonic()
        with sync_engine.begin() as connection:
            connection.execute(
                update(Board)
                .where(Board.id == BOARD_ID)
                .values(name="writer-during-revoker")
            )
        assert time.monotonic() - started < 0.5

        revoker.release.set()
        observed, created = await asyncio.wait_for(replay, timeout=2)
        assert created is False
        assert observed.run_id == queued.run_id
    finally:
        revoker.release.set()
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_confirmation_revocation_probe_does_not_hold_relational_writer(
    tmp_path: Path,
) -> None:
    class BlockingProbe(_PreparedRevoker):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()
            self.block = False

        def is_prepared_revoked(self, **kwargs) -> bool:
            if self.block:
                self.entered.set()
                assert self.release.wait(timeout=3)
            return super().is_prepared_revoked(**kwargs)

    revoker = BlockingProbe()
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "confirmation-probe-no-writer.db",
        prepared_revoker=revoker,
    )
    try:
        prepared = _prepare_recovery(store, "confirmation-probe-no-writer")
        revoker.block = True
        confirmation = asyncio.create_task(
            asyncio.to_thread(
                store.enqueue_execution,
                RecoveryStartCommand(
                    binding=replace(
                        prepared.binding,
                        confirmation_fingerprint="sha256:confirmation-probe",
                        reason="confirm without holding SQL writer",
                    ),
                    started_at=NOW + timedelta(seconds=2),
                    counts=prepared.counts,
                    attempt_budget_ms=60_000,
                    expected_epoch=prepared.epoch,
                    confirmed_by_actor_id="confirmation-operator",
                    confirmation_consumed_at=NOW + timedelta(seconds=2),
                ),
            )
        )
        assert await asyncio.to_thread(revoker.entered.wait, 2)

        started = time.monotonic()
        with sync_engine.begin() as connection:
            connection.execute(
                update(Board)
                .where(Board.id == BOARD_ID)
                .values(name="writer progressed during confirmation probe")
            )
        assert time.monotonic() - started < 0.5

        revoker.release.set()
        confirmed = await asyncio.wait_for(confirmation, timeout=2)
        assert confirmed.phase.value == "confirmed"
    finally:
        revoker.release.set()
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_preparation_terminal_revocation_runs_after_t1_without_writer(
    tmp_path: Path,
) -> None:
    class BlockingProbe(_PreparedRevoker):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()

        def is_prepared_revoked(self, **kwargs) -> bool:
            self.entered.set()
            assert self.release.wait(timeout=3)
            return super().is_prepared_revoked(**kwargs)

    revoker = BlockingProbe()
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "terminal-revocation-no-writer.db",
        prepared_revoker=revoker,
    )
    try:
        queued, _ = store.admit_preparation(
            _command("terminal-revocation-no-writer")
        )
        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="terminal-revocation-worker",
            claimed_at=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        assert claim is not None
        preparing = store.mark_preparing(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            at=NOW,
        )
        manifest_ref = "manifest://terminal-revocation-no-writer"
        store.heartbeat_preparation(
            dispatch_id=claim.dispatch_id,
            claim_token=claim.claim_token,
            observed_at=NOW + timedelta(seconds=1),
            requested_expires_at=NOW + timedelta(seconds=31),
            active_elapsed_ms=1_000,
            counts=preparing.counts,
            manifest_ref=manifest_ref,
        )
        terminalization = asyncio.create_task(
            asyncio.to_thread(
                store.record_preparation_failure,
                dispatch_id=claim.dispatch_id,
                claim_token=claim.claim_token,
                failed_at=NOW + timedelta(seconds=2),
                active_elapsed_ms=2_000,
                counts=replace(preparing.counts, errors=1),
                reason_code="terminal_test_failure",
                retryable=False,
                retry_available_at=NOW + timedelta(seconds=3),
                max_attempts=3,
                manifest_ref=manifest_ref,
            )
        )
        assert await asyncio.to_thread(revoker.entered.wait, 2)
        with sync_engine.connect() as connection:
            assert connection.scalar(
                select(func.count()).select_from(
                    GlobalDiscoveryRecoverySlot
                )
            ) == 1
            assert connection.scalar(
                select(GlobalDiscoveryRecoveryDispatch.state)
            ) == "settling"
        assert (
            store.claim_next_dispatch(
                stage=RecoveryDispatchStage.PREPARATION,
                worker_id="must-not-claim-settling",
                claimed_at=NOW + timedelta(seconds=3),
                claim_expires_at=NOW + timedelta(seconds=33),
            )
            is None
        )

        started = time.monotonic()
        with sync_engine.begin() as connection:
            connection.execute(
                update(Board)
                .where(Board.id == BOARD_ID)
                .values(name="writer progressed during terminal revoke")
            )
        assert time.monotonic() - started < 0.5

        revoker.release.set()
        await asyncio.wait_for(terminalization, timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with sync_engine.connect() as connection:
                slot_count = int(
                    connection.scalar(
                        select(func.count()).select_from(
                            GlobalDiscoveryRecoverySlot
                        )
                    )
                    or 0
                )
                dispatch_state = connection.scalar(
                    select(GlobalDiscoveryRecoveryDispatch.state)
                )
            if slot_count == 0 and dispatch_state == "done":
                break
            await asyncio.sleep(0.02)
        assert (slot_count, dispatch_state) == (0, "done")
        terminal = store.get_status(run_id=queued.run_id)
        assert terminal is not None
        assert terminal.state.value == "failed"
        assert (
            terminal.run_id,
            terminal.epoch,
            manifest_ref,
        ) in revoker.revoked
    finally:
        revoker.release.set()
        store.close(timeout_seconds=1)
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_startup_adopts_committed_preparation_settlement(
    tmp_path: Path,
) -> None:
    revoker = _PreparedRevoker()
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "startup-preparation-settlement.db",
        prepared_revoker=revoker,
    )
    adopter = None
    original_scheduler = store._schedule_preparation_settlement
    try:
        queued, _ = store.admit_preparation(
            _command("startup-preparation-settlement")
        )
        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="startup-settlement-worker",
            claimed_at=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        assert claim is not None
        preparing = store.mark_preparing(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            at=NOW,
        )
        manifest_ref = "manifest://startup-preparation-settlement"
        store.heartbeat_preparation(
            dispatch_id=claim.dispatch_id,
            claim_token=claim.claim_token,
            observed_at=NOW + timedelta(seconds=1),
            requested_expires_at=NOW + timedelta(seconds=31),
            active_elapsed_ms=1_000,
            counts=preparing.counts,
            manifest_ref=manifest_ref,
        )
        store._schedule_preparation_settlement = (  # type: ignore[method-assign]
            lambda **_kwargs: None
        )
        terminal = store.record_preparation_failure(
            dispatch_id=claim.dispatch_id,
            claim_token=claim.claim_token,
            failed_at=NOW + timedelta(seconds=2),
            active_elapsed_ms=2_000,
            counts=replace(preparing.counts, errors=1),
            reason_code="startup_adoption_test_failure",
            retryable=False,
            retry_available_at=NOW + timedelta(seconds=3),
            max_attempts=3,
            manifest_ref=manifest_ref,
        )
        assert terminal.state.value == "running"
        with sync_engine.connect() as connection:
            assert connection.scalar(
                select(GlobalDiscoveryRecoveryDispatch.state)
            ) == "settling"
            assert connection.scalar(
                select(func.count()).select_from(
                    GlobalDiscoveryRecoverySlot
                )
            ) == 1

        store._schedule_preparation_settlement = original_scheduler  # type: ignore[method-assign]
        adopter = SQLAlchemyRecoveryRunStore(
            engine=sync_engine,
            prepared_revoker=revoker,
            wall_clock=lambda: NOW,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with sync_engine.connect() as connection:
                slot_count = int(
                    connection.scalar(
                        select(func.count()).select_from(
                            GlobalDiscoveryRecoverySlot
                        )
                    )
                    or 0
                )
                dispatch_state = connection.scalar(
                    select(GlobalDiscoveryRecoveryDispatch.state)
                )
            if slot_count == 0 and dispatch_state == "done":
                break
            await asyncio.sleep(0.02)
        assert (slot_count, dispatch_state) == (0, "done")
        terminal = store.get_status(run_id=queued.run_id)
        assert terminal is not None
        assert terminal.state.value == "failed"
        assert (
            terminal.run_id,
            terminal.epoch,
            manifest_ref,
        ) in revoker.revoked
    finally:
        store._schedule_preparation_settlement = original_scheduler  # type: ignore[method-assign]
        if adopter is not None:
            adopter.close(timeout_seconds=1)
        store.close(timeout_seconds=1)
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_startup_adopts_resume_reservation_left_before_handoff(
    tmp_path: Path,
) -> None:
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "resume-startup-adoption.db"
    )
    handoff_completed = Event()
    try:
        failed = _prepare_and_fail_recovery(store, "resume-startup-adoption")
        reserved, transitioned, predecessor = store._reserve_explicit_resume(
            run_id=failed.run_id,
            expected_epoch=failed.epoch,
            requested_at=NOW + timedelta(seconds=5),
            requested_by_actor_id="resume-operator",
            reason="simulate crash after reservation",
        )
        assert transitioned is True
        assert predecessor is not None

        def adopt_handoff(_previous, resumed) -> None:
            assert resumed.attempt_id == reserved.attempt_id
            handoff_completed.set()

        reopened = SQLAlchemyRecoveryRunStore(
            engine=sync_engine,
            prepared_revoker=_PreparedRevoker(),
            resume_input_handoff=adopt_handoff,
            wall_clock=lambda: NOW + timedelta(seconds=6),
        )
        assert await asyncio.to_thread(handoff_completed.wait, 2)

        deadline = time.monotonic() + 2
        claim = None
        while claim is None and time.monotonic() < deadline:
            claim = reopened.claim_next_dispatch(
                stage=RecoveryDispatchStage.RECOVERY,
                worker_id="startup-adoption-worker",
                claimed_at=NOW + timedelta(seconds=7),
                claim_expires_at=NOW + timedelta(seconds=37),
            )
            if claim is None:
                await asyncio.sleep(0.02)
        assert claim is not None
        assert claim.epoch == reserved.epoch
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_handoff_failure_is_retried_by_owned_reservation_reconciler(
    tmp_path: Path,
) -> None:
    attempts = 0
    reconciled = Event()

    def flaky_handoff(_previous, _resumed) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient handoff failure")
        reconciled.set()

    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "resume-handoff-retry.db",
        resume_input_handoff=flaky_handoff,
    )
    try:
        failed = _prepare_and_fail_recovery(store, "resume-handoff-retry")
        with pytest.raises(RuntimeError, match="transient handoff failure"):
            store.admit_explicit_resume(
                run_id=failed.run_id,
                expected_epoch=failed.epoch,
                requested_at=NOW + timedelta(seconds=5),
                requested_by_actor_id="resume-operator",
                reason="retry handoff automatically",
            )
        assert await asyncio.to_thread(reconciled.wait, 2)

        deadline = time.monotonic() + 2
        claim = None
        while claim is None and time.monotonic() < deadline:
            claim = store.claim_next_dispatch(
                stage=RecoveryDispatchStage.RECOVERY,
                worker_id="handoff-retry-worker",
                claimed_at=NOW + timedelta(seconds=6),
                claim_expires_at=NOW + timedelta(seconds=36),
            )
            if claim is None:
                await asyncio.sleep(0.02)
        assert claim is not None
        assert claim.epoch == 2
        assert attempts >= 2
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_finalize_busy_is_retried_after_writer_releases(
    tmp_path: Path,
) -> None:
    handoff_entered = Event()
    writer_acquired = Event()
    callback_count = 0

    def handoff(_previous, _resumed) -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count == 1:
            handoff_entered.set()
            assert writer_acquired.wait(timeout=2)

    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "resume-finalize-busy.db",
        resume_input_handoff=handoff,
    )

    def hold_writer() -> None:
        with sync_engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.execute(
                update(Board).where(Board.id == BOARD_ID).values(name="finalize-busy")
            )
            writer_acquired.set()
            time.sleep(1.1)
            connection.rollback()

    try:
        failed = _prepare_and_fail_recovery(store, "resume-finalize-busy")
        resume = asyncio.create_task(
            asyncio.to_thread(
                store.admit_explicit_resume,
                run_id=failed.run_id,
                expected_epoch=failed.epoch,
                requested_at=NOW + timedelta(seconds=5),
                requested_by_actor_id="resume-operator",
                reason="retry finalize automatically",
            )
        )
        assert await asyncio.to_thread(handoff_entered.wait, 2)
        holder = Thread(target=hold_writer, daemon=True)
        holder.start()
        with pytest.raises(RecoveryInProgress):
            await resume
        holder.join(timeout=2)

        deadline = time.monotonic() + 3
        claim = None
        while claim is None and time.monotonic() < deadline:
            claim = store.claim_next_dispatch(
                stage=RecoveryDispatchStage.RECOVERY,
                worker_id="finalize-retry-worker",
                claimed_at=NOW + timedelta(seconds=7),
                claim_expires_at=NOW + timedelta(seconds=37),
            )
            if claim is None:
                await asyncio.sleep(0.03)
        assert claim is not None
        assert claim.epoch == 2
        assert callback_count >= 2
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_expiry_reservation_blocks_confirmation_before_external_revoke(
    tmp_path: Path,
) -> None:
    class BlockingRevoker(_PreparedRevoker):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()
            self.block = False

        def is_prepared_revoked(self, **kwargs) -> bool:
            if self.block:
                self.entered.set()
                assert self.release.wait(timeout=3)
            return super().is_prepared_revoked(**kwargs)

    revoker = BlockingRevoker()
    clock = [NOW]
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "expiry-confirmation-race.db",
        prepared_revoker=revoker,
        wall_clock=lambda: clock[0],
    )
    try:
        queued, _ = store.admit_preparation(_command("expiring-incumbent"))
        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="expiring-preparation",
            claimed_at=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        assert claim is not None
        store.mark_preparing(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            at=NOW,
        )
        prepared = store.complete_preparation(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            completed_at=NOW,
            result=RecoveryPreparedResult(
                manifest_ref="manifest://expiring-incumbent",
                preflight_hash="preflight-expiring-incumbent",
                snapshot_fingerprint="sha256:expiring-incumbent",
                prepared_at=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(seconds=301),
                counts=replace(
                    queued.counts,
                    boards_scanned=1,
                    sources_processed=1,
                ),
            ),
        )
        clock[0] = NOW + timedelta(seconds=302)
        revoker.block = True
        admission = asyncio.create_task(
            asyncio.to_thread(
                store.admit_preparation,
                _command("after-expired-incumbent"),
            )
        )
        assert await asyncio.to_thread(revoker.entered.wait, 2)

        with pytest.raises(RecoveryResumeRejected) as confirmation_refused:
            store.enqueue_execution(
                RecoveryStartCommand(
                    binding=replace(
                        prepared.binding,
                        confirmation_fingerprint="sha256:too-late",
                        reason="confirmation lost to expiry reservation",
                    ),
                    started_at=clock[0],
                    counts=prepared.counts,
                    attempt_budget_ms=60_000,
                    expected_epoch=prepared.epoch,
                    confirmed_by_actor_id="late-confirmer",
                    confirmation_consumed_at=clock[0],
                )
            )
        assert confirmation_refused.value.code == "recovery_cancel_pending"

        revoker.release.set()
        replacement, created = await asyncio.wait_for(admission, timeout=2)
        assert created is True
        assert replacement.run_id == "after-expired-incumbent"
        incumbent = store.get_status_at_epoch(
            run_id=prepared.run_id,
            epoch=prepared.epoch,
        )
        assert incumbent is not None
        assert incumbent.state.value == "cancelled"
    finally:
        revoker.release.set()
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_expiry_t2_rejects_changed_canonical_revocation_tuple(
    tmp_path: Path,
) -> None:
    class BlockingRevoker(_PreparedRevoker):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()

        def is_prepared_revoked(self, **kwargs) -> bool:
            self.entered.set()
            assert self.release.wait(timeout=3)
            return super().is_prepared_revoked(**kwargs)

    revoker = BlockingRevoker()
    clock = [NOW]
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "expiry-tuple-change.db",
        prepared_revoker=revoker,
        wall_clock=lambda: clock[0],
    )
    try:
        prepared = _prepare_recovery(store, "expiry-tuple-change")
        clock[0] = NOW + timedelta(seconds=302)
        admission = asyncio.create_task(
            asyncio.to_thread(
                store.admit_preparation,
                _command("replacement-after-tuple-change"),
            )
        )
        assert await asyncio.to_thread(revoker.entered.wait, 2)

        with sync_engine.begin() as connection:
            changed = connection.execute(
                update(GlobalDiscoveryRecoveryAttempt)
                .where(
                    GlobalDiscoveryRecoveryAttempt.run_id
                    == prepared.run_id,
                    GlobalDiscoveryRecoveryAttempt.epoch
                    == prepared.epoch,
                )
                .values(
                    cancel_requested_by_actor_id="competing-canceller"
                )
            )
            assert changed.rowcount == 1

        revoker.release.set()
        with pytest.raises(RecoveryStoreSchemaError) as conflict:
            await asyncio.wait_for(admission, timeout=2)
        assert conflict.value.code == (
            "global_discovery_recovery_revocation_intent_conflict"
        )
        with sync_engine.connect() as connection:
            assert connection.scalar(
                select(func.count()).select_from(
                    GlobalDiscoveryRecoverySlot
                )
            ) == 1
            assert connection.scalar(
                select(func.count())
                .select_from(GlobalDiscoveryRecoveryAttempt)
                .where(
                    GlobalDiscoveryRecoveryAttempt.run_id
                    == "replacement-after-tuple-change"
                )
            ) == 0
    finally:
        revoker.release.set()
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_prepared_cancellation_t2_rejects_changed_intent_tuple(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BlockingRevoker(_PreparedRevoker):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()

        def is_prepared_revoked(self, **kwargs) -> bool:
            self.entered.set()
            assert self.release.wait(timeout=3)
            return super().is_prepared_revoked(**kwargs)

    revoker = BlockingRevoker()
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "prepared-cancel-tuple-change.db",
        prepared_revoker=revoker,
    )
    caplog.set_level(
        "WARNING",
        logger="okto_pulse.community.global_discovery_recovery_worker",
    )
    try:
        prepared = _prepare_recovery(
            store,
            "prepared-cancel-tuple-change",
        )
        cancellation = asyncio.create_task(
            asyncio.to_thread(
                store.cancel_prepared,
                run_id=prepared.run_id,
                expected_epoch=prepared.epoch,
                requested_at=NOW + timedelta(seconds=2),
                requested_by_actor_id="original-canceller",
                reason="canonical cancel tuple",
            )
        )
        assert await asyncio.to_thread(revoker.entered.wait, 2)
        with sync_engine.begin() as connection:
            changed = connection.execute(
                update(GlobalDiscoveryRecoveryAttempt)
                .where(
                    GlobalDiscoveryRecoveryAttempt.run_id
                    == prepared.run_id,
                    GlobalDiscoveryRecoveryAttempt.epoch
                    == prepared.epoch,
                )
                .values(
                    cancel_requested_by_actor_id="competing-canceller"
                )
            )
            assert changed.rowcount == 1

        revoker.release.set()
        await asyncio.wait_for(cancellation, timeout=2)
        deadline = time.monotonic() + 2
        while (
            store._revocation_threads
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.02)
        assert store._revocation_threads == {}
        observed = store.get_status(run_id=prepared.run_id)
        assert observed is not None
        assert observed.state.value == "pending"
        assert observed.phase.value == "prepared"
        assert observed.cancel_requested_by_actor_id == "competing-canceller"
        with sync_engine.connect() as connection:
            assert connection.scalar(
                select(func.count()).select_from(
                    GlobalDiscoveryRecoverySlot
                )
            ) == 1
        assert "RecoveryStoreSchemaError" in caplog.text
    finally:
        revoker.release.set()
        store.close(timeout_seconds=1)
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_confirmation_winner_is_never_revoked_by_later_expiry_admission(
    tmp_path: Path,
) -> None:
    revoker = _PreparedRevoker()
    clock = [NOW]
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "confirmation-wins-expiry-race.db",
        prepared_revoker=revoker,
        wall_clock=lambda: clock[0],
    )
    try:
        prepared = _prepare_recovery(store, "confirmation-wins-expiry")
        clock[0] = NOW + timedelta(seconds=300)
        confirmed = store.enqueue_execution(
            RecoveryStartCommand(
                binding=replace(
                    prepared.binding,
                    confirmation_fingerprint="sha256:confirmation-wins",
                    reason="confirmation wins before expiry",
                ),
                started_at=clock[0],
                counts=prepared.counts,
                attempt_budget_ms=60_000,
                expected_epoch=prepared.epoch,
                confirmed_by_actor_id="confirmation-winner",
                confirmation_consumed_at=clock[0],
            )
        )
        assert confirmed.phase.value == "confirmed"

        clock[0] = NOW + timedelta(seconds=302)
        with pytest.raises(RecoveryInProgress):
            store.admit_preparation(
                _command("must-not-revoke-confirmed-attempt")
            )
        assert revoker.revoked == set()
        observed = store.get_status(run_id=confirmed.run_id)
        assert observed is not None
        assert observed.phase.value == "confirmed"
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_permanent_settlement_error_keeps_fail_closed_slot_and_telemetry(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class PermanentlyInvalidRevoker(_PreparedRevoker):
        def is_prepared_revoked(self, **_kwargs) -> bool:
            raise RecoveryStoreSchemaError(
                code="test_permanent_revocation_schema_error"
            )

    revoker = PermanentlyInvalidRevoker()
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "permanent-settlement-error.db",
        prepared_revoker=revoker,
    )
    caplog.set_level(
        "ERROR",
        logger="okto_pulse.community.global_discovery_recovery_worker",
    )
    try:
        queued, _ = store.admit_preparation(
            _command("permanent-settlement-error")
        )
        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="permanent-settlement-worker",
            claimed_at=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        assert claim is not None
        preparing = store.mark_preparing(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            at=NOW,
        )
        manifest_ref = "manifest://permanent-settlement-error"
        terminal = store.record_preparation_failure(
            dispatch_id=claim.dispatch_id,
            claim_token=claim.claim_token,
            failed_at=NOW + timedelta(seconds=2),
            active_elapsed_ms=2_000,
            counts=replace(preparing.counts, errors=1),
            reason_code="permanent_settlement_test_failure",
            retryable=False,
            retry_available_at=NOW + timedelta(seconds=3),
            max_attempts=3,
            manifest_ref=manifest_ref,
        )
        assert terminal.state.value == "running"
        assert terminal.phase.value == "preparing"
        assert store._preparation_settlement_threads == {}
        with sync_engine.connect() as connection:
            assert connection.scalar(
                select(GlobalDiscoveryRecoveryDispatch.state)
            ) == "settling"
            assert connection.scalar(
                select(func.count()).select_from(
                    GlobalDiscoveryRecoverySlot
                )
            ) == 1
        blocked_records = [
            record
            for record in caplog.records
            if "preparation settlement blocked" in record.getMessage()
        ]
        assert len(blocked_records) == 1
        assert blocked_records[0].retryable is False
        assert blocked_records[0].operator_action_required is True
    finally:
        store.close(timeout_seconds=1)
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_permanent_handoff_conflict_terminalizes_and_releases_tick_fence(
    tmp_path: Path,
) -> None:
    def conflicting_handoff(_previous, _resumed) -> None:
        raise RecoveryRuntimeConfigurationError(
            code="recovery_resume_source_inputs_binding_conflict"
        )

    sync_engine, async_engine, factory, store = await _database(
        tmp_path / "resume-permanent-conflict.db",
        resume_input_handoff=conflicting_handoff,
    )
    adapter = CommunitySqlAlchemyRelationalEffects()
    _register_tick_runtime(adapter)
    try:
        failed = _prepare_and_fail_recovery(store, "resume-permanent-conflict")
        with pytest.raises(RecoveryRuntimeConfigurationError):
            store.admit_explicit_resume(
                run_id=failed.run_id,
                expected_epoch=failed.epoch,
                requested_at=NOW + timedelta(seconds=5),
                requested_by_actor_id="resume-operator",
                reason="permanent input conflict",
            )

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            terminal = store.get_status(run_id=failed.run_id)
            assert terminal is not None
            if terminal.state.value == "failed" and terminal.epoch == 2:
                break
            await asyncio.sleep(0.02)
        assert terminal.state.value == "failed"
        assert terminal.reason_code == "recovery_resume_materialization_conflict"

        with sync_engine.connect() as connection:
            slot_count = int(
                connection.scalar(
                    select(func.count()).select_from(GlobalDiscoveryRecoverySlot)
                )
                or 0
            )
            dispatch = (
                connection.execute(
                    select(GlobalDiscoveryRecoveryDispatch.__table__).where(
                        GlobalDiscoveryRecoveryDispatch.run_id == failed.run_id,
                        GlobalDiscoveryRecoveryDispatch.epoch == 2,
                    )
                )
                .mappings()
                .one()
            )
        assert slot_count == 0
        assert dispatch["state"] == "done"
        assert dispatch["result_payload"]["retryable"] is False

        async with factory() as session:
            await publish_tick_events(session, board_id=BOARD_ID)
            await session.commit()
    finally:
        store.close(timeout_seconds=1)
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_store_close_wakes_and_drains_resume_reconciler(
    tmp_path: Path,
) -> None:
    def transient_failure(_previous, _resumed) -> None:
        raise OSError("transient artifact store outage")

    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "resume-reconciler-close.db",
        resume_input_handoff=transient_failure,
    )
    try:
        failed = _prepare_and_fail_recovery(store, "resume-reconciler-close")
        with pytest.raises(OSError):
            store.admit_explicit_resume(
                run_id=failed.run_id,
                expected_epoch=failed.epoch,
                requested_at=NOW + timedelta(seconds=5),
                requested_by_actor_id="resume-operator",
                reason="close owned retry",
            )
        deadline = time.monotonic() + 1
        while (
            not store._resume_reconciliation_threads
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert store._resume_reconciliation_threads

        store.close(timeout_seconds=1)
        assert store._resume_reconciliation_threads == {}
    finally:
        store.close(timeout_seconds=1)
        await async_engine.dispose()
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_store_close_wakes_and_drains_preparation_settlement_reconciler(
    tmp_path: Path,
) -> None:
    class TransientlyFailingRevoker(_PreparedRevoker):
        def is_prepared_revoked(self, **_kwargs) -> bool:
            raise OSError("transient manifest backend outage")

    revoker = TransientlyFailingRevoker()
    sync_engine, async_engine, _factory, store = await _database(
        tmp_path / "settlement-reconciler-close.db",
        prepared_revoker=revoker,
    )
    try:
        queued, _ = store.admit_preparation(
            _command("settlement-reconciler-close")
        )
        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="settlement-close-worker",
            claimed_at=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        assert claim is not None
        preparing = store.mark_preparing(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            at=NOW,
        )
        store.record_preparation_failure(
            dispatch_id=claim.dispatch_id,
            claim_token=claim.claim_token,
            failed_at=NOW + timedelta(seconds=2),
            active_elapsed_ms=2_000,
            counts=replace(preparing.counts, errors=1),
            reason_code="transient_settlement_test_failure",
            retryable=False,
            retry_available_at=NOW + timedelta(seconds=3),
            max_attempts=3,
            manifest_ref="manifest://settlement-reconciler-close",
        )
        assert store._preparation_settlement_threads

        store.close(timeout_seconds=1)
        assert store._preparation_settlement_threads == {}
        assert store._resume_reconciliation_threads == {}
        assert store._revocation_threads == {}
    finally:
        if store._preparation_settlement_threads:
            store.close(timeout_seconds=1)
        await async_engine.dispose()
        sync_engine.dispose()
