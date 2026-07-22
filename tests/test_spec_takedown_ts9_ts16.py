"""Edition-level persistence proofs for governed-takedown TS13 and TS16."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ConsolidationQueue,
    GlobalDiscoveryDeliveryLedger,
    GlobalUpdateOutbox,
    KGTakedownStateEvent,
)
from okto_pulse.core.application.processors import consolidation
from okto_pulse.core.kg.canonical_stale_reconciler import (
    ALL_NODE_TYPES,
    StaleReconcileResult,
)
from okto_pulse.core.ports.delivery_ledger import (
    DeliveryAttemptEnvelope,
    DeliveryAttemptOutcome,
    DeliveryAttemptResult,
    DeliveryState,
    DeliveryTransferRequest,
)
from okto_pulse.core.ports.stale_sweep import StaleSweepRescheduleRequest
from okto_pulse.core.ports.takedown_telemetry import (
    TakedownState,
    TakedownTelemetryQuery,
)
from test_card8_stale_sweep_persistence import (
    BOARD_ID as SWEEP_BOARD_ID,
    NOW as SWEEP_NOW,
    _claimed_sweep,
    sweep_db as _sweep_db,
)
from test_card9_takedown_telemetry import (
    ARTIFACT_ID,
    BOARD_ID,
    CLAIM_TOKEN,
    DELETE_EVENT_ID,
    ENTRY_ID,
    _claimed_queue,
    _intent,
    telemetry_store as _telemetry_store,
)


# Re-export the decorated fixtures into this module.  ``pytest_plugins`` is
# order-sensitive when the source test modules were already collected by a
# broader regression run; direct aliases remain discoverable in every order.
sweep_db = _sweep_db
telemetry_store = _telemetry_store


@pytest.mark.asyncio
async def test_ts13_complete_scan_receipt_survives_transfer_and_query(
    telemetry_store,
) -> None:
    """Per-type coverage reaches the immutable final graph-demoted receipt."""

    occurred_at = SWEEP_NOW
    counts = {
        node_type: index
        for index, node_type in enumerate(ALL_NODE_TYPES)
    }
    result = StaleReconcileResult(
        board_id=BOARD_ID,
        correlation_id=DELETE_EVENT_ID,
        scanned=sum(counts.values()),
        scanned_by_type=counts,
        completed_types=list(ALL_NODE_TYPES),
    )
    details = consolidation._stale_reconcile_telemetry_details(
        result,
        SimpleNamespace(attempts=3),
    )
    request = DeliveryTransferRequest(
        entry_id=ENTRY_ID,
        claim_token=CLAIM_TOKEN,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        target_state=DeliveryState.OUTBOX_PERSISTED,
        reconcile_details=details,
        occurred_at=occurred_at,
    )
    async with telemetry_store.sessions() as session:
        session.add(_claimed_queue(request))
        await session.flush()
        from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
            stage_takedown_transition,
        )

        await stage_takedown_transition(
            session,
            _intent(occurred_at=occurred_at - timedelta(seconds=1)),
        )
        await session.commit()

    async with telemetry_store.sessions() as session:
        receipt = await telemetry_store.ledger.transfer_delivery_ownership(
            session,
            request,
        )
        assert receipt.state is DeliveryState.OUTBOX_PERSISTED
        await session.commit()

    async with telemetry_store.sessions() as session:
        snapshot = await telemetry_store.telemetry.query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                delete_event_id=DELETE_EVENT_ID,
                now=occurred_at + timedelta(seconds=1),
            ),
        )
        row = (
            await session.execute(
                select(KGTakedownStateEvent).where(
                    KGTakedownStateEvent.state
                    == TakedownState.GRAPH_DEMOTED.value
                )
            )
        ).scalar_one()

    assert snapshot is not None
    graph_demoted = next(
        state
        for state in snapshot.states
        if state.state is TakedownState.GRAPH_DEMOTED
    )
    assert graph_demoted.details["scanned_by_type"] == counts
    assert graph_demoted.details["completed_types"] == list(ALL_NODE_TYPES)
    assert set(graph_demoted.details["scanned_by_type"]) == set(ALL_NODE_TYPES)
    assert row.details["scanned_by_type"] == counts
    assert row.details["completed_types"] == list(ALL_NODE_TYPES)


@pytest.mark.asyncio
async def test_ts16_board_absent_reschedule_persists_exact_checkpoint(
    sweep_db,
) -> None:
    """The Community adapter persists the Core board-absent decision atomically."""

    factory, adapter = sweep_db
    cursor = '["card","checkpoint-7"]'
    async with factory() as session:
        session.add(_claimed_sweep(cursor=cursor, budget=7, attempt=4))
        await session.commit()

    retry_at = SWEEP_NOW + timedelta(minutes=5)
    async with factory() as session:
        receipt = await adapter.reschedule_stale_sweep(
            session,
            StaleSweepRescheduleRequest(
                entry_id="sweep-card8",
                claim_token="claim-card8",
                board_id=SWEEP_BOARD_ID,
                cursor=cursor,
                budget=7,
                attempt=4,
                retry_at=retry_at,
                reason="board_absent",
            ),
        )
        await session.commit()

    async with factory() as session:
        row = await session.get(ConsolidationQueue, "sweep-card8")

    assert receipt.reason == "board_absent"
    assert receipt.cursor == cursor
    assert row is not None
    assert row.status == "pending"
    assert row.payload == {"cursor": cursor, "budget": 7, "attempt": 4}
    assert row.next_retry_at == retry_at.replace(tzinfo=None)
    assert row.claim_token is None


@pytest.mark.asyncio
async def test_ts16_degraded_transfer_redrives_attempt_one_then_delivers(
    telemetry_store,
) -> None:
    """Circuit debt has no attempt-zero row and heals through a fresh key."""

    occurred_at = SWEEP_NOW
    request = DeliveryTransferRequest(
        entry_id=ENTRY_ID,
        claim_token=CLAIM_TOKEN,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        target_state=DeliveryState.DELIVERY_DEBT,
        reconcile_details={
            "queue_attempt": 0,
            "scanned": 1,
            "demoted_count": 1,
            "routed_to_debt_count": 0,
            "incomplete": False,
            "incomplete_cause": None,
            "failed_types": [],
            "circuit_reason": "parity_probe_unavailable",
        },
        occurred_at=occurred_at,
    )
    async with telemetry_store.sessions() as session:
        session.add(_claimed_queue(request))
        await session.flush()
        from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
            stage_takedown_transition,
        )

        await stage_takedown_transition(
            session,
            _intent(occurred_at=occurred_at - timedelta(seconds=1)),
        )
        await session.commit()

    async with telemetry_store.sessions() as session:
        transferred = await telemetry_store.ledger.transfer_delivery_ownership(
            session,
            request,
        )
        assert transferred.state is DeliveryState.DELIVERY_DEBT
        assert transferred.attempt_event_key is None
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
        assert (
            await session.scalar(
                select(GlobalUpdateOutbox).where(
                    GlobalUpdateOutbox.event_id == attempt_zero.attempt_event_key
                )
            )
            is None
        )
        redrive = await telemetry_store.ledger.redrive_delivery_debt(
            session,
            now=occurred_at + timedelta(seconds=10),
            limit=1,
        )
        assert redrive.emitted == 1
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
        outbox = await session.scalar(
            select(GlobalUpdateOutbox).where(
                GlobalUpdateOutbox.event_id == attempt_one.attempt_event_key
            )
        )
        assert outbox is not None
        await telemetry_store.ledger.apply_attempt_outcomes(
            session,
            [
                DeliveryAttemptResult(
                    envelope=attempt_one,
                    outcome=DeliveryAttemptOutcome.DELIVERED,
                    occurred_at=occurred_at + timedelta(seconds=20),
                )
            ],
        )
        await session.commit()

    async with telemetry_store.sessions() as session:
        ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            attempt_one.delivery_key,
        )
        snapshot = await telemetry_store.telemetry.query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                delete_event_id=DELETE_EVENT_ID,
                now=occurred_at + timedelta(seconds=21),
            ),
        )

    assert ledger is not None
    assert ledger.state == DeliveryState.DELIVERED.value
    assert ledger.attempt == 1
    assert snapshot is not None
    assert [(state.state, state.attempt) for state in snapshot.states] == [
        (TakedownState.INTENT_CREATED, None),
        (TakedownState.GRAPH_DEMOTED, None),
        (TakedownState.DELIVERY_DEBT, 0),
        (TakedownState.OUTBOX_PERSISTED, 1),
        (TakedownState.DELIVERED, 1),
    ]
