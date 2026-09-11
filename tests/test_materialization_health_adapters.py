from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import okto_pulse.community.adapters.materialization_health as materialization_module
from okto_pulse.community.adapters.materialization_health import (
    INITIAL_MATERIALIZATION_GENERATION,
    CommunityMaterializationGenerationStore,
    CommunitySqlAlchemyMaterializationCensus,
    materialization_generation_key,
)
from okto_pulse.community.adapters.sqlalchemy_audit_repo import (
    CommunityAuditRepository,
    _is_sqlite_write_contention,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    AppSetting,
    Base,
    Board,
    Card,
    ConsolidationAudit,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    DomainEventRow,
    GlobalUpdateOutbox,
    Spec,
)
from okto_pulse.core.kg.health_state import HealthState, MetricStatus
from okto_pulse.core.kg.interfaces.audit_dtos import (
    ConsolidationAuditData,
    OutboxEventData,
)
from okto_pulse.core.kg.interfaces.audit_repository import AuditWriteContention
from okto_pulse.core.kg.materialization_health import (
    CensusStatus,
    HealthProbeDeadline,
    MaterializationHealthBaseline,
)
from okto_pulse.core.ports.global_outbox import (
    GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
    GLOBAL_OUTBOX_MAX_RETRIES,
)


async def _database(
    tmp_path: Path,
    name: str,
) -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(*, board_id: str, session_id: str) -> ConsolidationAuditData:
    now = _now()
    return ConsolidationAuditData(
        session_id=session_id,
        board_id=board_id,
        artifact_id="artifact-1",
        artifact_type="card",
        agent_id="agent-1",
        started_at=now,
        committed_at=now,
        nodes_added=1,
    )


def _outbox(*, board_id: str, session_id: str, event_id: str) -> OutboxEventData:
    return OutboxEventData(
        event_id=event_id,
        board_id=board_id,
        session_id=session_id,
        event_type="consolidation_committed",
        payload={"artifact_id": "artifact-1"},
    )


def _baseline() -> MaterializationHealthBaseline:
    return MaterializationHealthBaseline(
        graph_state=HealthState.AT_RISK,
        discovery_state=HealthState.HEALTHY,
        overall_state=HealthState.AT_RISK,
        metric_status=MetricStatus.UNAVAILABLE,
    )


class _ControllableGlobalOpenRuntime:
    def __init__(self, *, corruption: bool, failures: int = 1) -> None:
        self.corruption = corruption
        self.failures_remaining = failures

    def open_global_kuzu_db(
        self,
        _path: Path,
        *,
        on_corruption=None,
    ) -> object:
        if self.failures_remaining:
            self.failures_remaining -= 1
            failure = RuntimeError("invalid graph fixture")
            if self.corruption and on_corruption is not None:
                on_corruption(failure)
            raise failure
        return object()

    def is_ladybug_corruption_error(self, _exc: BaseException) -> bool:
        return self.corruption


@pytest.mark.asyncio
async def test_relational_census_is_exact_and_board_scoped(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path, "census.db")
    board_id = "board-census"
    other_board_id = "board-other"
    try:
        async with factory() as session:
            session.add_all(
                [
                    Board(id=board_id, name="Census", owner_id="owner"),
                    Board(id=other_board_id, name="Other", owner_id="owner"),
                    Card(
                        id="card-census",
                        board_id=board_id,
                        title="source",
                        created_by="owner",
                    ),
                    Spec(
                        id="spec-census",
                        board_id=board_id,
                        title="spec source",
                        created_by="owner",
                        decisions=[
                            {"id": "decision-active", "title": "Active"},
                            {
                                "id": "decision-inactive",
                                "title": "Inactive",
                                "status": "superseded",
                            },
                            {"id": "decision-empty", "status": "active"},
                        ],
                    ),
                    Card(
                        id="card-other",
                        board_id=other_board_id,
                        title="other source",
                        created_by="owner",
                    ),
                    ConsolidationQueue(
                        id="queue-pending",
                        board_id=board_id,
                        artifact_type="card",
                        artifact_id="artifact-pending",
                        status="pending",
                    ),
                    ConsolidationQueue(
                        id="queue-claimed",
                        board_id=board_id,
                        artifact_type="card",
                        artifact_id="artifact-claimed",
                        status="claimed",
                    ),
                    ConsolidationQueue(
                        id="queue-done",
                        board_id=board_id,
                        artifact_type="card",
                        artifact_id="artifact-done",
                        status="done",
                    ),
                    ConsolidationDeadLetter(
                        id="dlq-board",
                        board_id=board_id,
                        artifact_type="card",
                        artifact_id="artifact-dlq",
                        attempts=3,
                    ),
                    ConsolidationDeadLetter(
                        id="dlq-other",
                        board_id=other_board_id,
                        artifact_type="card",
                        artifact_id="artifact-other-dlq",
                        attempts=3,
                    ),
                    GlobalUpdateOutbox(
                        id="outbox-active",
                        event_id="event-active",
                        board_id=board_id,
                        session_id="session-active",
                        event_type="consolidation_committed",
                        payload={},
                        retry_count=0,
                    ),
                    GlobalUpdateOutbox(
                        id="outbox-terminal",
                        event_id="event-terminal",
                        board_id=board_id,
                        session_id="session-terminal",
                        event_type="consolidation_committed",
                        payload={},
                        retry_count=GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
                    ),
                    GlobalUpdateOutbox(
                        id="outbox-other-terminal",
                        event_id="event-other-terminal",
                        board_id=other_board_id,
                        session_id="session-other-terminal",
                        event_type="consolidation_committed",
                        payload={},
                        retry_count=GLOBAL_OUTBOX_MAX_RETRIES,
                    ),
                ]
            )
            await session.commit()

        census = await CommunitySqlAlchemyMaterializationCensus(factory).snapshot(
            board_id,
            generation="generation-census",
            deadline=HealthProbeDeadline(time.monotonic() + 2.0),
        )

        assert census.status is CensusStatus.AVAILABLE
        assert census.generation == "generation-census"
        # Card + Spec + one active, non-empty embedded decision.
        assert census.source_count == 3
        # Consolidation pending + claimed; done is not active.
        assert census.queue_depth == 2
        # Active consolidation rows plus the retry-window global outbox row.
        assert census.active_queue_count == 3
        assert census.dead_letter_count == 1
        assert census.global_outbox_dead_letter_count == 1
        assert census.reason_code == "board_census_available"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relational_census_returns_typed_timeout_without_sql(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, "expired-census.db")
    try:
        census = await CommunitySqlAlchemyMaterializationCensus(factory).snapshot(
            "board-expired",
            generation="generation-expired",
            deadline=HealthProbeDeadline(time.monotonic() - 0.001),
        )
        assert census.status is CensusStatus.UNAVAILABLE
        assert census.reason_code == "board_census_timeout"
        assert all(value is None for value in census.counts.values())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relational_census_cancellation_drains_checked_out_session(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, "cancelled-census.db")
    query_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class BlockingSessionScope:
        def __init__(self) -> None:
            self._scope = factory()
            self._session = None

        async def __aenter__(self):
            self._session = await self._scope.__aenter__()
            # Hold a real QueuePool checkout before the modeled long query.
            await self._session.connection()
            return self

        async def execute(self, _statement):
            query_started.set()
            await asyncio.Event().wait()

        def in_transaction(self) -> bool:
            assert self._session is not None
            return bool(self._session.in_transaction())

        async def rollback(self) -> None:
            assert self._session is not None
            cleanup_started.set()
            await allow_cleanup.wait()
            await self._session.rollback()

        async def __aexit__(self, *exc_info) -> None:
            try:
                await self._scope.__aexit__(*exc_info)
            finally:
                cleanup_finished.set()

    census = CommunitySqlAlchemyMaterializationCensus(BlockingSessionScope)
    task = asyncio.create_task(
        census.snapshot(
            "board-cancelled",
            generation="generation-cancelled",
            deadline=HealthProbeDeadline(time.monotonic() + 30),
        )
    )
    try:
        await asyncio.wait_for(query_started.wait(), timeout=2)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        # Model the overlapping outer-gather/stage timeout cancellation from
        # recovery preparation while rollback/close is already in progress.
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert engine.sync_engine.pool.checkedout() == 1

        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert cleanup_finished.is_set()
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        allow_cleanup.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_resistant_cleanup_is_bounded_through_real_asyncio_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        materialization_module,
        "_CENSUS_SESSION_CLEANUP_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        materialization_module,
        "_CENSUS_SESSION_CLEANUP_CANCEL_DRAIN_TIMEOUT_SECONDS",
        0.2,
    )
    database_path = tmp_path / "asyncio-run-resistant-cleanup.db"
    scenario_ready = threading.Event()

    def run_scenario() -> tuple[float, int, bool]:
        async def scenario() -> tuple[float, int, bool]:
            engine, factory = await _database(
                database_path.parent,
                database_path.name,
            )
            query_started = asyncio.Event()
            cleanup_started = asyncio.Event()
            cleanup_finished = asyncio.Event()

            class CancellationResistantScope:
                def __init__(self) -> None:
                    self._scope = factory()
                    self._session = None

                async def __aenter__(self):
                    self._session = await self._scope.__aenter__()
                    await self._session.connection()
                    return self

                async def execute(self, _statement):
                    query_started.set()
                    await asyncio.Event().wait()

                def in_transaction(self) -> bool:
                    assert self._session is not None
                    return bool(self._session.in_transaction())

                async def rollback(self) -> None:
                    assert self._session is not None
                    cleanup_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        # Deliberately suppress the first teardown cancellation,
                        # then complete the real database rollback.
                        await self._session.rollback()

                async def __aexit__(self, *exc_info) -> None:
                    try:
                        await self._scope.__aexit__(*exc_info)
                    finally:
                        cleanup_finished.set()

            census = CommunitySqlAlchemyMaterializationCensus(
                CancellationResistantScope
            )
            task = asyncio.create_task(
                census.snapshot(
                    "board-resistant-cleanup",
                    generation="generation-resistant-cleanup",
                    deadline=HealthProbeDeadline(time.monotonic() + 30),
                )
            )
            started = time.monotonic()
            scenario_ready.set()
            try:
                await asyncio.wait_for(query_started.wait(), timeout=1)
                task.cancel()
                await asyncio.wait_for(cleanup_started.wait(), timeout=1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
                return (
                    time.monotonic() - started,
                    engine.sync_engine.pool.checkedout(),
                    cleanup_finished.is_set(),
                )
            finally:
                if not task.done():
                    task.cancel()
                await engine.dispose()

        return asyncio.run(scenario())

    scenario_task = asyncio.create_task(asyncio.to_thread(run_scenario))
    # Schema creation is test-fixture setup, not part of the cancellation
    # boundary under test. Wait for the inner loop to finish that setup before
    # starting the unchanged one-second cleanup budget.
    assert await asyncio.to_thread(scenario_ready.wait, 5.0)
    elapsed, checked_out, cleanup_finished = await asyncio.wait_for(
        scenario_task,
        timeout=1,
    )
    assert elapsed < 0.5
    assert checked_out == 0
    assert cleanup_finished is True


@pytest.mark.asyncio
async def test_normal_commit_advances_generation_before_ack_and_rolls_back_on_failure(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, "generation.db")
    board_id = "board-generation"
    generation_store = CommunityMaterializationGenerationStore(factory)
    repository = CommunityAuditRepository(factory)
    try:
        async with factory() as session:
            session.add(Board(id=board_id, name="Generation", owner_id="owner"))
            await session.commit()

        assert await generation_store.current(board_id) == (
            INITIAL_MATERIALIZATION_GENERATION
        )
        await repository.commit_consolidation_records(
            _audit(board_id=board_id, session_id="session-generation"),
            [],
            _outbox(
                board_id=board_id,
                session_id="session-generation",
                event_id="event-generation",
            ),
        )
        committed_generation = await generation_store.current(board_id)
        assert committed_generation != INITIAL_MATERIALIZATION_GENERATION

        async with factory() as session:
            outbox = (
                await session.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id == "event-generation"
                    )
                )
            ).scalar_one()
            generation_event = (
                await session.execute(
                    select(DomainEventRow).where(
                        DomainEventRow.event_type
                        == "kg.materialization_generation_advanced",
                        DomainEventRow.board_id == board_id,
                    )
                )
            ).scalar_one()
        assert outbox.payload == {"artifact_id": "artifact-1"}
        assert generation_event.payload_json["materialization_generation"] == (
            committed_generation
        )
        assert generation_event.payload_json["previous_materialization_generation"] == (
            INITIAL_MATERIALIZATION_GENERATION
        )
        assert generation_event.payload_json["correlation_id"] == ("session-generation")

        # Duplicate audit PK fails the whole transaction. The generation update
        # attempted in that transaction must not escape the rollback.
        with pytest.raises(IntegrityError):
            await repository.commit_consolidation_records(
                _audit(board_id=board_id, session_id="session-generation"),
                [],
                _outbox(
                    board_id=board_id,
                    session_id="session-generation",
                    event_id="event-generation-rollback",
                ),
            )
        assert await generation_store.current(board_id) == committed_generation
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_borrowed_audit_transaction_reuses_locked_writer_and_obeys_owner_commit(
    tmp_path: Path,
) -> None:
    """The worker-held SQLite writer transaction owns audit and outbox ACK."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'borrowed-audit.db'}",
        connect_args={"timeout": 0.05},
    )
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory_calls = 0

    def tracked_factory():
        nonlocal factory_calls
        factory_calls += 1
        return factory()

    logged_correlations: list[str | None] = []

    class RecordingGenerationStore(CommunityMaterializationGenerationStore):
        def log_advanced(self, advance) -> None:
            logged_correlations.append(advance.correlation_id)

    board_id = "board-borrowed-audit"
    repository = CommunityAuditRepository(
        tracked_factory,
        materialization_generation_store=RecordingGenerationStore(tracked_factory),
    )
    try:
        async with factory() as session:
            session.add(Board(id=board_id, name="Before", owner_id="owner"))
            await session.commit()

        # This unrelated worker mutation acquires SQLite's writer lock. Before
        # the fix the repository opened a second writer and failed fast with
        # ``database is locked`` under the 50 ms timeout above.
        async with factory() as owner:
            board = await owner.get(Board, board_id)
            assert board is not None
            board.name = "Rolled back"
            await owner.flush()

            await repository.stage_consolidation_records(
                owner,
                _audit(board_id=board_id, session_id="session-borrowed-rollback"),
                [],
                _outbox(
                    board_id=board_id,
                    session_id="session-borrowed-rollback",
                    event_id="event-borrowed-rollback",
                ),
            )

            assert factory_calls == 0
            assert (
                await owner.get(ConsolidationAudit, "session-borrowed-rollback")
                is not None
            )
            assert (
                await owner.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id == "event-borrowed-rollback"
                    )
                )
            ).scalar_one_or_none() is not None
            await owner.rollback()
            assert logged_correlations == []

        async with factory() as observer:
            board = await observer.get(Board, board_id)
            assert board is not None and board.name == "Before"
            assert (
                await observer.get(ConsolidationAudit, "session-borrowed-rollback")
                is None
            )
            assert (
                await observer.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id == "event-borrowed-rollback"
                    )
                )
            ).scalar_one_or_none() is None
            assert (
                await observer.get(AppSetting, materialization_generation_key(board_id))
                is None
            )
            generation_events = (
                (
                    await observer.execute(
                        select(DomainEventRow).where(
                            DomainEventRow.board_id == board_id,
                            DomainEventRow.event_type
                            == "kg.materialization_generation_advanced",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert generation_events == []

        async with factory() as owner:
            await repository.stage_consolidation_records(
                owner,
                _audit(board_id=board_id, session_id="session-borrowed-commit"),
                [],
                _outbox(
                    board_id=board_id,
                    session_id="session-borrowed-commit",
                    event_id="event-borrowed-commit",
                ),
            )
            assert factory_calls == 0
            assert logged_correlations == []
            await owner.commit()
            assert logged_correlations == ["session-borrowed-commit"]

        async with factory() as observer:
            assert (
                await observer.get(ConsolidationAudit, "session-borrowed-commit")
                is not None
            )
            assert (
                await observer.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id == "event-borrowed-commit"
                    )
                )
            ).scalar_one_or_none() is not None
            assert (
                await observer.get(AppSetting, materialization_generation_key(board_id))
                is not None
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_self_owned_audit_maps_real_sqlite_contention_to_stable_port_error(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'audit-contention.db'}",
        connect_args={"timeout": 0.05},
    )
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    board_id = "board-audit-contention"
    repository = CommunityAuditRepository(factory)
    try:
        async with factory() as session:
            session.add(Board(id=board_id, name="Before", owner_id="owner"))
            await session.commit()

        async with factory() as lock_owner:
            board = await lock_owner.get(Board, board_id)
            assert board is not None
            board.name = "Writer lock held"
            await lock_owner.flush()

            with pytest.raises(AuditWriteContention) as caught:
                await repository.commit_consolidation_records(
                    _audit(board_id=board_id, session_id="session-contended"),
                    [],
                    _outbox(
                        board_id=board_id,
                        session_id="session-contended",
                        event_id="event-contended",
                    ),
                )
            assert caught.value.code == "audit_write_contention"
            assert caught.value.retryable is True
            assert isinstance(caught.value.__cause__, OperationalError)
            await lock_owner.rollback()

        async with factory() as observer:
            assert await observer.get(ConsolidationAudit, "session-contended") is None
            assert (
                await observer.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id == "event-contended"
                    )
                )
            ).scalar_one_or_none() is None

        await repository.commit_consolidation_records(
            _audit(board_id=board_id, session_id="session-after-contention"),
            [],
            _outbox(
                board_id=board_id,
                session_id="session-after-contention",
                event_id="event-after-contention",
            ),
        )
        async with factory() as observer:
            assert (
                await observer.get(ConsolidationAudit, "session-after-contention")
                is not None
            )
    finally:
        await engine.dispose()


def test_audit_contention_text_from_non_sqlite_driver_is_not_mapped() -> None:
    error = OperationalError(
        "opaque statement",
        {},
        RuntimeError("database is locked"),
    )

    assert _is_sqlite_write_contention(error) is False
