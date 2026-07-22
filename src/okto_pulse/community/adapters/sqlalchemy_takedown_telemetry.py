"""SQLAlchemy adapter for governed-takedown state telemetry."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased

from okto_pulse.community.adapters.sqlalchemy_models import (
    GlobalDiscoveryDeliveryLedger,
    KGTakedownStateEvent,
)
from okto_pulse.core.ports.delivery_ledger import DeliveryState
from okto_pulse.core.ports.takedown_telemetry import (
    TAKEDOWN_P95_WINDOW_SECONDS,
    TAKEDOWN_STATE_RANK,
    TakedownAggregates,
    TakedownSloEvaluation,
    TakedownState,
    TakedownTelemetryQuery,
    TakedownTelemetrySnapshot,
    TakedownTransition,
    TakedownTransitionConflict,
    build_takedown_slo_alert,
)


logger = logging.getLogger("okto_pulse.kg.takedown_telemetry")


def _normalized_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _row_identity(row: KGTakedownStateEvent) -> tuple[object, ...]:
    return (
        str(row.transition_key),
        str(row.delete_event_id),
        str(row.delivery_key) if row.delivery_key is not None else None,
        str(row.board_id),
        str(row.artifact_type),
        str(row.artifact_id),
        int(row.generation),
        str(row.state),
        int(row.attempt) if row.attempt is not None else None,
        _normalized_datetime(row.occurred_at),
        str(row.last_error) if row.last_error is not None else None,
        _normalized_datetime(row.next_retry_at),
        dict(row.details or {}),
    )


def _transition_identity(transition: TakedownTransition) -> tuple[object, ...]:
    return (
        transition.transition_key,
        transition.delete_event_id,
        transition.delivery_key,
        transition.board_id,
        transition.artifact_type,
        transition.artifact_id,
        transition.generation,
        transition.state.value,
        transition.attempt,
        _normalized_datetime(transition.occurred_at),
        transition.last_error,
        _normalized_datetime(transition.next_retry_at),
        dict(transition.details),
    )


async def stage_takedown_transition(
    context: Any,
    transition: TakedownTransition,
) -> bool:
    """Insert one immutable transition in the caller-owned transaction.

    Exact replay is a no-op.  Reusing a deterministic transition key with any
    divergent field is a typed, fail-closed conflict.
    """

    if not isinstance(transition, TakedownTransition):
        raise ValueError("takedown_telemetry_transition_invalid")
    values = {
        "transition_key": transition.transition_key,
        "delete_event_id": transition.delete_event_id,
        "delivery_key": transition.delivery_key,
        "board_id": transition.board_id,
        "artifact_type": transition.artifact_type,
        "artifact_id": transition.artifact_id,
        "generation": transition.generation,
        "state": transition.state.value,
        "attempt": transition.attempt,
        "occurred_at": transition.occurred_at,
        "last_error": transition.last_error,
        "next_retry_at": transition.next_retry_at,
        "details": dict(transition.details),
    }
    inserted = (
        await context.execute(
            sqlite_insert(KGTakedownStateEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["transition_key"])
            .returning(KGTakedownStateEvent.transition_key)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        await context.flush()
        return True

    existing = await context.get(
        KGTakedownStateEvent,
        transition.transition_key,
        populate_existing=True,
    )
    if existing is None or _row_identity(existing) != _transition_identity(transition):
        raise TakedownTransitionConflict(
            "takedown_transition_replay_conflict"
        )
    return False


def _transition_from_row(row: KGTakedownStateEvent) -> TakedownTransition:
    return TakedownTransition(
        delete_event_id=str(row.delete_event_id),
        delivery_key=(
            str(row.delivery_key) if row.delivery_key is not None else None
        ),
        board_id=str(row.board_id),
        artifact_type=str(row.artifact_type),
        artifact_id=str(row.artifact_id),
        generation=int(row.generation),
        state=TakedownState(str(row.state)),
        occurred_at=row.occurred_at,
        attempt=int(row.attempt) if row.attempt is not None else None,
        last_error=(str(row.last_error) if row.last_error is not None else None),
        next_retry_at=row.next_retry_at,
        details=dict(row.details or {}),
    )


def _elapsed_seconds(*, now: datetime, since: datetime | None) -> float | None:
    if since is None:
        return None
    comparable = since
    if comparable.tzinfo is None and now.tzinfo is not None:
        comparable = comparable.replace(tzinfo=now.tzinfo)
    elif comparable.tzinfo is not None and now.tzinfo is None:
        comparable = comparable.replace(tzinfo=None)
    return max(0.0, float((now - comparable).total_seconds()))


async def read_takedown_aggregates(
    context: Any,
    *,
    now: datetime,
    circuit_reader: Any | None,
    board_id: str,
) -> TakedownAggregates:
    """Read the four OR2 metrics with a controlled observation clock."""

    if not isinstance(now, datetime):
        raise ValueError("takedown_telemetry_now_invalid")
    backlog = int(
        await context.scalar(
            select(func.count())
            .select_from(GlobalDiscoveryDeliveryLedger)
            .where(
                GlobalDiscoveryDeliveryLedger.state
                == DeliveryState.DELIVERY_DEBT.value
            )
        )
        or 0
    )
    debt_event = aliased(KGTakedownStateEvent)
    oldest_debt_at = await context.scalar(
        select(
            func.min(
                func.coalesce(
                    debt_event.occurred_at,
                    GlobalDiscoveryDeliveryLedger.updated_at,
                )
            )
        )
        .select_from(GlobalDiscoveryDeliveryLedger)
        .outerjoin(
            debt_event,
            (
                debt_event.delivery_key
                == GlobalDiscoveryDeliveryLedger.delivery_key
            )
            & (debt_event.state == TakedownState.DELIVERY_DEBT.value),
        )
        .where(
            GlobalDiscoveryDeliveryLedger.state
            == DeliveryState.DELIVERY_DEBT.value
        )
    )

    delivered = aliased(KGTakedownStateEvent)
    intent = aliased(KGTakedownStateEvent)
    duration_seconds = (
        func.julianday(delivered.occurred_at)
        - func.julianday(intent.occurred_at)
    ) * 86400.0
    window_start = now - timedelta(seconds=TAKEDOWN_P95_WINDOW_SECONDS)
    durations = (
        select(duration_seconds.label("duration_seconds"))
        .join(
            intent,
            (intent.delete_event_id == delivered.delete_event_id)
            & (intent.state == TakedownState.INTENT_CREATED.value),
        )
        .where(
            delivered.state == TakedownState.DELIVERED.value,
            delivered.occurred_at >= window_start,
            delivered.occurred_at <= now,
        )
        .subquery()
    )
    sample_count = int(
        await context.scalar(select(func.count()).select_from(durations)) or 0
    )
    p95_seconds: float | None = None
    if sample_count:
        nearest_rank_offset = max(0, math.ceil(0.95 * sample_count) - 1)
        observed = await context.scalar(
            select(durations.c.duration_seconds)
            .order_by(durations.c.duration_seconds.asc())
            .offset(nearest_rank_offset)
            .limit(1)
        )
        p95_seconds = max(0.0, float(observed or 0.0))

    if circuit_reader is None:
        circuit_state = "open"
        circuit_reason = "delivery_circuit_reader_unavailable"
    else:
        try:
            circuit = await circuit_reader.read_circuit_snapshot(
                context,
                board_id=board_id,
            )
            circuit_state = "open" if circuit.degraded else "closed"
            circuit_reason = str(circuit.reason)
        except Exception as exc:  # fail closed, query remains available
            circuit_state = "open"
            circuit_reason = f"delivery_circuit_probe_failed:{type(exc).__name__}"

    return TakedownAggregates(
        delivery_debt_backlog=backlog,
        oldest_debt_age_seconds=_elapsed_seconds(
            now=now,
            since=oldest_debt_at,
        ),
        circuit_breaker_state=circuit_state,
        circuit_breaker_reason=circuit_reason,
        p95_seconds_1h=p95_seconds,
        p95_sample_count=sample_count,
    )


async def log_takedown_slo_breach(
    context: Any,
    *,
    now: datetime,
    circuit_reader: Any | None,
    board_id: str,
    transaction_state: str,
) -> dict[str, object] | None:
    """Evaluate OR2 and emit the stable structured alert when breached."""

    aggregates = await read_takedown_aggregates(
        context,
        now=now,
        circuit_reader=circuit_reader,
        board_id=board_id,
    )
    return _emit_takedown_slo_breach(
        aggregates,
        board_id=board_id,
        transaction_state=transaction_state,
    )


def _emit_takedown_slo_breach(
    aggregates: TakedownAggregates,
    *,
    board_id: str,
    transaction_state: str,
) -> dict[str, object] | None:
    """Emit one breach from already-read aggregates without a second query."""

    alert = build_takedown_slo_alert(aggregates)
    if alert is None:
        return None
    payload = {
        **alert,
        "board_id": board_id,
        "transaction_state": transaction_state,
    }
    logger.critical(
        "kg.takedown.slo_breach reasons=%s p95_seconds_1h=%s "
        "oldest_debt_age_seconds=%s backlog=%d circuit=%s",
        payload["reasons"],
        aggregates.p95_seconds_1h,
        aggregates.oldest_debt_age_seconds,
        aggregates.delivery_debt_backlog,
        aggregates.circuit_breaker_state,
        extra=payload,
    )
    return payload


class CommunitySqlAlchemyTakedownTelemetry:
    """Read projection over the immutable timeline and current owner ledger."""

    def __init__(self, circuit_reader: Any | None = None) -> None:
        self._circuit_reader = circuit_reader

    async def evaluate_takedown_slo(
        self,
        context: Any,
        *,
        board_id: str,
        now: datetime,
        transaction_state: str,
    ) -> TakedownSloEvaluation:
        """Evaluate the periodic SLO in the caller-owned transaction."""

        aggregates = await read_takedown_aggregates(
            context,
            now=now,
            circuit_reader=self._circuit_reader,
            board_id=board_id,
        )
        _emit_takedown_slo_breach(
            aggregates,
            board_id=board_id,
            transaction_state=transaction_state,
        )
        return TakedownSloEvaluation(
            board_id=board_id,
            observed_at=now,
            transaction_state=transaction_state,
            aggregates=aggregates,
        )

    async def query_takedown_telemetry(
        self,
        context: Any,
        query: TakedownTelemetryQuery,
    ) -> TakedownTelemetrySnapshot | None:
        if not isinstance(query, TakedownTelemetryQuery):
            raise ValueError("takedown_telemetry_query_invalid")
        resolved_delete_event_id = query.delete_event_id
        if resolved_delete_event_id is None:
            delete_event_ids = tuple(
                str(value)
                for value in (
                    await context.scalars(
                        select(KGTakedownStateEvent.delete_event_id)
                        .where(
                            KGTakedownStateEvent.delivery_key
                            == query.delivery_key
                        )
                        .distinct()
                    )
                ).all()
            )
            if not delete_event_ids:
                return None
            if len(delete_event_ids) != 1:
                raise TakedownTransitionConflict(
                    "takedown_timeline_delivery_identity_conflict"
                )
            resolved_delete_event_id = delete_event_ids[0]
        predicate = (
            KGTakedownStateEvent.delete_event_id == resolved_delete_event_id
        )
        state_rank = case(
            *(
                (KGTakedownStateEvent.state == state.value, rank)
                for state, rank in TAKEDOWN_STATE_RANK.items()
            ),
            else_=99,
        )
        rows = (
            (
                await context.execute(
                    select(KGTakedownStateEvent)
                    .where(predicate)
                    .order_by(
                        KGTakedownStateEvent.occurred_at.asc(),
                        state_rank.asc(),
                        KGTakedownStateEvent.attempt.asc(),
                        KGTakedownStateEvent.transition_key.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        first = rows[0]
        immutable_identity = (
            str(first.delete_event_id),
            str(first.board_id),
            str(first.artifact_type),
            str(first.artifact_id),
            int(first.generation),
        )
        if any(
            (
                str(row.delete_event_id),
                str(row.board_id),
                str(row.artifact_type),
                str(row.artifact_id),
                int(row.generation),
            )
            != immutable_identity
            for row in rows
        ):
            raise TakedownTransitionConflict(
                "takedown_timeline_identity_conflict"
            )
        delivery_keys = {
            str(row.delivery_key)
            for row in rows
            if row.delivery_key is not None
        }
        if len(delivery_keys) > 1:
            raise TakedownTransitionConflict(
                "takedown_timeline_delivery_identity_conflict"
            )
        delivery_key = next(iter(delivery_keys), None)
        if query.delivery_key is not None and delivery_key != query.delivery_key:
            raise TakedownTransitionConflict(
                "takedown_timeline_selector_conflict"
            )
        aggregates = await read_takedown_aggregates(
            context,
            now=query.now,
            circuit_reader=self._circuit_reader,
            board_id=str(first.board_id),
        )
        return TakedownTelemetrySnapshot(
            board_id=str(first.board_id),
            delete_event_id=str(first.delete_event_id),
            delivery_key=delivery_key,
            artifact_type=str(first.artifact_type),
            artifact_id=str(first.artifact_id),
            generation=int(first.generation),
            states=tuple(_transition_from_row(row) for row in rows),
            aggregates=aggregates,
        )


__all__ = [
    "CommunitySqlAlchemyTakedownTelemetry",
    "log_takedown_slo_breach",
    "read_takedown_aggregates",
    "stage_takedown_transition",
]
