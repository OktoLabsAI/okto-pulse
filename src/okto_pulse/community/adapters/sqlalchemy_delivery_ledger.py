"""Atomic Community ownership transfer for Global Discovery delivery.

The stale-reconciliation worker already committed its graph-side statement
before entering this adapter.  This adapter owns the relational hand-off: the
delivery ledger, the physical attempt-zero outbox row (healthy path), and the
strong compare-and-delete of the claimed queue row are staged in one caller-
owned transaction.

There is deliberately no ``commit`` or ``rollback`` here.  A failed flush,
replay validation, or queue CAS must leave transaction disposal to the Core
unit of work so none of the three effects can become independently durable.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import and_, case, delete, func, literal, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.sqlalchemy_models import (
    ConsolidationQueue,
    GlobalDiscoveryDeliveryLedger,
    GlobalDiscoveryDeliveryRedriveControl,
    GlobalDiscoveryDeliveryWatchdogControl,
    GlobalUpdateOutbox,
)
from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
    stage_takedown_transition,
)
from okto_pulse.core.ports.delivery_ledger import (
    DeliveryAttemptContractError,
    DeliveryAttemptEnvelope,
    DeliveryAttemptMutationConflict,
    DeliveryAttemptOutcome,
    DeliveryAttemptResult,
    DeliveryCircuitSnapshot,
    DeliveryMaintenanceReceipt,
    DeliveryRedriveConflict,
    DeliveryState,
    DeliveryTransferClaimConflict,
    DeliveryTransferReceipt,
    DeliveryTransferReplayConflict,
    DeliveryTransferRequest,
    parse_delivery_attempt_event,
)
from okto_pulse.core.ports.global_outbox import (
    GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
    GLOBAL_OUTBOX_MAX_RETRIES,
)
from okto_pulse.core.ports.takedown_telemetry import (
    TakedownState,
    TakedownTransition,
)


_INITIAL_STATES = frozenset(
    {DeliveryState.OUTBOX_PERSISTED, DeliveryState.DELIVERY_DEBT}
)

_WATCHDOG_OUTBOX_MISSING = "delivery_watchdog_outbox_missing"
_WATCHDOG_OUTBOX_INVALID = "delivery_watchdog_outbox_contract_invalid"
_WATCHDOG_OUTBOX_TERMINAL = "delivery_watchdog_outbox_terminal"
_REDRIVE_CONTROL_ID = "_global"


async def _stage_ledger_transition(
    context: Any,
    *,
    ledger: GlobalDiscoveryDeliveryLedger,
    state: TakedownState,
    occurred_at: datetime,
    attempt: int | None,
    source: str,
    last_error: str | None = None,
    next_retry_at: datetime | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    """Persist one immutable timeline row in the caller-owned transaction."""

    await stage_takedown_transition(
        context,
        TakedownTransition(
            delete_event_id=str(ledger.delete_event_id),
            delivery_key=str(ledger.delivery_key),
            board_id=str(ledger.board_id),
            artifact_type=str(ledger.artifact_type),
            artifact_id=str(ledger.artifact_id),
            generation=int(ledger.generation),
            state=state,
            occurred_at=occurred_at,
            attempt=attempt,
            last_error=last_error,
            next_retry_at=next_retry_at,
            details={**dict(details or {}), "source": source},
        ),
    )


def _state(value: Any) -> DeliveryState:
    try:
        return DeliveryState(str(getattr(value, "value", value)))
    except (TypeError, ValueError) as exc:
        raise DeliveryTransferReplayConflict(
            "delivery_ledger_state_invalid"
        ) from exc


def _validate_request(request: DeliveryTransferRequest) -> DeliveryState:
    target_state = _state(request.target_state)
    required_text = (
        request.entry_id,
        request.claim_token,
        request.board_id,
        request.artifact_type,
        request.artifact_id,
        request.work_kind,
        request.delete_event_id,
        request.delivery_key,
    )
    if (
        any(not isinstance(value, str) or not value for value in required_text)
        or request.work_kind != "stale_reconcile"
        or int(request.generation) < 1
        or int(request.attempt) != 0
        or target_state not in _INITIAL_STATES
    ):
        raise ValueError("delivery_transfer_request_invalid")
    if target_state is DeliveryState.OUTBOX_PERSISTED and (
        not request.attempt_event_key
        or not request.outbox_session_id
        or not request.outbox_event_type
    ):
        raise ValueError("delivery_transfer_outbox_identity_invalid")
    return target_state


def _ledger_identity_matches(
    row: GlobalDiscoveryDeliveryLedger,
    request: DeliveryTransferRequest,
) -> bool:
    return (
        str(row.delivery_key),
        str(row.board_id),
        str(row.artifact_type),
        str(row.artifact_id),
        int(row.generation),
        str(row.delete_event_id),
    ) == (
        request.delivery_key,
        request.board_id,
        request.artifact_type,
        request.artifact_id,
        int(request.generation),
        request.delete_event_id,
    )


def _validate_existing_ledger(
    row: GlobalDiscoveryDeliveryLedger,
    request: DeliveryTransferRequest,
) -> DeliveryState:
    if not _ledger_identity_matches(row, request):
        raise DeliveryTransferReplayConflict(
            "delivery_ledger_identity_replay_conflict"
        )

    state = _state(row.state)
    stored_attempt = int(row.attempt)
    # A durable owner is authoritative across circuit-snapshot changes.  The
    # circuit only selects state for a brand-new owner; replay validates the
    # stored attempt and its physical invariant without rewriting that state.
    if stored_attempt != int(request.attempt) or state not in _INITIAL_STATES:
        raise DeliveryTransferReplayConflict(
            "delivery_ledger_mutable_state_replay_conflict"
        )

    stored_event_key = (
        str(row.attempt_event_key) if row.attempt_event_key is not None else None
    )
    expected_stored_key = (
        request.attempt_event_key
        if state is DeliveryState.OUTBOX_PERSISTED
        else None
    )
    if stored_event_key != expected_stored_key:
        raise DeliveryTransferReplayConflict(
            "delivery_ledger_attempt_event_replay_conflict"
        )
    return state


def _validate_exact_outbox(
    row: GlobalUpdateOutbox,
    request: DeliveryTransferRequest,
) -> None:
    persisted = (
        str(row.event_id),
        str(row.board_id),
        str(row.session_id),
        str(row.event_type),
        dict(row.payload or {}),
    )
    expected = (
        request.attempt_event_key,
        request.board_id,
        request.outbox_session_id,
        request.outbox_event_type,
        dict(request.payload),
    )
    if persisted != expected:
        raise DeliveryTransferReplayConflict(
            "delivery_attempt_outbox_replay_conflict"
        )


def _validate_maintenance_scope(
    *,
    board_id: str,
    now: datetime,
    limit: int,
) -> None:
    if (
        not isinstance(board_id, str)
        or not board_id
        or board_id != board_id.strip()
    ):
        raise ValueError("delivery_maintenance_board_id_invalid")
    if not isinstance(now, datetime):
        raise ValueError("delivery_maintenance_now_invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("delivery_maintenance_limit_invalid")


def _validate_redrive_scope(*, now: datetime, limit: int) -> None:
    if not isinstance(now, datetime):
        raise ValueError("delivery_maintenance_now_invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("delivery_maintenance_limit_invalid")


def _due_debt_predicate(*, now: datetime) -> object:
    return and_(
        GlobalDiscoveryDeliveryLedger.state
        == DeliveryState.DELIVERY_DEBT.value,
        or_(
            GlobalDiscoveryDeliveryLedger.next_retry_at.is_(None),
            GlobalDiscoveryDeliveryLedger.next_retry_at <= now,
        ),
    )


def _elapsed_seconds(*, now: datetime, oldest_at: datetime | None) -> float | None:
    if oldest_at is None:
        return None
    comparable = oldest_at
    if comparable.tzinfo is None and now.tzinfo is not None:
        comparable = comparable.replace(tzinfo=now.tzinfo)
    elif comparable.tzinfo is not None and now.tzinfo is None:
        comparable = comparable.replace(tzinfo=None)
    return max(0.0, float((now - comparable).total_seconds()))


def _envelope_from_ledger(
    row: GlobalDiscoveryDeliveryLedger,
) -> DeliveryAttemptEnvelope:
    try:
        envelope = DeliveryAttemptEnvelope(
            board_id=str(row.board_id),
            artifact_type=str(row.artifact_type),
            artifact_id=str(row.artifact_id),
            generation=int(row.generation),
            delete_event_id=str(row.delete_event_id),
            attempt=int(row.attempt),
        )
    except (TypeError, ValueError) as exc:
        raise DeliveryAttemptContractError(
            "delivery_ledger_attempt_identity_invalid"
        ) from exc
    if str(row.delivery_key) != envelope.delivery_key:
        raise DeliveryAttemptContractError(
            "delivery_ledger_attempt_identity_invalid"
        )
    return envelope


def _ledger_identity_matches_envelope(
    row: GlobalDiscoveryDeliveryLedger,
    envelope: DeliveryAttemptEnvelope,
) -> bool:
    return (
        str(row.delivery_key),
        str(row.board_id),
        str(row.artifact_type),
        str(row.artifact_id),
        int(row.generation),
        str(row.delete_event_id),
    ) == (
        envelope.delivery_key,
        envelope.board_id,
        envelope.artifact_type,
        envelope.artifact_id,
        envelope.generation,
        envelope.delete_event_id,
    )


def _is_terminal_outbox(row: GlobalUpdateOutbox) -> bool:
    return row.processed_at is None and (
        int(row.retry_count) == GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL
        or int(row.retry_count) >= GLOBAL_OUTBOX_MAX_RETRIES
    )


class CommunitySqlAlchemyDeliveryLedger:
    """SQLite/SQLAlchemy adapter for the delivery-ledger ownership boundary."""

    async def read_circuit_snapshot(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> DeliveryCircuitSnapshot:
        """Return a conservative global circuit snapshot.

        Global Discovery is one global sink, so a terminal event for any board
        degrades new ownership transfers for every board.  Probe failures are
        fail-closed and route the transfer to durable delivery debt.
        """

        del board_id  # Scope is intentionally global, not per-board.
        try:
            payload_delivery_key = GlobalUpdateOutbox.payload[
                "delivery_key"
            ].as_string()
            payload_delete_event_id = GlobalUpdateOutbox.payload[
                "delete_event_id"
            ].as_string()
            attempt_marker = func.instr(
                GlobalUpdateOutbox.event_id,
                ":attempt:",
            )
            event_delivery_key = func.substr(
                GlobalUpdateOutbox.event_id,
                1,
                attempt_marker - 1,
            )
            historical_candidate = or_(
                GlobalDiscoveryDeliveryLedger.delivery_key
                == payload_delivery_key,
                GlobalDiscoveryDeliveryLedger.delete_event_id
                == payload_delete_event_id,
                and_(
                    GlobalUpdateOutbox.event_id.like("gd_parity:%"),
                    attempt_marker > 0,
                    GlobalDiscoveryDeliveryLedger.delivery_key
                    == event_delivery_key,
                ),
            )
            candidate_exists = (
                select(GlobalDiscoveryDeliveryLedger.delivery_key)
                .where(historical_candidate)
                .correlate(GlobalUpdateOutbox)
                .exists()
            )
            unresolved_candidate_exists = (
                select(GlobalDiscoveryDeliveryLedger.delivery_key)
                .where(
                    historical_candidate,
                    GlobalDiscoveryDeliveryLedger.state
                    != DeliveryState.DELIVERED.value,
                )
                .correlate(GlobalUpdateOutbox)
                .exists()
            )
            # Consumption remains strict. Only this historical DLQ probe is
            # identity-tolerant: a unique logical key, unique delete event or
            # physical attempt prefix may recover the ledger relationship. A
            # row is suppressible only when at least one candidate exists and
            # every candidate is delivered. Missing candidates and ambiguous
            # delivered+non-delivered identities remain fail-closed. The outer
            # LIMIT and correlated EXISTS probes do not materialize history.
            unresolved_terminal = await context.scalar(
                select(GlobalUpdateOutbox.id)
                .where(
                    GlobalUpdateOutbox.processed_at.is_(None),
                    or_(
                        GlobalUpdateOutbox.retry_count
                        == GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
                        GlobalUpdateOutbox.retry_count
                        >= GLOBAL_OUTBOX_MAX_RETRIES,
                    ),
                    or_(
                        ~candidate_exists,
                        unresolved_candidate_exists,
                    ),
                )
                .limit(1)
            )
            if unresolved_terminal is not None:
                return DeliveryCircuitSnapshot(
                    degraded=True,
                    reason="global_outbox_terminal_backlog",
                )
        except Exception as exc:
            return DeliveryCircuitSnapshot(
                degraded=True,
                reason=f"global_outbox_terminal_probe_failed:{type(exc).__name__}",
            )
        return DeliveryCircuitSnapshot(
            degraded=False,
            reason="global_outbox_terminal_backlog_absent",
        )

    async def transfer_delivery_ownership(
        self,
        context: Any,
        request: DeliveryTransferRequest,
    ) -> DeliveryTransferReceipt:
        """Stage an all-or-nothing ledger/outbox/queue ownership transfer."""

        target_state = _validate_request(request)
        ledger_rows = (
            (
                await context.execute(
                    select(GlobalDiscoveryDeliveryLedger).where(
                        or_(
                            GlobalDiscoveryDeliveryLedger.delivery_key
                            == request.delivery_key,
                            and_(
                                GlobalDiscoveryDeliveryLedger.board_id
                                == request.board_id,
                                GlobalDiscoveryDeliveryLedger.artifact_type
                                == request.artifact_type,
                                GlobalDiscoveryDeliveryLedger.artifact_id
                                == request.artifact_id,
                                GlobalDiscoveryDeliveryLedger.generation
                                == request.generation,
                            ),
                            GlobalDiscoveryDeliveryLedger.delete_event_id
                            == request.delete_event_id,
                            (
                                GlobalDiscoveryDeliveryLedger.attempt_event_key
                                == request.attempt_event_key
                                if target_state is DeliveryState.OUTBOX_PERSISTED
                                else False
                            ),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        ledger = next(
            (
                row
                for row in ledger_rows
                if str(row.delivery_key) == request.delivery_key
            ),
            None,
        )
        if any(row is not ledger for row in ledger_rows):
            raise DeliveryTransferReplayConflict(
                "delivery_ledger_unique_identity_conflict"
            )

        replayed = ledger is not None
        if ledger is None:
            attempt_event_key = (
                request.attempt_event_key
                if target_state is DeliveryState.OUTBOX_PERSISTED
                else None
            )
            ledger = GlobalDiscoveryDeliveryLedger(
                delivery_key=request.delivery_key,
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
                delete_event_id=request.delete_event_id,
                state=target_state.value,
                attempt=request.attempt,
                attempt_event_key=attempt_event_key,
            )
            context.add(ledger)
            # Explicit phase flushes surface storage faults at deterministic
            # crash boundaries while all effects remain in one transaction.
            await context.flush([ledger])
            ledger_state = target_state
        else:
            ledger_state = _validate_existing_ledger(ledger, request)

        stored_event_key = (
            str(ledger.attempt_event_key)
            if ledger.attempt_event_key is not None
            else None
        )
        outbox: GlobalUpdateOutbox | None = None
        if stored_event_key is not None:
            outbox = (
                await context.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id == stored_event_key
                    )
                )
            ).scalar_one_or_none()
            if outbox is None:
                if replayed:
                    raise DeliveryTransferReplayConflict(
                        "delivery_ledger_outbox_invariant_broken"
                    )
                outbox = GlobalUpdateOutbox(
                    event_id=request.attempt_event_key,
                    board_id=request.board_id,
                    session_id=request.outbox_session_id,
                    event_type=request.outbox_event_type,
                    payload=dict(request.payload),
                )
                context.add(outbox)
                await context.flush([outbox])
            else:
                _validate_exact_outbox(outbox, request)
        else:
            unexpected_outbox = (
                await context.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id
                        == request.attempt_event_key
                    )
                )
            ).scalar_one_or_none()
            if unexpected_outbox is not None:
                raise DeliveryTransferReplayConflict(
                    "delivery_debt_outbox_invariant_broken"
                )

        transfer_at = request.occurred_at or ledger.created_at
        await _stage_ledger_transition(
            context,
            ledger=ledger,
            state=TakedownState.GRAPH_DEMOTED,
            occurred_at=transfer_at,
            attempt=None,
            source="stale_reconcile",
            details=request.reconcile_details,
        )
        if ledger_state is DeliveryState.OUTBOX_PERSISTED:
            if outbox is None:
                raise DeliveryTransferReplayConflict(
                    "delivery_ledger_outbox_invariant_broken"
                )
            await _stage_ledger_transition(
                context,
                ledger=ledger,
                state=TakedownState.OUTBOX_PERSISTED,
                occurred_at=transfer_at,
                attempt=int(ledger.attempt),
                source="ownership_transfer",
            )
        else:
            await _stage_ledger_transition(
                context,
                ledger=ledger,
                state=TakedownState.DELIVERY_DEBT,
                occurred_at=transfer_at,
                attempt=int(ledger.attempt),
                source="circuit_breaker",
                last_error="delivery_circuit_degraded_at_transfer",
                next_retry_at=transfer_at,
            )

        delete_event_predicate = (
            ConsolidationQueue.delete_event_id == request.delete_event_id
        )
        result = await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.id == request.entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == request.claim_token,
                ConsolidationQueue.board_id == request.board_id,
                ConsolidationQueue.artifact_type == request.artifact_type,
                ConsolidationQueue.artifact_id == request.artifact_id,
                ConsolidationQueue.work_kind == request.work_kind,
                ConsolidationQueue.generation == request.generation,
                delete_event_predicate,
            )
        )
        if int(result.rowcount or 0) != 1:
            raise DeliveryTransferClaimConflict(
                "delivery_transfer_queue_claim_conflict"
            )
        await context.flush()

        return DeliveryTransferReceipt(
            delivery_key=request.delivery_key,
            state=ledger_state,
            attempt=int(ledger.attempt),
            attempt_event_key=stored_event_key,
            replayed=replayed,
        )

    async def apply_attempt_outcomes(
        self,
        context: Any,
        outcomes: Sequence[DeliveryAttemptResult],
    ) -> None:
        """Stage current-owner terminal outcomes in the caller transaction.

        ``delivered`` is absorbing and an older physical attempt is harmless
        after redrive advanced the logical owner.  Every other CAS miss is a
        typed invariant failure rather than an implicit success.
        """

        for result in outcomes:
            if not isinstance(result, DeliveryAttemptResult):
                raise ValueError("delivery_attempt_result_invalid")
            envelope = result.envelope
            delivered = result.outcome is DeliveryAttemptOutcome.DELIVERED
            values: dict[str, object] = {
                "state": (
                    DeliveryState.DELIVERED.value
                    if delivered
                    else DeliveryState.DELIVERY_DEBT.value
                ),
                "last_error": None if delivered else result.error,
                "next_retry_at": None if delivered else result.occurred_at,
                "updated_at": result.occurred_at,
                "delivered_at": result.occurred_at if delivered else None,
            }
            changed = await context.execute(
                update(GlobalDiscoveryDeliveryLedger)
                .where(
                    GlobalDiscoveryDeliveryLedger.delivery_key
                    == envelope.delivery_key,
                    GlobalDiscoveryDeliveryLedger.board_id
                    == envelope.board_id,
                    GlobalDiscoveryDeliveryLedger.artifact_type
                    == envelope.artifact_type,
                    GlobalDiscoveryDeliveryLedger.artifact_id
                    == envelope.artifact_id,
                    GlobalDiscoveryDeliveryLedger.generation
                    == envelope.generation,
                    GlobalDiscoveryDeliveryLedger.delete_event_id
                    == envelope.delete_event_id,
                    GlobalDiscoveryDeliveryLedger.attempt == envelope.attempt,
                    GlobalDiscoveryDeliveryLedger.attempt_event_key
                    == envelope.attempt_event_key,
                    GlobalDiscoveryDeliveryLedger.state
                    != DeliveryState.DELIVERED.value,
                )
                .values(**values)
            )
            if int(changed.rowcount or 0) == 1:
                current = await context.get(
                    GlobalDiscoveryDeliveryLedger,
                    envelope.delivery_key,
                    populate_existing=True,
                )
                if current is None:
                    raise DeliveryAttemptMutationConflict(
                        "delivery_attempt_owner_missing_or_divergent"
                    )
                await _stage_ledger_transition(
                    context,
                    ledger=current,
                    state=(
                        TakedownState.DELIVERED
                        if delivered
                        else TakedownState.DELIVERY_DEBT
                    ),
                    occurred_at=result.occurred_at,
                    attempt=envelope.attempt,
                    source="global_outbox_attempt",
                    last_error=None if delivered else result.error,
                    next_retry_at=None if delivered else result.occurred_at,
                )
                continue

            current = await context.get(
                GlobalDiscoveryDeliveryLedger,
                envelope.delivery_key,
                populate_existing=True,
            )
            if current is None or not _ledger_identity_matches_envelope(
                current,
                envelope,
            ):
                raise DeliveryAttemptMutationConflict(
                    "delivery_attempt_owner_missing_or_divergent"
                )
            if (
                _state(current.state) is DeliveryState.DELIVERED
                or int(current.attempt) > envelope.attempt
            ):
                continue
            raise DeliveryAttemptMutationConflict(
                "delivery_attempt_owner_cas_conflict"
            )

    async def reconcile_orphaned_attempts(
        self,
        context: Any,
        *,
        board_id: str,
        now: datetime,
        limit: int,
    ) -> DeliveryMaintenanceReceipt:
        """Repair one bounded, restart-safe board-local watchdog page."""

        _validate_maintenance_scope(
            board_id=board_id,
            now=now,
            limit=limit,
        )
        if limit == 0:
            return DeliveryMaintenanceReceipt(scanned=0)

        await context.execute(
            sqlite_insert(GlobalDiscoveryDeliveryWatchdogControl)
            .values(board_id=board_id, checkpoint_version=0)
            .on_conflict_do_nothing(index_elements=["board_id"])
        )
        control = (
            await context.execute(
                select(GlobalDiscoveryDeliveryWatchdogControl).where(
                    GlobalDiscoveryDeliveryWatchdogControl.board_id
                    == board_id
                )
            )
        ).scalar_one()
        checkpoint_version = int(control.checkpoint_version)

        cursor_rotation = literal(0)
        if (
            control.cursor_updated_at is not None
            and control.cursor_delivery_key is not None
        ):
            after_cursor = or_(
                GlobalDiscoveryDeliveryLedger.updated_at
                > control.cursor_updated_at,
                and_(
                    GlobalDiscoveryDeliveryLedger.updated_at
                    == control.cursor_updated_at,
                    GlobalDiscoveryDeliveryLedger.delivery_key
                    > control.cursor_delivery_key,
                ),
            )
            cursor_rotation = case((after_cursor, 0), else_=1)

        ledgers = (
            (
                await context.execute(
                    select(GlobalDiscoveryDeliveryLedger)
                    .where(
                        GlobalDiscoveryDeliveryLedger.board_id == board_id,
                        GlobalDiscoveryDeliveryLedger.state
                        == DeliveryState.OUTBOX_PERSISTED.value,
                    )
                    .order_by(
                        cursor_rotation.asc(),
                        GlobalDiscoveryDeliveryLedger.updated_at.asc(),
                        GlobalDiscoveryDeliveryLedger.delivery_key.asc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        resume_updated_at = ledgers[-1].updated_at if ledgers else None
        resume_delivery_key = (
            str(ledgers[-1].delivery_key) if ledgers else None
        )
        transitioned = 0
        concurrency_lost = 0

        for ledger in ledgers:
            expected_key: str | None = None
            error: str | None = None
            delivered = False
            try:
                envelope = _envelope_from_ledger(ledger)
                expected_key = envelope.attempt_event_key
                if str(ledger.attempt_event_key) != expected_key:
                    error = _WATCHDOG_OUTBOX_INVALID
                else:
                    outbox = (
                        await context.execute(
                            select(GlobalUpdateOutbox).where(
                                GlobalUpdateOutbox.event_id == expected_key
                            )
                        )
                    ).scalar_one_or_none()
                    if outbox is None:
                        error = _WATCHDOG_OUTBOX_MISSING
                    else:
                        try:
                            persisted = parse_delivery_attempt_event(outbox)
                        except DeliveryAttemptContractError:
                            error = _WATCHDOG_OUTBOX_INVALID
                        else:
                            if persisted != envelope:
                                error = _WATCHDOG_OUTBOX_INVALID
                            elif outbox.processed_at is not None:
                                delivered = True
                            elif _is_terminal_outbox(outbox):
                                error = _WATCHDOG_OUTBOX_TERMINAL
                            else:
                                continue
            except DeliveryAttemptContractError:
                error = _WATCHDOG_OUTBOX_INVALID

            state = (
                DeliveryState.DELIVERED.value
                if delivered
                else DeliveryState.DELIVERY_DEBT.value
            )
            changed = await context.execute(
                update(GlobalDiscoveryDeliveryLedger)
                .where(
                    GlobalDiscoveryDeliveryLedger.delivery_key
                    == str(ledger.delivery_key),
                    GlobalDiscoveryDeliveryLedger.board_id == board_id,
                    GlobalDiscoveryDeliveryLedger.state
                    == DeliveryState.OUTBOX_PERSISTED.value,
                    GlobalDiscoveryDeliveryLedger.attempt == int(ledger.attempt),
                    GlobalDiscoveryDeliveryLedger.attempt_event_key
                    == ledger.attempt_event_key,
                )
                .values(
                    state=state,
                    last_error=None if delivered else error,
                    next_retry_at=None if delivered else now,
                    updated_at=now,
                    delivered_at=now if delivered else None,
                )
            )
            if int(changed.rowcount or 0) == 1:
                transitioned += 1
                await _stage_ledger_transition(
                    context,
                    ledger=ledger,
                    state=(
                        TakedownState.DELIVERED
                        if delivered
                        else TakedownState.DELIVERY_DEBT
                    ),
                    occurred_at=now,
                    attempt=int(ledger.attempt),
                    source="delivery_watchdog",
                    last_error=None if delivered else error,
                    next_retry_at=None if delivered else now,
                )
            else:
                concurrency_lost += 1

        if ledgers:
            advanced = await context.execute(
                update(GlobalDiscoveryDeliveryWatchdogControl)
                .where(
                    GlobalDiscoveryDeliveryWatchdogControl.board_id
                    == board_id,
                    GlobalDiscoveryDeliveryWatchdogControl.checkpoint_version
                    == checkpoint_version,
                )
                .values(
                    cursor_updated_at=resume_updated_at,
                    cursor_delivery_key=resume_delivery_key,
                    checkpoint_version=checkpoint_version + 1,
                    updated_at=now,
                )
            )
            if int(advanced.rowcount or 0) != 1:
                raise DeliveryAttemptMutationConflict(
                    "delivery_watchdog_checkpoint_cas_conflict"
                )
            checkpoint_version += 1

        return DeliveryMaintenanceReceipt(
            scanned=len(ledgers),
            transitioned=transitioned,
            concurrency_lost=concurrency_lost,
            checkpoint_version=checkpoint_version,
            resume_board_id=board_id,
        )

    async def redrive_delivery_debt(
        self,
        context: Any,
        *,
        now: datetime,
        limit: int,
    ) -> DeliveryMaintenanceReceipt:
        """Stage one global, bounded and restart-safe fair redrive page.

        The SQL window ranks debt oldest-first inside each board.  The outer
        ordering consumes rank one from every board before rank two, rotating
        the board order after the durable singleton cursor.  Only the bounded
        candidate page is materialized in Python.  Ledger CAS, new outbox rows
        and checkpoint advancement remain in the caller-owned transaction.
        """

        _validate_redrive_scope(now=now, limit=limit)
        if limit == 0:
            return DeliveryMaintenanceReceipt(
                scanned=0,
                has_more=False,
                checkpoint_version=0,
            )

        # INSERT .. ON CONFLICT avoids an unbounded retry loop when two fresh
        # processes race to establish the singleton.  The row and all later
        # mutations still belong to this transaction.
        await context.execute(
            sqlite_insert(GlobalDiscoveryDeliveryRedriveControl)
            .values(id=_REDRIVE_CONTROL_ID, checkpoint_version=0)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        control = (
            await context.execute(
                select(GlobalDiscoveryDeliveryRedriveControl).where(
                    GlobalDiscoveryDeliveryRedriveControl.id
                    == _REDRIVE_CONTROL_ID
                )
            )
        ).scalar_one()
        checkpoint_version = int(control.checkpoint_version)

        oldest = func.coalesce(
            GlobalDiscoveryDeliveryLedger.next_retry_at,
            GlobalDiscoveryDeliveryLedger.updated_at,
        )
        within_board_rotation = literal(0)
        if (
            control.cursor_board_id is not None
            and control.cursor_oldest_at is not None
            and control.cursor_delivery_key is not None
        ):
            after_cursor = or_(
                oldest > control.cursor_oldest_at,
                and_(
                    oldest == control.cursor_oldest_at,
                    GlobalDiscoveryDeliveryLedger.delivery_key
                    > control.cursor_delivery_key,
                ),
            )
            within_board_rotation = case(
                (
                    and_(
                        GlobalDiscoveryDeliveryLedger.board_id
                        == control.cursor_board_id,
                        after_cursor,
                    ),
                    0,
                ),
                (
                    GlobalDiscoveryDeliveryLedger.board_id
                    == control.cursor_board_id,
                    1,
                ),
                else_=0,
            )

        ranked = (
            select(
                GlobalDiscoveryDeliveryLedger.delivery_key.label(
                    "delivery_key"
                ),
                GlobalDiscoveryDeliveryLedger.board_id.label("board_id"),
                oldest.label("oldest_at"),
                func.row_number()
                .over(
                    partition_by=GlobalDiscoveryDeliveryLedger.board_id,
                    order_by=(
                        within_board_rotation.asc(),
                        oldest.asc(),
                        GlobalDiscoveryDeliveryLedger.delivery_key.asc(),
                    ),
                )
                .label("board_rank"),
            )
            .where(_due_debt_predicate(now=now))
            .subquery()
        )
        board_rotation = literal(0)
        if control.cursor_board_id is not None:
            board_rotation = case(
                (ranked.c.board_id > control.cursor_board_id, 0),
                else_=1,
            )
        candidates = (
            await context.execute(
                select(
                    ranked.c.delivery_key,
                    ranked.c.board_id,
                    ranked.c.oldest_at,
                )
                .order_by(
                    ranked.c.board_rank.asc(),
                    board_rotation.asc(),
                    ranked.c.board_id.asc(),
                    ranked.c.oldest_at.asc(),
                    ranked.c.delivery_key.asc(),
                )
                .limit(limit)
            )
        ).all()
        emitted = 0
        concurrency_lost = 0

        for candidate in candidates:
            ledger = await context.get(
                GlobalDiscoveryDeliveryLedger,
                str(candidate.delivery_key),
                populate_existing=True,
            )
            if ledger is None:
                concurrency_lost += 1
                continue
            try:
                current = _envelope_from_ledger(ledger)
                stored_event_key = (
                    str(ledger.attempt_event_key)
                    if ledger.attempt_event_key is not None
                    else None
                )
                # Initial circuit debt legitimately has no attempt-zero row.
                # Every debt produced by a physical attempt must retain that
                # exact immutable key; silently overwriting a forged/missing
                # owner here would hide ledger corruption.
                if not (
                    current.attempt == 0 and stored_event_key is None
                ) and stored_event_key != current.attempt_event_key:
                    raise DeliveryAttemptContractError(
                        "delivery_ledger_attempt_event_key_invalid"
                    )
                envelope = DeliveryAttemptEnvelope(
                    board_id=current.board_id,
                    artifact_type=current.artifact_type,
                    artifact_id=current.artifact_id,
                    generation=current.generation,
                    delete_event_id=current.delete_event_id,
                    attempt=current.attempt + 1,
                )
            except DeliveryAttemptContractError as exc:
                raise DeliveryRedriveConflict(
                    "delivery_redrive_ledger_identity_invalid"
                ) from exc

            changed = await context.execute(
                update(GlobalDiscoveryDeliveryLedger)
                .where(
                    GlobalDiscoveryDeliveryLedger.delivery_key
                    == current.delivery_key,
                    GlobalDiscoveryDeliveryLedger.board_id
                    == current.board_id,
                    GlobalDiscoveryDeliveryLedger.state
                    == DeliveryState.DELIVERY_DEBT.value,
                    GlobalDiscoveryDeliveryLedger.attempt == current.attempt,
                    GlobalDiscoveryDeliveryLedger.attempt_event_key
                    == ledger.attempt_event_key,
                )
                .values(
                    state=DeliveryState.OUTBOX_PERSISTED.value,
                    attempt=envelope.attempt,
                    attempt_event_key=envelope.attempt_event_key,
                    last_error=None,
                    next_retry_at=None,
                    updated_at=now,
                    delivered_at=None,
                )
            )
            if int(changed.rowcount or 0) != 1:
                concurrency_lost += 1
                continue

            preexisting = await context.scalar(
                select(GlobalUpdateOutbox.id).where(
                    GlobalUpdateOutbox.event_id
                    == envelope.attempt_event_key
                )
            )
            if preexisting is not None:
                raise DeliveryRedriveConflict(
                    "delivery_redrive_attempt_key_already_exists"
                )

            outbox = GlobalUpdateOutbox(
                event_id=envelope.attempt_event_key,
                board_id=envelope.board_id,
                session_id=envelope.outbox_session_id,
                event_type=envelope.outbox_event_type,
                payload=dict(envelope.payload),
            )
            try:
                async with context.begin_nested():
                    context.add(outbox)
                    await context.flush([outbox])
            except IntegrityError as exc:
                raise DeliveryRedriveConflict(
                    "delivery_redrive_attempt_key_already_exists"
                ) from exc
            await _stage_ledger_transition(
                context,
                ledger=ledger,
                state=TakedownState.OUTBOX_PERSISTED,
                occurred_at=now,
                attempt=envelope.attempt,
                source="delivery_redrive",
            )
            emitted += 1

        resume_board_id: str | None = (
            str(candidates[-1].board_id) if candidates else None
        )
        if candidates:
            advanced = await context.execute(
                update(GlobalDiscoveryDeliveryRedriveControl)
                .where(
                    GlobalDiscoveryDeliveryRedriveControl.id
                    == _REDRIVE_CONTROL_ID,
                    GlobalDiscoveryDeliveryRedriveControl.checkpoint_version
                    == checkpoint_version,
                )
                .values(
                    cursor_board_id=resume_board_id,
                    cursor_oldest_at=candidates[-1].oldest_at,
                    cursor_delivery_key=str(candidates[-1].delivery_key),
                    checkpoint_version=checkpoint_version + 1,
                    updated_at=now,
                )
            )
            if int(advanced.rowcount or 0) != 1:
                raise DeliveryRedriveConflict(
                    "delivery_redrive_checkpoint_cas_conflict"
                )
            checkpoint_version += 1

        remaining_oldest = await context.scalar(
            select(
                func.min(
                    func.coalesce(
                        GlobalDiscoveryDeliveryLedger.next_retry_at,
                        GlobalDiscoveryDeliveryLedger.updated_at,
                    )
                )
            ).where(_due_debt_predicate(now=now))
        )
        return DeliveryMaintenanceReceipt(
            scanned=len(candidates),
            transitioned=emitted,
            emitted=emitted,
            concurrency_lost=concurrency_lost,
            has_more=remaining_oldest is not None,
            oldest_debt_age_seconds=(
                0.0
                if remaining_oldest is None
                else _elapsed_seconds(
                    now=now,
                    oldest_at=remaining_oldest,
                )
            ),
            checkpoint_version=checkpoint_version,
            resume_board_id=resume_board_id,
        )


__all__ = ["CommunitySqlAlchemyDeliveryLedger"]
