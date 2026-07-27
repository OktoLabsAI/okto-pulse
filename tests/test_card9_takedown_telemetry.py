"""Card 9 -- durable governed-takedown timeline and operational query."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_ledger import (
    CommunitySqlAlchemyDeliveryLedger,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone,
    Board,
    ConsolidationQueue,
    KGTakedownStateEvent,
)
from okto_pulse.community.adapters.sqlalchemy_schema_contract import (
    COMMUNITY_SCHEMA_EXTENSION_TABLES,
)
from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
    CommunitySqlAlchemyTakedownTelemetry,
    stage_takedown_transition,
)
from okto_pulse.core.ports.delivery_ledger import (
    DeliveryAttemptEnvelope,
    DeliveryAttemptOutcome,
    DeliveryAttemptResult,
    DeliveryState,
    DeliveryTransferRequest,
    build_delivery_key,
)
from okto_pulse.core.ports.reconcile_intent import ReconcileIntentCreate
from okto_pulse.core.ports.takedown_telemetry import (
    TakedownSloEvaluationStatus,
    TakedownState,
    TakedownTelemetryQuery,
    TakedownTransition,
    TakedownTransitionConflict,
)


BOARD_ID = "91111111-1111-4111-8111-111111111111"
ARTIFACT_ID = "92222222-2222-4222-8222-222222222222"
DELETE_EVENT_ID = "card9-delete-spec-generation-1"
ENTRY_ID = "93333333-3333-4333-8333-333333333333"
CLAIM_TOKEN = "card9-claim-token"


@pytest_asyncio.fixture
async def telemetry_store(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'card9-takedown.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
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
        # The additive migration boundary must remain restart-safe.
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Board(id=BOARD_ID, name="Card 9", owner_id="tester"))
        await session.commit()

    ledger = CommunitySqlAlchemyDeliveryLedger()
    try:
        yield SimpleNamespace(
            engine=engine,
            sessions=sessions,
            ledger=ledger,
            telemetry=CommunitySqlAlchemyTakedownTelemetry(ledger),
        )
    finally:
        await engine.dispose()


def _request(
    *,
    occurred_at: datetime | None = None,
    reconcile_details: dict[str, object] | None = None,
) -> DeliveryTransferRequest:
    return DeliveryTransferRequest(
        entry_id=ENTRY_ID,
        claim_token=CLAIM_TOKEN,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        target_state=DeliveryState.OUTBOX_PERSISTED,
        occurred_at=occurred_at,
        reconcile_details=reconcile_details or {},
    )


def _claimed_queue(request: DeliveryTransferRequest) -> ConsolidationQueue:
    return ConsolidationQueue(
        id=request.entry_id,
        board_id=request.board_id,
        artifact_type=request.artifact_type,
        artifact_id=request.artifact_id,
        work_kind=request.work_kind,
        generation=request.generation,
        payload={
            "schema_version": 1,
            "delete_event_id": request.delete_event_id,
            "source_refs": [f"{request.artifact_type}:{request.artifact_id}"],
        },
        delete_event_id=request.delete_event_id,
        priority="high",
        source="governed_delete",
        status="claimed",
        worker_id="card9-worker",
        claimed_by_session_id="card9-worker",
        claim_token=request.claim_token,
    )


def _intent(*, occurred_at: datetime) -> TakedownTransition:
    return TakedownTransition(
        delete_event_id=DELETE_EVENT_ID,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        state=TakedownState.INTENT_CREATED,
        occurred_at=occurred_at,
        details={"source": "governed_delete"},
    )


def _delivered(*, occurred_at: datetime) -> TakedownTransition:
    return TakedownTransition(
        delete_event_id=DELETE_EVENT_ID,
        delivery_key=(
            f"gd_parity:{BOARD_ID}:spec:{ARTIFACT_ID}:1"
        ),
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        state=TakedownState.DELIVERED,
        occurred_at=occurred_at,
        attempt=0,
    )


@pytest.mark.asyncio
async def test_transition_write_is_append_only_exact_replay_or_conflict(
    telemetry_store,
):
    occurred_at = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    transition = _intent(occurred_at=occurred_at)

    async with telemetry_store.sessions() as session:
        assert await stage_takedown_transition(session, transition) is True
        assert await stage_takedown_transition(session, transition) is False
        with pytest.raises(
            TakedownTransitionConflict,
            match="takedown_transition_replay_conflict",
        ):
            await stage_takedown_transition(
                session,
                _intent(occurred_at=occurred_at + timedelta(seconds=1)),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_reconcile_intent_uses_the_controlled_delete_timestamp(
    telemetry_store,
):
    occurred_at = datetime(2026, 7, 21, 12, 34, 56, tzinfo=timezone.utc)
    request = ReconcileIntentCreate(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        source_refs=(f"spec:{ARTIFACT_ID}",),
        occurred_at=occurred_at,
    )
    adapter = CommunitySqlAlchemyConsolidationPersistence()
    async with telemetry_store.sessions() as session:
        session.add(
            ArtifactDeletionTombstone(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=ARTIFACT_ID,
                generation=1,
                delete_event_id=DELETE_EVENT_ID,
            )
        )
        await session.commit()

    async with telemetry_store.sessions() as session:
        first = await adapter.persist_reconcile_intent(session, request)
        replay = await adapter.persist_reconcile_intent(session, request)
        await session.commit()
    assert first.created is True
    assert replay.created is False

    async with telemetry_store.sessions() as session:
        queue = await session.get(ConsolidationQueue, first.intent_id)
        event_row = await session.get(
            KGTakedownStateEvent,
            f"takedown:{DELETE_EVENT_ID}:intent_created",
        )
    expected = occurred_at.replace(tzinfo=None)
    expected_delivery_key = build_delivery_key(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
    )
    assert queue is not None
    assert queue.triggered_at == expected
    assert event_row is not None
    assert event_row.occurred_at == expected
    assert event_row.delivery_key == expected_delivery_key

    async with telemetry_store.sessions() as session:
        immediate = await telemetry_store.telemetry.query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                board_id=BOARD_ID,
                delivery_key=expected_delivery_key,
                now=occurred_at,
            ),
        )
    assert immediate is not None
    assert immediate.delete_event_id == DELETE_EVENT_ID
    assert immediate.delivery_key == expected_delivery_key
    assert [state.state for state in immediate.states] == [
        TakedownState.INTENT_CREATED
    ]


@pytest.mark.asyncio
async def test_reconcile_intent_requeues_drained_work_with_original_timestamp(
    telemetry_store,
):
    occurred_at = datetime(2026, 7, 21, 12, 34, 56, tzinfo=timezone.utc)
    original = ReconcileIntentCreate(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        source_refs=(f"spec:{ARTIFACT_ID}",),
        occurred_at=occurred_at,
    )
    adapter = CommunitySqlAlchemyConsolidationPersistence()
    async with telemetry_store.sessions() as session:
        session.add(
            ArtifactDeletionTombstone(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=ARTIFACT_ID,
                generation=1,
                delete_event_id=DELETE_EVENT_ID,
            )
        )
        await session.commit()

    async with telemetry_store.sessions() as session:
        first = await adapter.persist_reconcile_intent(session, original)
        await session.commit()

    async with telemetry_store.sessions() as session:
        drained = await session.get(ConsolidationQueue, first.intent_id)
        assert drained is not None
        await session.delete(drained)
        await session.commit()

    async with telemetry_store.sessions() as session:
        replay = await adapter.persist_reconcile_intent(
            session,
            ReconcileIntentCreate(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=ARTIFACT_ID,
                generation=1,
                delete_event_id=DELETE_EVENT_ID,
                source_refs=(f"spec:{ARTIFACT_ID}",),
                occurred_at=occurred_at + timedelta(hours=1),
            ),
        )
        await session.commit()

    assert replay.created is True
    async with telemetry_store.sessions() as session:
        queue = await session.get(ConsolidationQueue, replay.intent_id)
        transitions = (
            await session.execute(
                select(KGTakedownStateEvent).where(
                    KGTakedownStateEvent.delete_event_id == DELETE_EVENT_ID,
                    KGTakedownStateEvent.state == "intent_created",
                )
            )
        ).scalars().all()
    assert queue is not None
    assert queue.triggered_at == occurred_at.replace(tzinfo=None)
    assert len(transitions) == 1
    assert transitions[0].occurred_at == occurred_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_transfer_redrives_and_outcomes_preserve_complete_timeline(
    telemetry_store,
):
    intent_at = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    request = _request(
        occurred_at=intent_at + timedelta(seconds=1),
        reconcile_details={
            "queue_attempt": 1,
            "scanned": 4,
            "demoted_count": 2,
            "routed_to_debt_count": 1,
            "incomplete": False,
            "incomplete_cause": None,
            "failed_types": [],
            "circuit_reason": "global_outbox_terminal_backlog_absent",
        },
    )
    async with telemetry_store.sessions() as session:
        queue = _claimed_queue(request)
        queue.triggered_at = intent_at
        session.add(queue)
        await session.flush([queue])
        await stage_takedown_transition(session, _intent(occurred_at=intent_at))
        await session.commit()

    async with telemetry_store.sessions() as session:
        receipt = await telemetry_store.ledger.transfer_delivery_ownership(
            session,
            request,
        )
        assert receipt.state is DeliveryState.OUTBOX_PERSISTED
        await session.commit()

    attempt_zero = DeliveryAttemptEnvelope(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        attempt=0,
    )
    async with telemetry_store.sessions() as session:
        await telemetry_store.ledger.apply_attempt_outcomes(
            session,
            [
                DeliveryAttemptResult(
                    envelope=attempt_zero,
                    outcome=DeliveryAttemptOutcome.DELIVERY_DEBT,
                    occurred_at=intent_at + timedelta(seconds=10),
                    error="attempt_zero_terminal",
                )
            ],
        )
        await session.commit()

    async with telemetry_store.sessions() as session:
        redrive_one = await telemetry_store.ledger.redrive_delivery_debt(
            session,
            now=intent_at + timedelta(seconds=20),
            limit=1,
        )
        assert redrive_one.emitted == 1
        await session.commit()

    attempt_one = DeliveryAttemptEnvelope(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        attempt=1,
    )
    async with telemetry_store.sessions() as session:
        await telemetry_store.ledger.apply_attempt_outcomes(
            session,
            [
                DeliveryAttemptResult(
                    envelope=attempt_one,
                    outcome=DeliveryAttemptOutcome.DELIVERY_DEBT,
                    occurred_at=intent_at + timedelta(seconds=30),
                    error="attempt_one_terminal",
                )
            ],
        )
        await session.commit()

    async with telemetry_store.sessions() as session:
        redrive_two = await telemetry_store.ledger.redrive_delivery_debt(
            session,
            now=intent_at + timedelta(seconds=40),
            limit=1,
        )
        assert redrive_two.emitted == 1
        await session.commit()

    attempt_two = DeliveryAttemptEnvelope(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        attempt=2,
    )
    async with telemetry_store.sessions() as session:
        await telemetry_store.ledger.apply_attempt_outcomes(
            session,
            [
                DeliveryAttemptResult(
                    envelope=attempt_two,
                    outcome=DeliveryAttemptOutcome.DELIVERED,
                    occurred_at=intent_at + timedelta(seconds=50),
                )
            ],
        )
        await session.commit()

    query_at = intent_at + timedelta(seconds=60)
    async with telemetry_store.sessions() as session:
        by_event = await telemetry_store.telemetry.query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                board_id=BOARD_ID,
                delete_event_id=DELETE_EVENT_ID,
                now=query_at,
            ),
        )
        by_delivery = await telemetry_store.telemetry.query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                board_id=BOARD_ID,
                delivery_key=attempt_zero.delivery_key,
                now=query_at,
            ),
        )

    assert by_event is not None
    assert by_delivery == by_event
    assert [state.state for state in by_event.states] == [
        TakedownState.INTENT_CREATED,
        TakedownState.GRAPH_DEMOTED,
        TakedownState.OUTBOX_PERSISTED,
        TakedownState.DELIVERY_DEBT,
        TakedownState.OUTBOX_PERSISTED,
        TakedownState.DELIVERY_DEBT,
        TakedownState.OUTBOX_PERSISTED,
        TakedownState.DELIVERED,
    ]
    assert [
        (state.state, state.attempt, state.last_error)
        for state in by_event.states
        if state.state is TakedownState.DELIVERY_DEBT
    ] == [
        (TakedownState.DELIVERY_DEBT, 0, "attempt_zero_terminal"),
        (TakedownState.DELIVERY_DEBT, 1, "attempt_one_terminal"),
    ]
    assert by_event.aggregates.delivery_debt_backlog == 0
    assert by_event.aggregates.p95_sample_count == 1
    assert by_event.aggregates.p95_seconds_1h == pytest.approx(50.0, abs=0.1)
    graph_demoted = next(
        state
        for state in by_event.states
        if state.state is TakedownState.GRAPH_DEMOTED
    )
    assert graph_demoted.details == {
        "source": "stale_reconcile",
        "queue_attempt": 1,
        "scanned": 4,
        "demoted_count": 2,
        "routed_to_debt_count": 1,
        "incomplete": False,
        "incomplete_cause": None,
        "failed_types": [],
        "circuit_reason": "global_outbox_terminal_backlog_absent",
    }
    assert by_event.to_dict()["slo"]["health_predicate"] == (
        "delivered_state_and_evaluable_parity_probe"
    )


@pytest.mark.asyncio
async def test_schema_contract_and_query_not_found(telemetry_store):
    assert "kg_takedown_state_events" in COMMUNITY_SCHEMA_EXTENSION_TABLES
    async with telemetry_store.sessions() as session:
        assert (
            await telemetry_store.telemetry.query_takedown_telemetry(
                session,
                TakedownTelemetryQuery(
                    board_id=BOARD_ID,
                    delete_event_id="unknown-delete-event",
                    now=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
                ),
            )
            is None
        )
        rows = (
            await session.execute(select(KGTakedownStateEvent))
        ).scalars().all()
        assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duration_seconds", "expected_status", "expected_breached"),
    (
        (120, TakedownSloEvaluationStatus.WITHIN_SLO, False),
        (121, TakedownSloEvaluationStatus.BREACHED, True),
    ),
    ids=("at_threshold", "above_threshold"),
)
async def test_periodic_slo_evaluation_emits_only_on_strict_breach(
    telemetry_store,
    caplog: pytest.LogCaptureFixture,
    duration_seconds: int,
    expected_status: TakedownSloEvaluationStatus,
    expected_breached: bool,
):
    intent_at = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    observed_at = intent_at + timedelta(seconds=130)
    async with telemetry_store.sessions() as session:
        await stage_takedown_transition(session, _intent(occurred_at=intent_at))
        await stage_takedown_transition(
            session,
            _delivered(
                occurred_at=intent_at + timedelta(seconds=duration_seconds)
            ),
        )
        await session.commit()

    with caplog.at_level(
        "CRITICAL",
        logger="okto_pulse.kg.takedown_telemetry",
    ):
        async with telemetry_store.sessions() as session:
            evaluation = await telemetry_store.telemetry.evaluate_takedown_slo(
                session,
                board_id=BOARD_ID,
                now=observed_at,
                transaction_state="pending_caller_commit",
            )

    assert evaluation.status is expected_status
    assert evaluation.breached is expected_breached
    assert evaluation.observed_at == observed_at
    assert evaluation.transaction_state == "pending_caller_commit"
    breaches = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "kg.takedown.slo_breach"
    ]
    assert len(breaches) == int(expected_breached)
    if expected_breached:
        assert breaches[0].board_id == BOARD_ID
        assert breaches[0].transaction_state == "pending_caller_commit"


@pytest.mark.asyncio
async def test_empty_window_is_explicitly_insufficient_not_healthy(
    telemetry_store,
    caplog: pytest.LogCaptureFixture,
):
    observed_at = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

    with caplog.at_level(
        "CRITICAL",
        logger="okto_pulse.kg.takedown_telemetry",
    ):
        async with telemetry_store.sessions() as session:
            evaluation = await telemetry_store.telemetry.evaluate_takedown_slo(
                session,
                board_id=BOARD_ID,
                now=observed_at,
                transaction_state="pending_caller_commit",
            )

    payload = evaluation.to_dict()
    assert evaluation.status is TakedownSloEvaluationStatus.INSUFFICIENT_DATA
    assert payload["evaluated"] is True
    assert payload["breached"] is False
    assert payload["status"] == "insufficient_data"
    assert payload["metrics"]["p95_sample_count"] == 0
    assert payload["alert"] is None
    assert not any(
        getattr(record, "event", None) == "kg.takedown.slo_breach"
        for record in caplog.records
    )
