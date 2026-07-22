"""Edge coverage for the Community governed-takedown telemetry adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    GlobalDiscoveryDeliveryLedger,
    KGTakedownStateEvent,
)
from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
    CommunitySqlAlchemyTakedownTelemetry,
    read_takedown_aggregates,
    stage_takedown_transition,
)
from okto_pulse.core.ports.delivery_ledger import (
    DeliveryCircuitSnapshot,
    DeliveryState,
)
from okto_pulse.core.ports.takedown_telemetry import (
    TakedownState,
    TakedownTelemetryQuery,
    TakedownTransition,
)


NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)
BOARD_ID = "a1111111-1111-4111-8111-111111111111"
OTHER_BOARD_ID = "a2222222-2222-4222-8222-222222222222"


@pytest_asyncio.fixture
async def telemetry_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'card9-telemetry-edges.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Board(id=BOARD_ID, name="Card 9 edges", owner_id="tester"))
        await session.commit()
    try:
        yield SimpleNamespace(engine=engine, sessions=sessions)
    finally:
        await engine.dispose()


def _identity(index: int) -> tuple[str, str, str]:
    artifact_id = f"b{index:07d}-2222-4222-8222-222222222222"
    delete_event_id = f"delete-card9-edge-{index}"
    delivery_key = f"gd_parity:{BOARD_ID}:spec:{artifact_id}:1"
    return artifact_id, delete_event_id, delivery_key


def _ledger(
    index: int,
    *,
    state: DeliveryState,
    attempt: int,
    updated_at: datetime,
) -> GlobalDiscoveryDeliveryLedger:
    artifact_id, delete_event_id, delivery_key = _identity(index)
    return GlobalDiscoveryDeliveryLedger(
        delivery_key=delivery_key,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=artifact_id,
        generation=1,
        delete_event_id=delete_event_id,
        state=state.value,
        attempt=attempt,
        attempt_event_key=(
            f"{delivery_key}:attempt:{attempt}"
            if state in {DeliveryState.OUTBOX_PERSISTED, DeliveryState.DELIVERED}
            or attempt > 0
            else None
        ),
        last_error=(
            "delivery failed" if state is DeliveryState.DELIVERY_DEBT else None
        ),
        next_retry_at=(updated_at if state is DeliveryState.DELIVERY_DEBT else None),
        created_at=updated_at - timedelta(hours=1),
        updated_at=updated_at,
        delivered_at=(updated_at if state is DeliveryState.DELIVERED else None),
    )


def _transition(
    index: int,
    state: TakedownState,
    *,
    occurred_at: datetime,
    attempt: int | None = None,
) -> TakedownTransition:
    artifact_id, delete_event_id, delivery_key = _identity(index)
    return TakedownTransition(
        delete_event_id=delete_event_id,
        delivery_key=(None if state is TakedownState.INTENT_CREATED else delivery_key),
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=artifact_id,
        generation=1,
        state=state,
        occurred_at=occurred_at,
        attempt=attempt,
    )


@pytest.mark.asyncio
async def test_backlog_is_current_and_oldest_open_debt_survives_redrives(
    telemetry_db,
) -> None:
    async with telemetry_db.sessions() as session:
        session.add_all(
            (
                _ledger(
                    1,
                    state=DeliveryState.DELIVERY_DEBT,
                    attempt=2,
                    updated_at=NOW - timedelta(minutes=1),
                ),
                _ledger(
                    2,
                    state=DeliveryState.DELIVERED,
                    attempt=1,
                    updated_at=NOW - timedelta(minutes=2),
                ),
                _ledger(
                    3,
                    state=DeliveryState.OUTBOX_PERSISTED,
                    attempt=1,
                    updated_at=NOW - timedelta(minutes=3),
                ),
            )
        )
        # Only delivery 1 remains debt. Its mutable owner was advanced twice,
        # but age must retain the first immutable debt observation.
        await stage_takedown_transition(
            session,
            _transition(
                1,
                TakedownState.DELIVERY_DEBT,
                occurred_at=NOW - timedelta(hours=30),
                attempt=0,
            ),
        )
        await stage_takedown_transition(
            session,
            _transition(
                1,
                TakedownState.OUTBOX_PERSISTED,
                occurred_at=NOW - timedelta(hours=20),
                attempt=1,
            ),
        )
        await stage_takedown_transition(
            session,
            _transition(
                1,
                TakedownState.OUTBOX_PERSISTED,
                occurred_at=NOW - timedelta(minutes=5),
                attempt=2,
            ),
        )
        await stage_takedown_transition(
            session,
            _transition(
                1,
                TakedownState.DELIVERY_DEBT,
                occurred_at=NOW - timedelta(minutes=1),
                attempt=2,
            ),
        )
        await stage_takedown_transition(
            session,
            _transition(
                1,
                TakedownState.DELIVERY_DEBT,
                occurred_at=NOW - timedelta(hours=10),
                attempt=1,
            ),
        )
        # Historical debt for a delivered owner must not enter current backlog
        # or oldest-open-debt age.
        await stage_takedown_transition(
            session,
            _transition(
                2,
                TakedownState.DELIVERY_DEBT,
                occurred_at=NOW - timedelta(hours=40),
                attempt=0,
            ),
        )
        await session.commit()

    async with telemetry_db.sessions() as session:
        aggregates = await read_takedown_aggregates(
            session,
            now=NOW,
            circuit_reader=None,
            board_id=BOARD_ID,
        )

    assert aggregates.delivery_debt_backlog == 1
    assert aggregates.oldest_debt_age_seconds == 30 * 60 * 60


@pytest.mark.asyncio
async def test_reopened_debt_age_starts_after_the_latest_delivery(
    telemetry_db,
) -> None:
    async with telemetry_db.sessions() as session:
        session.add(
            _ledger(
                4,
                state=DeliveryState.DELIVERY_DEBT,
                attempt=2,
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        for state, occurred_at, attempt in (
            (TakedownState.DELIVERY_DEBT, NOW - timedelta(hours=40), 0),
            (TakedownState.DELIVERED, NOW - timedelta(hours=1), 1),
            (TakedownState.DELIVERY_DEBT, NOW - timedelta(minutes=5), 2),
        ):
            await stage_takedown_transition(
                session,
                _transition(
                    4,
                    state,
                    occurred_at=occurred_at,
                    attempt=attempt,
                ),
            )
        await session.commit()

    async with telemetry_db.sessions() as session:
        aggregates = await read_takedown_aggregates(
            session,
            now=NOW,
            circuit_reader=None,
            board_id=BOARD_ID,
        )

    assert aggregates.delivery_debt_backlog == 1
    assert aggregates.oldest_debt_age_seconds == 5 * 60


@pytest.mark.asyncio
async def test_oldest_debt_ignores_divergent_identity_for_the_same_key(
    telemetry_db,
) -> None:
    artifact_id, delete_event_id, delivery_key = _identity(5)
    divergent_artifact_id = "divergent-artifact"
    divergent_delete_event_id = "divergent-delete-event"
    async with telemetry_db.sessions() as session:
        session.add(
            _ledger(
                5,
                state=DeliveryState.DELIVERY_DEBT,
                attempt=2,
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        await stage_takedown_transition(
            session,
            _transition(
                5,
                TakedownState.DELIVERY_DEBT,
                occurred_at=NOW - timedelta(minutes=5),
                attempt=2,
            ),
        )
        for state, occurred_at, attempt in (
            (TakedownState.DELIVERED, NOW - timedelta(hours=45), 0),
            (TakedownState.DELIVERY_DEBT, NOW - timedelta(hours=40), 1),
        ):
            await stage_takedown_transition(
                session,
                TakedownTransition(
                    delete_event_id=divergent_delete_event_id,
                    delivery_key=delivery_key,
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id=divergent_artifact_id,
                    generation=1,
                    state=state,
                    occurred_at=occurred_at,
                    attempt=attempt,
                ),
            )
        await session.commit()

    async with telemetry_db.sessions() as session:
        aggregates = await read_takedown_aggregates(
            session,
            now=NOW,
            circuit_reader=None,
            board_id=BOARD_ID,
        )

    assert artifact_id != divergent_artifact_id
    assert delete_event_id != divergent_delete_event_id
    assert aggregates.delivery_debt_backlog == 1
    assert aggregates.oldest_debt_age_seconds == 5 * 60


@pytest.mark.asyncio
async def test_p95_uses_nearest_rank_and_delivered_at_window_boundaries(
    telemetry_db,
) -> None:
    async with telemetry_db.sessions() as session:
        for index in range(1, 21):
            delivered_at = (
                NOW - timedelta(hours=1)
                if index == 20
                else NOW - timedelta(seconds=index)
            )
            await stage_takedown_transition(
                session,
                _transition(
                    100 + index,
                    TakedownState.INTENT_CREATED,
                    occurred_at=delivered_at - timedelta(seconds=index),
                ),
            )
            await stage_takedown_transition(
                session,
                _transition(
                    100 + index,
                    TakedownState.DELIVERED,
                    occurred_at=delivered_at,
                    attempt=0,
                ),
            )
        for index, delivered_at in (
            (201, NOW - timedelta(hours=1, seconds=1)),
            (202, NOW + timedelta(seconds=1)),
        ):
            await stage_takedown_transition(
                session,
                _transition(
                    index,
                    TakedownState.INTENT_CREATED,
                    occurred_at=delivered_at - timedelta(seconds=999),
                ),
            )
            await stage_takedown_transition(
                session,
                _transition(
                    index,
                    TakedownState.DELIVERED,
                    occurred_at=delivered_at,
                    attempt=0,
                ),
            )
        await session.commit()

    async with telemetry_db.sessions() as session:
        aggregates = await read_takedown_aggregates(
            session,
            now=NOW,
            circuit_reader=None,
            board_id=BOARD_ID,
        )

    assert aggregates.p95_sample_count == 20
    # nearest-rank p95 for sorted [1..20]: ceil(.95 * 20) == rank 19
    assert aggregates.p95_seconds_1h == pytest.approx(19.0, abs=0.001)


@pytest.mark.asyncio
async def test_p95_counts_only_the_latest_delivery_per_delete_event(
    telemetry_db,
) -> None:
    transitions = (
        _transition(
            250,
            TakedownState.INTENT_CREATED,
            occurred_at=NOW - timedelta(seconds=50),
        ),
        _transition(
            250,
            TakedownState.DELIVERED,
            occurred_at=NOW - timedelta(seconds=5),
            attempt=0,
        ),
        _transition(
            250,
            TakedownState.DELIVERED,
            occurred_at=NOW - timedelta(seconds=30),
            attempt=1,
        ),
    )
    async with telemetry_db.sessions() as session:
        for transition in transitions:
            await stage_takedown_transition(session, transition)
        await session.commit()

    async with telemetry_db.sessions() as session:
        aggregates = await read_takedown_aggregates(
            session,
            now=NOW,
            circuit_reader=None,
            board_id=BOARD_ID,
        )

    assert aggregates.p95_sample_count == 1
    assert aggregates.p95_seconds_1h == pytest.approx(20.0, abs=0.001)


@pytest.mark.asyncio
async def test_empty_p95_window_is_undefined_not_zero(telemetry_db) -> None:
    async with telemetry_db.sessions() as session:
        aggregates = await read_takedown_aggregates(
            session,
            now=NOW,
            circuit_reader=None,
            board_id=BOARD_ID,
        )

    assert aggregates.p95_sample_count == 0
    assert aggregates.p95_seconds_1h is None


@pytest.mark.asyncio
async def test_delivery_selector_recovers_unkeyed_intent(telemetry_db) -> None:
    _, delete_event_id, delivery_key = _identity(301)
    async with telemetry_db.sessions() as session:
        await stage_takedown_transition(
            session,
            _transition(
                301,
                TakedownState.INTENT_CREATED,
                occurred_at=NOW - timedelta(seconds=20),
            ),
        )
        await stage_takedown_transition(
            session,
            _transition(
                301,
                TakedownState.OUTBOX_PERSISTED,
                occurred_at=NOW - timedelta(seconds=10),
                attempt=0,
            ),
        )
        await session.commit()

    async with telemetry_db.sessions() as session:
        snapshot = (
            await CommunitySqlAlchemyTakedownTelemetry().query_takedown_telemetry(
                session,
                TakedownTelemetryQuery(
                    board_id=BOARD_ID,
                    delivery_key=delivery_key,
                    now=NOW,
                ),
            )
        )

    assert snapshot is not None
    assert snapshot.delete_event_id == delete_event_id
    assert [transition.state for transition in snapshot.states] == [
        TakedownState.INTENT_CREATED,
        TakedownState.OUTBOX_PERSISTED,
    ]
    assert snapshot.states[0].delivery_key is None


@pytest.mark.asyncio
async def test_timeline_and_aggregates_are_scoped_before_materialization(
    telemetry_db,
) -> None:
    own_artifact = "cross-board-own-artifact"
    own_event = "cross-board-own-delete"
    own_delivery = f"gd_parity:{BOARD_ID}:spec:{own_artifact}:1"
    other_artifact = "cross-board-other-artifact"
    other_event = "cross-board-other-delete"
    other_delivery = f"gd_parity:{OTHER_BOARD_ID}:spec:{other_artifact}:1"

    def transition(
        *,
        board_id: str,
        artifact_id: str,
        delete_event_id: str,
        delivery_key: str,
        state: TakedownState,
        occurred_at: datetime,
    ) -> TakedownTransition:
        return TakedownTransition(
            delete_event_id=delete_event_id,
            delivery_key=(
                None if state is TakedownState.INTENT_CREATED else delivery_key
            ),
            board_id=board_id,
            artifact_type="spec",
            artifact_id=artifact_id,
            generation=1,
            state=state,
            occurred_at=occurred_at,
            attempt=(0 if state is TakedownState.DELIVERED else None),
        )

    async with telemetry_db.sessions() as session:
        session.add(
            Board(
                id=OTHER_BOARD_ID,
                name="Card 9 other board",
                owner_id="other-owner",
            )
        )
        for item in (
            transition(
                board_id=BOARD_ID,
                artifact_id=own_artifact,
                delete_event_id=own_event,
                delivery_key=own_delivery,
                state=TakedownState.INTENT_CREATED,
                occurred_at=NOW - timedelta(seconds=20),
            ),
            transition(
                board_id=BOARD_ID,
                artifact_id=own_artifact,
                delete_event_id=own_event,
                delivery_key=own_delivery,
                state=TakedownState.DELIVERED,
                occurred_at=NOW,
            ),
            transition(
                board_id=OTHER_BOARD_ID,
                artifact_id=other_artifact,
                delete_event_id=other_event,
                delivery_key=other_delivery,
                state=TakedownState.INTENT_CREATED,
                occurred_at=NOW - timedelta(seconds=500),
            ),
            transition(
                board_id=OTHER_BOARD_ID,
                artifact_id=other_artifact,
                delete_event_id=other_event,
                delivery_key=other_delivery,
                state=TakedownState.DELIVERED,
                occurred_at=NOW,
            ),
        ):
            await stage_takedown_transition(session, item)
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=(f"gd_parity:{OTHER_BOARD_ID}:spec:other-debt:1"),
                board_id=OTHER_BOARD_ID,
                artifact_type="spec",
                artifact_id="other-debt",
                generation=1,
                delete_event_id="other-board-debt-delete",
                state=DeliveryState.DELIVERY_DEBT.value,
                attempt=0,
                last_error="other board only",
                next_retry_at=NOW + timedelta(minutes=1),
                created_at=NOW - timedelta(hours=2),
                updated_at=NOW - timedelta(hours=1),
            )
        )
        await session.commit()

    reader = CommunitySqlAlchemyTakedownTelemetry()
    async with telemetry_db.sessions() as session:
        own = await reader.query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                board_id=BOARD_ID,
                delete_event_id=own_event,
                now=NOW,
            ),
        )
        hidden_by_event = await reader.query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                board_id=BOARD_ID,
                delete_event_id=other_event,
                now=NOW,
            ),
        )
        hidden_by_delivery = await reader.query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                board_id=BOARD_ID,
                delivery_key=other_delivery,
                now=NOW,
            ),
        )

    assert own is not None
    assert own.board_id == BOARD_ID
    assert own.aggregates.delivery_debt_backlog == 0
    assert own.aggregates.oldest_debt_age_seconds is None
    assert own.aggregates.p95_sample_count == 1
    assert own.aggregates.p95_seconds_1h == pytest.approx(20.0, abs=0.1)
    assert hidden_by_event is None
    assert hidden_by_delivery is None


@pytest.mark.asyncio
async def test_circuit_snapshot_and_probe_failure_are_fail_closed(
    telemetry_db,
) -> None:
    class _Circuit:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail
            self.boards: list[str] = []

        async def read_circuit_snapshot(self, _context, *, board_id: str):
            self.boards.append(board_id)
            if self.fail:
                raise RuntimeError("probe unavailable")
            return DeliveryCircuitSnapshot(
                degraded=True,
                reason="global_outbox_terminal_backlog_present",
            )

    async with telemetry_db.sessions() as session:
        degraded = _Circuit()
        aggregate = await read_takedown_aggregates(
            session,
            now=NOW,
            circuit_reader=degraded,
            board_id=BOARD_ID,
        )
        failed = await read_takedown_aggregates(
            session,
            now=NOW,
            circuit_reader=_Circuit(fail=True),
            board_id=BOARD_ID,
        )

    assert degraded.boards == [BOARD_ID]
    assert aggregate.circuit_breaker_state == "open"
    assert aggregate.circuit_breaker_reason == (
        "global_outbox_terminal_backlog_present"
    )
    assert failed.circuit_breaker_state == "open"
    assert failed.circuit_breaker_reason == (
        "delivery_circuit_probe_failed:RuntimeError"
    )


@pytest.mark.asyncio
async def test_staged_transition_query_visibility_rolls_back_atomically(
    telemetry_db,
) -> None:
    transition = _transition(
        401,
        TakedownState.INTENT_CREATED,
        occurred_at=NOW,
    )
    async with telemetry_db.sessions() as session:
        assert await stage_takedown_transition(session, transition) is True
        visible = await CommunitySqlAlchemyTakedownTelemetry().query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                board_id=BOARD_ID,
                delete_event_id=transition.delete_event_id,
                now=NOW,
            ),
        )
        assert visible is not None
        await session.rollback()

    async with telemetry_db.sessions() as session:
        persisted = (
            await CommunitySqlAlchemyTakedownTelemetry().query_takedown_telemetry(
                session,
                TakedownTelemetryQuery(
                    board_id=BOARD_ID,
                    delete_event_id=transition.delete_event_id,
                    now=NOW,
                ),
            )
        )
        row_count = await session.scalar(
            select(func.count()).select_from(KGTakedownStateEvent)
        )

    assert persisted is None
    assert row_count == 0
