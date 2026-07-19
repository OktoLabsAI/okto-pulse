"""Durable Community store and owned worker for Global Discovery recovery.

The control call only persists and dispatches.  Native graph work runs on a
separate executor while the owned worker advances durable heartbeats.  One
SQLAlchemy table stores the authoritative ``RecoveryRunStatus`` history; the
older Core ``RecoveryAttempt`` test value is deliberately not persisted here.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from hashlib import sha256
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError, wait
from contextlib import suppress
from contextvars import copy_context
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Event, RLock, Thread
from typing import Any, Protocol

from sqlalchemy import (
    and_,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecoveryFenceError,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    GlobalDiscoveryRecoveryAttempt,
    GlobalDiscoveryRecoveryDispatch,
    GlobalDiscoveryRecoverySlot,
    GlobalDiscoveryRecoveryTransition,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryRecovery,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    GlobalDiscoveryRecoveryWorkerInputStore,
    GlobalDiscoveryRecoveryWorkerInputs,
    GlobalDiscoveryWriterContention,
    GlobalDiscoveryWriterFenceLost,
    GlobalDiscoveryWriterLease,
    GlobalDiscoveryRecoveryError,
    GLOBAL_RECOVERY_SLOT_ID,
    MAX_RECOVERY_CUMULATIVE_BUDGET_MS,
    RECOVERY_HEARTBEAT_INTERVAL_MS,
    RECOVERY_WORKER_LEASE_MS,
    RecoveryBindingConflict,
    RecoveryCheckpoint,
    RecoveryCheckpointConflict,
    RecoveryConfirmationState,
    RecoveryControlPlane,
    RecoveryDispatchClaimConflict,
    RecoveryDispatchKind,
    RecoveryInProgress,
    RecoveryLeaseTakeoverPolicy,
    RecoveryPhysicalTruth,
    RecoveryPreparationCommand,
    RecoveryPreparedResult,
    RecoveryProgressCounts,
    RecoveryResumePolicy,
    RecoveryResumeRejected,
    RecoveryRunBinding,
    RecoveryRunNotFound,
    RecoveryRunPhase,
    RecoveryRunState,
    RecoveryRunStatus,
    RecoveryStartCommand,
    RecoveryTerminalAttempt,
    RecoveryTerminalOutcome,
    RecoveryTransitionEvent,
    RecoveryWorkerResult,
    RecoveryWorkerRunStore,
    normalize_recovery_audit_reason,
    recovery_attempt_id,
)


_attempts = GlobalDiscoveryRecoveryAttempt.__table__
_slots = GlobalDiscoveryRecoverySlot.__table__
_dispatches = GlobalDiscoveryRecoveryDispatch.__table__
_transitions = GlobalDiscoveryRecoveryTransition.__table__
_boards = Board.__table__
logger = logging.getLogger("okto_pulse.community.global_discovery_recovery_worker")


_UNCONFIRMED_SENTINEL = "unconfirmed"
_REASON_PENDING_SENTINEL = "reason_pending"
_MANIFEST_PENDING_SENTINEL = "manifest_pending"
_PREFLIGHT_PENDING_SENTINEL = "preflight_pending"
_PREPARATION_FAILURE_REASON = "global_discovery_recovery_preparation_failed"
_PREPARATION_RETRY_EXHAUSTED_REASON = (
    "global_discovery_recovery_preparation_retry_exhausted"
)
_PREPARATION_CANCELLED_REASON = "global_discovery_recovery_preparation_cancelled"
_PREPARATION_BUDGET_EXHAUSTED_REASON = "recovery_attempt_budget_exhausted"
_RECOVERY_WRITER_LEASE_SECONDS = max(
    1,
    (RECOVERY_WORKER_LEASE_MS // 1_000) - 2,
)
_RECOVERY_WRITER_RENEW_INTERVAL_SECONDS = max(
    0.25,
    _RECOVERY_WRITER_LEASE_SECONDS / 3,
)
_DEFAULT_PREPARATION_MAX_ATTEMPTS = 3
_DEFAULT_PREPARATION_RETRY_BACKOFF_SECONDS = 1.0


class RecoveryDispatchStage(str, Enum):
    """Closed durable work-stage vocabulary owned by Community."""

    PREPARATION = "preparation"
    RECOVERY = "recovery"


class RecoveryDispatchState(str, Enum):
    """Closed durable dispatch lifecycle."""

    READY = "ready"
    CLAIMED = "claimed"
    DONE = "done"


class RecoveryPreparationError(RuntimeError):
    """Sanitized preparation failure carrying an explicit retry decision."""

    def __init__(
        self,
        *,
        code: str,
        retryable: bool,
        manifest_ref: str | None = None,
    ) -> None:
        normalized = str(code).strip()
        if (
            not normalized
            or len(normalized) > 128
            or any(
                not (character.islower() or character.isdigit() or character == "_")
                for character in normalized
            )
        ):
            raise ValueError(
                "preparation failure code must be lowercase snake-case and <=128 chars"
            )
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be bool")
        bound_manifest = None
        if manifest_ref is not None:
            bound_manifest = str(manifest_ref).strip()
            if not bound_manifest:
                raise ValueError("manifest_ref must be non-empty when provided")
            if retryable:
                raise ValueError("a published manifest failure cannot be retryable")
        self.code = normalized
        self.retryable = retryable
        self.manifest_ref = bound_manifest
        super().__init__(normalized)


class RecoveryPreparationTerminalError(RecoveryPreparationError):
    """Deterministic preparation failure that must become terminal now."""

    def __init__(self, code: str, *, manifest_ref: str | None = None) -> None:
        super().__init__(code=code, retryable=False, manifest_ref=manifest_ref)


class RecoveryPreparationRetryableError(RecoveryPreparationError):
    """Explicit transient preparation failure eligible for bounded requeue."""

    def __init__(self, code: str) -> None:
        super().__init__(code=code, retryable=True)


class RecoveryPreparationFenceError(RuntimeError):
    """Preparation operation lost its durable claim or reached a stop fence."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class RecoveryDispatchClaim:
    """One fenced, expiring claim over a durable dispatch row."""

    dispatch_id: str
    run_id: str
    attempt_id: str
    epoch: int
    stage: RecoveryDispatchStage
    state: RecoveryDispatchState
    claim_token: str
    worker_id: str
    claimed_at: datetime
    claim_expires_at: datetime
    attempt_count: int
    available_at: datetime
    reconciliation_only: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryDispatchReceipt:
    """Projection returned after an exact-token dispatch acknowledgement."""

    dispatch_id: str
    run_id: str
    attempt_id: str
    epoch: int
    stage: RecoveryDispatchStage
    state: RecoveryDispatchState
    claim_token: str | None
    attempt_count: int
    completed_at: datetime | None
    result: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class RecoveryRequesterAudit:
    """Bounded durable ledger of actors requesting one global preflight."""

    actor_ids: tuple[str, ...]
    request_count: int
    replay_count: int
    overflow_count: int
    first_requested_at: datetime | None
    last_requested_at: datetime | None


class PreparedRecoveryRevoker(Protocol):
    """External, create-only prepared-artifact revocation boundary."""

    def revoke_prepared(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
        revoked_at: datetime,
        requested_by_actor_id: str,
        reason: str | None,
    ) -> object: ...

    def is_prepared_revoked(
        self,
        *,
        run_id: str,
        epoch: int,
        manifest_ref: str,
    ) -> bool: ...

    def resolve_attempt_manifest_ref(
        self,
        *,
        run_id: str,
        epoch: int,
    ) -> str | None: ...


class RecoveryPreparedRevokerUnavailable(RuntimeError):
    code = "recovery_prepared_revoker_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("recovery timestamps must be timezone-aware")
    return value.isoformat()


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted recovery timestamp is not timezone-aware")
    return parsed


def _aware_datetime(value: object) -> datetime:
    """Normalize SQLite's timezone-naive DateTime projection to UTC."""

    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime):
        raise TypeError("persisted recovery value is not a datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _status_values(status: RecoveryRunStatus) -> dict[str, object]:
    counts = status.counts
    physical = status.physical_truth
    return {
        "run_id": status.run_id,
        "epoch": status.epoch,
        "attempt_id": status.attempt_id,
        "actor_id": status.binding.actor_id,
        "confirmation_fingerprint": (
            status.binding.confirmation_fingerprint or _UNCONFIRMED_SENTINEL
        ),
        "manifest_ref": status.binding.manifest_ref or _MANIFEST_PENDING_SENTINEL,
        "preflight_hash": status.binding.preflight_hash or _PREFLIGHT_PENDING_SENTINEL,
        "binding_reason": status.binding.reason or _REASON_PENDING_SENTINEL,
        "state": status.state.value,
        "progress_seq": status.progress_seq,
        "phase": status.phase.value,
        "preparation_state": status.preparation_state,
        "confirmation_state": status.confirmation_state.value,
        "boards_total": counts.boards_total,
        "boards_scanned": counts.boards_scanned,
        "sources_total": counts.sources_total,
        "sources_processed": counts.sources_processed,
        "nodes_written": counts.nodes_written,
        "edges_written": counts.edges_written,
        "outbox_events_drained": counts.outbox_events_drained,
        "errors": counts.errors,
        "heartbeat_at": _stamp(status.heartbeat_at),
        "started_at": _stamp(status.started_at),
        "updated_at": _stamp(status.updated_at),
        "active_elapsed_ms": status.active_elapsed_ms,
        "active_deadline_at": _stamp(status.active_deadline_at),
        "cumulative_active_ms": status.cumulative_active_ms,
        "attempt_budget_ms": status.attempt_budget_ms,
        "prepared_at": status.prepared_at,
        "expires_at": status.expires_at,
        "snapshot_fingerprint": status.snapshot_fingerprint,
        "confirmed_by_actor_id": status.confirmed_by_actor_id,
        "confirmation_consumed_at": status.confirmation_consumed_at,
        "audit_reason": status.audit_reason,
        "cancel_requested_at": (
            None
            if status.cancel_requested_at is None
            else _stamp(status.cancel_requested_at)
        ),
        "cancel_requested_by_actor_id": status.cancel_requested_by_actor_id,
        "cancel_reason": (
            status.audit_reason if status.cancel_requested_at is not None else None
        ),
        "resume_requested_at": status.resume_requested_at,
        "resume_requested_by_actor_id": status.resume_requested_by_actor_id,
        "resume_audit_reason": status.resume_audit_reason,
        "terminal_outcome": (
            None if status.terminal_outcome is None else status.terminal_outcome.value
        ),
        "reason_code": status.reason_code,
        "retryable": status.retryable,
        "supersedes_epoch": status.supersedes_epoch,
        "superseded_by_epoch": status.superseded_by_epoch,
        "physical_journal_phase": (
            None if physical is None else physical.journal_phase
        ),
        "physical_pointer_replaced": (
            None if physical is None else physical.pointer_replaced
        ),
        "physical_rollback_performed": (
            None if physical is None else physical.rollback_performed
        ),
        "physical_evidence_ref": None if physical is None else physical.evidence_ref,
    }


def _row_status(row: Mapping[str, Any]) -> RecoveryRunStatus:
    terminal = row["terminal_outcome"]
    confirmation_fingerprint = str(row["confirmation_fingerprint"])
    manifest_ref = str(row["manifest_ref"])
    preflight_hash = str(row["preflight_hash"])
    binding_reason = str(row["binding_reason"])
    physical = (
        None
        if row["physical_journal_phase"] is None
        else RecoveryPhysicalTruth(
            attempt_id=str(row["attempt_id"]),
            journal_phase=str(row["physical_journal_phase"]),
            pointer_replaced=bool(row["physical_pointer_replaced"]),
            rollback_performed=bool(row["physical_rollback_performed"]),
            evidence_ref=(
                None
                if row["physical_evidence_ref"] is None
                else str(row["physical_evidence_ref"])
            ),
        )
    )
    return RecoveryRunStatus(
        binding=RecoveryRunBinding(
            run_id=str(row["run_id"]),
            actor_id=str(row["actor_id"]),
            confirmation_fingerprint=(
                None
                if confirmation_fingerprint == _UNCONFIRMED_SENTINEL
                else confirmation_fingerprint
            ),
            manifest_ref=(
                None if manifest_ref == _MANIFEST_PENDING_SENTINEL else manifest_ref
            ),
            preflight_hash=(
                None
                if preflight_hash == _PREFLIGHT_PENDING_SENTINEL
                else preflight_hash
            ),
            reason=(
                None if binding_reason == _REASON_PENDING_SENTINEL else binding_reason
            ),
        ),
        epoch=int(row["epoch"]),
        state=RecoveryRunState(str(row["state"])),
        progress_seq=int(row["progress_seq"]),
        phase=str(row["phase"]),
        counts=RecoveryProgressCounts(
            boards_total=int(row["boards_total"]),
            boards_scanned=int(row["boards_scanned"]),
            sources_total=int(row["sources_total"]),
            sources_processed=int(row["sources_processed"]),
            nodes_written=int(row["nodes_written"]),
            edges_written=int(row["edges_written"]),
            outbox_events_drained=int(row["outbox_events_drained"]),
            errors=int(row["errors"]),
        ),
        heartbeat_at=_parse_stamp(str(row["heartbeat_at"])),
        started_at=_parse_stamp(str(row["started_at"])),
        updated_at=_parse_stamp(str(row["updated_at"])),
        active_elapsed_ms=int(row["active_elapsed_ms"]),
        active_deadline_at=_parse_stamp(str(row["active_deadline_at"])),
        cumulative_active_ms=int(row["cumulative_active_ms"]),
        attempt_budget_ms=int(row["attempt_budget_ms"]),
        confirmation_state=RecoveryConfirmationState(str(row["confirmation_state"])),
        prepared_at=(
            None if row["prepared_at"] is None else _aware_datetime(row["prepared_at"])
        ),
        expires_at=(
            None if row["expires_at"] is None else _aware_datetime(row["expires_at"])
        ),
        snapshot_fingerprint=(
            None
            if row["snapshot_fingerprint"] is None
            else str(row["snapshot_fingerprint"])
        ),
        confirmed_by_actor_id=(
            None
            if row["confirmed_by_actor_id"] is None
            else str(row["confirmed_by_actor_id"])
        ),
        confirmation_consumed_at=(
            None
            if row["confirmation_consumed_at"] is None
            else _aware_datetime(row["confirmation_consumed_at"])
        ),
        audit_reason=(
            None if row["audit_reason"] is None else str(row["audit_reason"])
        ),
        cancel_requested_at=(
            None
            if row["cancel_requested_at"] is None
            else _parse_stamp(str(row["cancel_requested_at"]))
        ),
        cancel_requested_by_actor_id=(
            None
            if row["cancel_requested_by_actor_id"] is None
            else str(row["cancel_requested_by_actor_id"])
        ),
        resume_requested_at=(
            None
            if row["resume_requested_at"] is None
            else _aware_datetime(row["resume_requested_at"])
        ),
        resume_requested_by_actor_id=(
            None
            if row["resume_requested_by_actor_id"] is None
            else str(row["resume_requested_by_actor_id"])
        ),
        resume_audit_reason=(
            None
            if row["resume_audit_reason"] is None
            else str(row["resume_audit_reason"])
        ),
        terminal_outcome=(
            None if terminal is None else RecoveryTerminalOutcome(str(terminal))
        ),
        reason_code=str(row["reason_code"]),
        retryable=bool(row["retryable"]),
        supersedes_epoch=(
            None if row["supersedes_epoch"] is None else int(row["supersedes_epoch"])
        ),
        superseded_by_epoch=(
            None
            if row["superseded_by_epoch"] is None
            else int(row["superseded_by_epoch"])
        ),
        physical_truth=physical,
    )


def _dispatch_claim(
    row: Mapping[str, Any],
    *,
    reconciliation_only: bool = False,
) -> RecoveryDispatchClaim:
    return RecoveryDispatchClaim(
        dispatch_id=str(row["dispatch_id"]),
        run_id=str(row["run_id"]),
        attempt_id=str(row["attempt_id"]),
        epoch=int(row["epoch"]),
        stage=RecoveryDispatchStage(str(row["stage"])),
        state=RecoveryDispatchState(str(row["state"])),
        claim_token=str(row["claim_token"]),
        worker_id=str(row["worker_id"]),
        claimed_at=_aware_datetime(row["claimed_at"]),
        claim_expires_at=_aware_datetime(row["claim_expires_at"]),
        attempt_count=int(row["attempt_count"]),
        available_at=_aware_datetime(row["available_at"]),
        reconciliation_only=bool(reconciliation_only),
    )


def _dispatch_receipt(row: Mapping[str, Any]) -> RecoveryDispatchReceipt:
    result = row["result_payload"]
    return RecoveryDispatchReceipt(
        dispatch_id=str(row["dispatch_id"]),
        run_id=str(row["run_id"]),
        attempt_id=str(row["attempt_id"]),
        epoch=int(row["epoch"]),
        stage=RecoveryDispatchStage(str(row["stage"])),
        state=RecoveryDispatchState(str(row["state"])),
        claim_token=(None if row["claim_token"] is None else str(row["claim_token"])),
        attempt_count=int(row["attempt_count"]),
        completed_at=(
            None
            if row["completed_at"] is None
            else _aware_datetime(row["completed_at"])
        ),
        result=(None if result is None else dict(result)),
    )


class RecoveryStoreSchemaError(RuntimeError):
    def __init__(self, *, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class RecoveryStoreContractError(RuntimeError):
    """Typed refusal of an obsolete or incoherent store entry point."""

    def __init__(self, *, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class SQLAlchemyRecoveryTransitionObserver:
    """Durable exactly-once transition ledger and bounded metric projection."""

    def __init__(
        self,
        *,
        engine: Engine,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not callable(wall_clock):
            raise TypeError("wall_clock must be callable")
        self._engine = engine
        self._wall_clock = wall_clock

    @staticmethod
    def _event_id(event: RecoveryTransitionEvent) -> str:
        identity = "\x1f".join(
            (
                event.run_id,
                event.attempt_id,
                str(event.epoch),
                str(event.progress_seq),
                event.operation,
            )
        )
        return sha256(identity.encode("utf-8")).hexdigest()

    @classmethod
    def record_in_transaction(
        cls,
        connection: Connection,
        *,
        event: RecoveryTransitionEvent,
        observed_at: datetime,
    ) -> bool:
        if not isinstance(event, RecoveryTransitionEvent):
            raise TypeError("event must be RecoveryTransitionEvent")
        observed = _aware_datetime(observed_at)
        values = {
            "event_id": cls._event_id(event),
            "run_id": event.run_id,
            "attempt_id": event.attempt_id,
            "epoch": event.epoch,
            "progress_seq": event.progress_seq,
            "operation": event.metric_labels["operation"],
            "outcome": event.metric_labels["outcome"],
            "phase": event.metric_labels["phase"],
            "reason_code": event.metric_labels["reason_code"],
            "state": event.state.value,
            "metric_labels": event.metric_labels,
            "observed_at": observed,
        }
        inserted = connection.execute(
            sqlite_insert(_transitions)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=(
                    "run_id",
                    "attempt_id",
                    "epoch",
                    "progress_seq",
                )
            )
        )
        if inserted.rowcount == 1:
            return True
        existing = (
            connection.execute(
                select(_transitions).where(
                    and_(
                        _transitions.c.run_id == event.run_id,
                        _transitions.c.attempt_id == event.attempt_id,
                        _transitions.c.epoch == event.epoch,
                        _transitions.c.progress_seq == event.progress_seq,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is None or any(
            existing[field] != values[field]
            for field in (
                "event_id",
                "operation",
                "outcome",
                "phase",
                "reason_code",
                "state",
                "metric_labels",
            )
        ):
            raise RecoveryStoreSchemaError(
                code="global_discovery_recovery_transition_identity_conflict"
            )
        return False

    def on_transition(self, event: RecoveryTransitionEvent) -> None:
        with self._engine.begin() as connection:
            self.record_in_transaction(
                connection,
                event=event,
                observed_at=self._wall_clock(),
            )

    def metric_snapshot(self, *, run_id: str | None = None) -> dict[str, int]:
        predicates = () if run_id is None else (_transitions.c.run_id == str(run_id),)
        statement = (
            select(
                _transitions.c.operation,
                _transitions.c.outcome,
                _transitions.c.phase,
                _transitions.c.reason_code,
                func.count().label("count"),
            )
            .where(*predicates)
            .group_by(
                _transitions.c.operation,
                _transitions.c.outcome,
                _transitions.c.phase,
                _transitions.c.reason_code,
            )
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return {
            ":".join(
                (
                    str(row["operation"]),
                    str(row["outcome"]),
                    str(row["phase"]),
                    str(row["reason_code"]),
                )
            ): int(row["count"])
            for row in rows
        }


class SQLAlchemyRecoveryRunStore:
    """Synchronous SQLAlchemy CAS store over one authoritative attempt table."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        engine: Engine | None = None,
        prepared_revoker: PreparedRecoveryRevoker | None = None,
        resume_input_handoff: (
            Callable[[RecoveryRunStatus, RecoveryRunStatus], None] | None
        ) = None,
        wall_clock: Callable[[], datetime] = _utc_now,
        realm_id: str = LOCAL_REALM_ID,
    ) -> None:
        if (database_url is None) == (engine is None):
            raise ValueError("provide exactly one of database_url or engine")
        if engine is None:
            connect_args: dict[str, object] = {}
            if str(database_url).startswith("sqlite"):
                connect_args = {"check_same_thread": False, "timeout": 0.75}
            engine = create_engine(
                str(database_url),
                future=True,
                connect_args=connect_args,
            )
        self._engine = engine
        inspector = inspect(self._engine)
        if not inspector.has_table(_attempts.name):
            raise RecoveryStoreSchemaError(
                code="global_discovery_recovery_schema_missing"
            )
        self._has_r5_schema = all(
            inspector.has_table(table.name)
            for table in (_slots, _dispatches, _transitions)
        )
        if prepared_revoker is not None and not (
            callable(getattr(prepared_revoker, "revoke_prepared", None))
            and callable(getattr(prepared_revoker, "is_prepared_revoked", None))
        ):
            raise TypeError(
                "prepared_revoker must implement revoke_prepared and "
                "is_prepared_revoked"
            )
        if not callable(wall_clock):
            raise TypeError("wall_clock must be callable")
        if resume_input_handoff is not None and not callable(resume_input_handoff):
            raise TypeError("resume_input_handoff must be callable")
        normalized_realm_id = str(realm_id).strip()
        if normalized_realm_id != LOCAL_REALM_ID:
            raise ValueError("Community recovery must use LOCAL_REALM_ID")
        self._prepared_revoker = prepared_revoker
        self._resume_input_handoff = resume_input_handoff
        self._wall_clock = wall_clock
        self._transition_observer = SQLAlchemyRecoveryTransitionObserver(
            engine=self._engine,
            wall_clock=wall_clock,
        )
        self._realm_id = normalized_realm_id
        self._revocation_lock = RLock()
        self._revocation_threads: dict[tuple[str, int], Thread] = {}
        if prepared_revoker is not None and self._has_r5_schema:
            self._schedule_pending_prepared_cancellations()

    @property
    def engine(self) -> Engine:
        return self._engine

    def create_run(
        self,
        command: RecoveryStartCommand,
    ) -> tuple[RecoveryRunStatus, bool]:
        del command
        raise RecoveryStoreContractError(
            code="global_discovery_recovery_two_stage_required"
        )

    def get_status(self, *, run_id: str) -> RecoveryRunStatus | None:
        with self._engine.connect() as connection:
            return self._latest(connection, str(run_id))

    def get_status_at_epoch(
        self, *, run_id: str, epoch: int
    ) -> RecoveryRunStatus | None:
        """Read the persisted attempt record for one EXACT epoch (the latest-only
        projection cannot expose a superseded predecessor)."""

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(_attempts).where(
                        _attempts.c.run_id == str(run_id),
                        _attempts.c.epoch == int(epoch),
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _row_status(row)

    def get_requester_audit(self, *, run_id: str) -> RecoveryRequesterAudit | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        _attempts.c.requester_actor_ids_json,
                        _attempts.c.request_count,
                        _attempts.c.replay_count,
                        _attempts.c.requester_actor_overflow_count,
                        _attempts.c.first_requested_at,
                        _attempts.c.last_requested_at,
                    )
                    .where(_attempts.c.run_id == str(run_id))
                    .order_by(_attempts.c.epoch.desc())
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        try:
            actor_ids = json.loads(str(row["requester_actor_ids_json"]))
        except (TypeError, ValueError) as exc:
            raise RecoveryStoreSchemaError(
                code="global_discovery_recovery_requester_audit_corrupt"
            ) from exc
        if (
            not isinstance(actor_ids, list)
            or any(
                not isinstance(value, str) or not value or len(value) > 255
                for value in actor_ids
            )
            or len(actor_ids) > 32
            or actor_ids != sorted(set(actor_ids))
        ):
            raise RecoveryStoreSchemaError(
                code="global_discovery_recovery_requester_audit_corrupt"
            )
        return RecoveryRequesterAudit(
            actor_ids=tuple(actor_ids),
            request_count=int(row["request_count"]),
            replay_count=int(row["replay_count"]),
            overflow_count=int(row["requester_actor_overflow_count"]),
            first_requested_at=(
                None
                if row["first_requested_at"] is None
                else _aware_datetime(row["first_requested_at"])
            ),
            last_requested_at=(
                None
                if row["last_requested_at"] is None
                else _aware_datetime(row["last_requested_at"])
            ),
        )

    def request_cancel(
        self,
        *,
        run_id: str,
        expected_epoch: int,
        requested_at: datetime,
        requested_by_actor_id: str | None = None,
        reason: str | None = None,
    ) -> RecoveryRunStatus:
        requested = self._require_dispatch_time(
            requested_at,
            field="requested_at",
        )
        requested_by_override = (
            None
            if requested_by_actor_id is None
            else self._require_bounded_text(
                requested_by_actor_id,
                field="requested_by_actor_id",
                max_length=255,
            )
        )
        prepared_intent: RecoveryRunStatus | None = None
        for _ in range(8):
            with self._engine.begin() as connection:
                current = self._require_latest(connection, run_id)
                self._require_epoch(current, expected_epoch=expected_epoch)
                requested_by = requested_by_override or current.actor_id
                if (
                    current.state is RecoveryRunState.PENDING
                    and current.phase is RecoveryRunPhase.PREPARED
                    and current.binding.manifest_ref is not None
                ):
                    self._require_prepared_revoker()
                    updated = current.request_cancel(
                        requested_at=requested,
                        requested_by_actor_id=requested_by,
                        reason=reason,
                    )
                    if updated is current:
                        prepared_intent = current
                    else:
                        self._lock_slot(
                            connection,
                            status=current,
                            at=requested,
                        )
                        if not self._write_cas(
                            connection,
                            current=current,
                            updated=updated,
                        ):
                            continue
                        prepared_intent = updated
                else:
                    updated = current.request_cancel(
                        requested_at=requested,
                        requested_by_actor_id=requested_by,
                        reason=reason,
                    )
                    if updated is current:
                        return current
                    preparation_dispatch = self._attempt_dispatch_row(
                        connection,
                        run_id=updated.run_id,
                        attempt_id=updated.attempt_id,
                        epoch=updated.epoch,
                        stage=RecoveryDispatchStage.PREPARATION,
                    )
                    if (
                        preparation_dispatch is not None
                        and str(preparation_dispatch["state"])
                        == RecoveryDispatchState.READY.value
                    ):
                        return self._terminalize_preparation_in_transaction(
                            connection,
                            current=updated,
                            cas_current=current,
                            dispatch=preparation_dispatch,
                            completed_at=requested,
                            active_elapsed_ms=updated.active_elapsed_ms,
                            counts=updated.counts,
                            outcome=RecoveryTerminalOutcome.CANCELLED,
                            reason_code=_PREPARATION_CANCELLED_REASON,
                        )
                    if self._write_cas(
                        connection,
                        current=current,
                        updated=updated,
                    ):
                        return updated
            if prepared_intent is not None:
                thread = self._schedule_prepared_cancellation(prepared_intent)
                return self._briefly_observe_prepared_cancellation(
                    intent=prepared_intent,
                    thread=thread,
                )
        actual = self.get_status(run_id=str(run_id))
        if actual is None:
            raise RecoveryRunNotFound(run_id=str(run_id))
        raise RecoveryCheckpointConflict(
            code="recovery_progress_conflict",
            run_id=str(run_id),
            expected_epoch=current.epoch,
            actual_epoch=actual.epoch,
            expected_progress_seq=current.progress_seq,
            actual_progress_seq=actual.progress_seq,
        )

    def mark_running(
        self,
        *,
        run_id: str,
        epoch: int,
        at: datetime,
    ) -> RecoveryRunStatus:
        with self._engine.begin() as connection:
            current = self._require_latest(connection, run_id)
            self._require_epoch(current, expected_epoch=epoch)
            updated = current.mark_running(at=at)
            if updated is current:
                return current
            if not self._write_cas(connection, current=current, updated=updated):
                raise self._progress_conflict(connection, current)
            return updated

    def compare_and_set_checkpoint(
        self,
        checkpoint: RecoveryCheckpoint,
    ) -> RecoveryRunStatus:
        with self._engine.begin() as connection:
            current = self._require_latest(connection, checkpoint.run_id)
            updated = current.advance_checkpoint(checkpoint)
            if not self._write_cas(connection, current=current, updated=updated):
                raise self._progress_conflict(connection, current)
            return updated

    def complete(
        self,
        *,
        run_id: str,
        epoch: int,
        expected_progress_seq: int,
        completed_at: datetime,
        active_elapsed_ms: int,
        result: RecoveryWorkerResult,
    ) -> RecoveryRunStatus:
        del (
            run_id,
            epoch,
            expected_progress_seq,
            completed_at,
            active_elapsed_ms,
            result,
        )
        raise RecoveryStoreContractError(
            code="global_discovery_recovery_claim_token_required"
        )

    @staticmethod
    def _bounded_terminal_elapsed_ms(
        status: RecoveryRunStatus,
        requested_elapsed_ms: int,
    ) -> tuple[int, bool, str]:
        requested = max(status.active_elapsed_ms, int(requested_elapsed_ms))
        previous_attempts_ms = status.cumulative_active_ms - status.active_elapsed_ms
        cumulative_allowance_ms = max(
            0,
            MAX_RECOVERY_CUMULATIVE_BUDGET_MS - previous_attempts_ms,
        )
        active_cap_ms = min(
            status.attempt_budget_ms,
            cumulative_allowance_ms,
        )
        bounded = min(requested, active_cap_ms)
        exhausted = requested >= active_cap_ms
        reason_code = (
            "recovery_cumulative_budget_exhausted"
            if cumulative_allowance_ms < status.attempt_budget_ms
            else "recovery_attempt_budget_exhausted"
        )
        return bounded, exhausted, reason_code

    @staticmethod
    def _recovery_result_payload(
        terminal: RecoveryRunStatus,
    ) -> dict[str, object]:
        physical = terminal.physical_truth
        return {
            "outcome": (
                None
                if terminal.terminal_outcome is None
                else terminal.terminal_outcome.value
            ),
            "reason_code": terminal.reason_code,
            "retryable": terminal.retryable,
            "counts": terminal.counts.to_dict(),
            "physical_truth": (
                None
                if physical is None
                else {
                    "attempt_id": physical.attempt_id,
                    "journal_phase": physical.journal_phase,
                    "pointer_replaced": physical.pointer_replaced,
                    "rollback_performed": physical.rollback_performed,
                    "evidence_ref": physical.evidence_ref,
                }
            ),
        }

    def _complete_recovery_in_transaction(
        self,
        connection: Connection,
        *,
        current: RecoveryRunStatus,
        dispatch: Mapping[str, Any],
        claim_token: str | None,
        completed_at: datetime,
        active_elapsed_ms: int,
        result: RecoveryWorkerResult,
        require_claimed: bool = True,
        cas_current: RecoveryRunStatus | None = None,
    ) -> RecoveryRunStatus:
        if not isinstance(result, RecoveryWorkerResult):
            raise TypeError("result must be RecoveryWorkerResult")
        observed = self._require_dispatch_time(
            completed_at,
            field="completed_at",
        )
        if (
            str(dispatch["stage"]) != RecoveryDispatchStage.RECOVERY.value
            or str(dispatch["run_id"]) != current.run_id
            or str(dispatch["attempt_id"]) != current.attempt_id
            or int(dispatch["epoch"]) != current.epoch
        ):
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )
        dispatch_state = str(dispatch["state"])
        persisted_token = (
            None if dispatch["claim_token"] is None else str(dispatch["claim_token"])
        )
        if dispatch_state == RecoveryDispatchState.DONE.value:
            if claim_token is None or persisted_token == str(claim_token):
                return current
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )
        claimed = dispatch_state == RecoveryDispatchState.CLAIMED.value
        token_is_exact = bool(
            claim_token is not None
            and str(claim_token).strip()
            and persisted_token == str(claim_token)
        )
        if (
            (claimed and not token_is_exact)
            or (require_claimed and not claimed)
            or (
                not claimed
                and claim_token is not None
                and persisted_token != str(claim_token)
            )
        ):
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )

        cas_base = current if cas_current is None else cas_current
        if (
            cas_base.run_id != current.run_id
            or cas_base.attempt_id != current.attempt_id
            or cas_base.epoch != current.epoch
        ):
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )
        # Lock order is global slot -> attempt CAS -> exact dispatch token.
        # The transaction releases in reverse after the dispatch is terminal.
        terminal_at = max(current.updated_at, observed)
        self._lock_slot(connection, status=current, at=terminal_at)
        bounded_elapsed, budget_exhausted, budget_reason = (
            self._bounded_terminal_elapsed_ms(current, active_elapsed_ms)
        )
        terminal_result = result
        if _is_pending_reconciliation_result(result):
            # B6.1: the EXACT unknown post-pointer reconciliation-pending sentinel
            # is NEVER rewritten by a late cancel/deadline (that would erase an
            # unknown physical outcome after the pointer may have crossed).  A late
            # cancel/deadline is still recorded upstream, but this SQL override
            # layer must preserve the pending truth verbatim.
            terminal_result = result
        elif current.cancel_requested_at is not None and result.physical_truth is None:
            terminal_result = RecoveryWorkerResult(
                outcome=RecoveryTerminalOutcome.CANCELLED,
                reason_code="recovery_cancelled",
                retryable=False,
                counts=current.counts,
            )
        elif result.physical_truth is None and (
            budget_exhausted or terminal_at >= current.active_deadline_at
        ):
            terminal_result = RecoveryWorkerResult(
                outcome=RecoveryTerminalOutcome.TIMEOUT,
                reason_code=budget_reason,
                retryable=False,
                counts=current.counts,
            )
        terminal = current.complete(
            result=terminal_result,
            completed_at=terminal_at,
            active_elapsed_ms=bounded_elapsed,
        )
        if not self._write_cas(
            connection,
            current=cas_base,
            updated=terminal,
        ):
            raise self._progress_conflict(connection, cas_base)
        acknowledged = connection.execute(
            update(_dispatches)
            .where(and_(*self._dispatch_ownership_predicates(dispatch)))
            .values(
                state=RecoveryDispatchState.DONE.value,
                completed_at=terminal_at,
                result_payload=self._recovery_result_payload(terminal),
            )
        )
        if acknowledged.rowcount != 1:
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )
        released = connection.execute(
            delete(_slots).where(
                and_(
                    _slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID,
                    _slots.c.run_id == current.run_id,
                    _slots.c.attempt_id == current.attempt_id,
                    _slots.c.epoch == current.epoch,
                )
            )
        )
        if released.rowcount != 1:
            raise RecoveryInProgress()
        return terminal

    def complete_recovery(
        self,
        *,
        dispatch_id: str,
        claim_token: str,
        expected_progress_seq: int,
        completed_at: datetime,
        active_elapsed_ms: int,
        result: RecoveryWorkerResult,
    ) -> RecoveryRunStatus:
        """Atomically consume one exact RECOVERY claim and release the slot."""

        token = self._require_non_empty_text(
            claim_token,
            field="claim_token",
        )
        with self._engine.begin() as connection:
            dispatch = self._dispatch_row(connection, dispatch_id=dispatch_id)
            if dispatch is None:
                raise RecoveryDispatchClaimConflict(run_id="unknown", epoch=0)
            current = self._require_latest(connection, str(dispatch["run_id"]))
            if (
                current.state.is_terminal
                and str(dispatch["state"]) == RecoveryDispatchState.DONE.value
                and str(dispatch["claim_token"]) == token
            ):
                return current
            if current.progress_seq != int(expected_progress_seq):
                raise self._progress_conflict(connection, current)
            return self._complete_recovery_in_transaction(
                connection,
                current=current,
                dispatch=dispatch,
                claim_token=token,
                completed_at=completed_at,
                active_elapsed_ms=active_elapsed_ms,
                result=result,
            )

    def admit_explicit_resume(
        self,
        *,
        run_id: str,
        expected_epoch: int,
        requested_at: datetime,
        requested_by_actor_id: str,
        reason: str | None = None,
    ) -> tuple[RecoveryRunStatus, bool]:
        """Atomically supersede one attempt, transfer the slot, and dispatch."""

        requested = self._require_dispatch_time(
            requested_at,
            field="requested_at",
        )
        requested_by = self._require_bounded_text(
            requested_by_actor_id,
            field="requested_by_actor_id",
            max_length=255,
        )
        audit_reason = normalize_recovery_audit_reason(reason)

        rejection: RecoveryResumeRejected | None = None
        try:
            with self._engine.begin() as connection:
                current = self._require_latest(connection, run_id)
                if (
                    current.epoch == int(expected_epoch) + 1
                    and current.supersedes_epoch == int(expected_epoch)
                    and current.resume_requested_by_actor_id == requested_by
                    and current.resume_audit_reason == audit_reason
                ):
                    slot = self._slot_row(connection)
                    stage = (
                        RecoveryDispatchStage.PREPARATION
                        if current.phase
                        in {RecoveryRunPhase.QUEUED, RecoveryRunPhase.PREPARING}
                        else RecoveryDispatchStage.RECOVERY
                    )
                    dispatch = self._attempt_dispatch_row(
                        connection,
                        run_id=current.run_id,
                        attempt_id=current.attempt_id,
                        epoch=current.epoch,
                        stage=stage,
                    )
                    if (
                        slot is None
                        or dispatch is None
                        or (
                            str(slot["run_id"]),
                            str(slot["attempt_id"]),
                            int(slot["epoch"]),
                        )
                        != (current.run_id, current.attempt_id, current.epoch)
                    ):
                        raise RecoveryStoreSchemaError(
                            code="global_discovery_recovery_resume_commit_incomplete"
                        )
                    return current, False
                self._require_epoch(current, expected_epoch=expected_epoch)
                slot = self._slot_row(connection)
                if slot is not None and (
                    str(slot["run_id"]),
                    str(slot["attempt_id"]),
                    int(slot["epoch"]),
                ) != (current.run_id, current.attempt_id, current.epoch):
                    raise RecoveryInProgress()

                if current.state is RecoveryRunState.RUNNING:
                    decision = RecoveryLeaseTakeoverPolicy().evaluate(
                        current,
                        now=requested,
                    )
                    if not decision.admitted:
                        rejection = RecoveryResumeRejected(
                            code=decision.reason_code,
                            run_id=current.run_id,
                            epoch=current.epoch,
                        )
                        if decision.reason_code not in {
                            "recovery_attempt_budget_exhausted",
                            "recovery_cumulative_budget_exhausted",
                        }:
                            raise rejection
                    previous_attempts_ms = (
                        current.cumulative_active_ms - current.active_elapsed_ms
                    )
                    charged_active_ms = (
                        decision.charged_cumulative_ms - previous_attempts_ms
                    )
                    completed_at = min(
                        decision.lease_expires_at,
                        current.active_deadline_at,
                    )
                    stage = (
                        RecoveryDispatchStage.PREPARATION
                        if current.phase is RecoveryRunPhase.PREPARING
                        else RecoveryDispatchStage.RECOVERY
                    )
                    incumbent_dispatch = self._attempt_dispatch_row(
                        connection,
                        run_id=current.run_id,
                        attempt_id=current.attempt_id,
                        epoch=current.epoch,
                        stage=stage,
                    )
                    if incumbent_dispatch is None:
                        raise RecoveryDispatchClaimConflict(
                            run_id=current.run_id,
                            epoch=current.epoch,
                        )
                    if stage is RecoveryDispatchStage.RECOVERY:
                        # Physical work may have crossed the pointer boundary
                        # immediately before its lease expired.  Only a new
                        # exact-token claim of this same epoch may inspect and
                        # reconcile that journal; an operator request cannot
                        # blindly manufacture epoch N+1 first.
                        raise RecoveryResumeRejected(
                            code="recovery_physical_reconciliation_pending",
                            run_id=current.run_id,
                            epoch=current.epoch,
                        )
                    if rejection is not None:
                        timeout_result = RecoveryWorkerResult(
                            outcome=RecoveryTerminalOutcome.TIMEOUT,
                            reason_code=decision.reason_code,
                            retryable=False,
                            counts=current.counts,
                        )
                        if stage is RecoveryDispatchStage.RECOVERY:
                            self._complete_recovery_in_transaction(
                                connection,
                                current=current,
                                dispatch=incumbent_dispatch,
                                claim_token=(
                                    None
                                    if incumbent_dispatch["claim_token"] is None
                                    else str(incumbent_dispatch["claim_token"])
                                ),
                                completed_at=completed_at,
                                active_elapsed_ms=charged_active_ms,
                                result=timeout_result,
                                require_claimed=False,
                            )
                        else:
                            self._terminalize_preparation_in_transaction(
                                connection,
                                current=current,
                                dispatch=incumbent_dispatch,
                                completed_at=completed_at,
                                active_elapsed_ms=charged_active_ms,
                                counts=current.counts,
                                outcome=RecoveryTerminalOutcome.TIMEOUT,
                                reason_code=decision.reason_code,
                            )
                    else:
                        previous = current.complete(
                            result=RecoveryWorkerResult(
                                outcome=RecoveryTerminalOutcome.PARTIAL,
                                reason_code="worker_lease_expired",
                                retryable=False,
                                counts=current.counts,
                            ),
                            completed_at=completed_at,
                            active_elapsed_ms=charged_active_ms,
                        )
                        resumed = current.take_over_lease(
                            decision=decision,
                            requested_at=requested,
                            requested_by_actor_id=requested_by,
                            reason=audit_reason,
                        )
                else:
                    if current.terminal_outcome is None:
                        code = (
                            "prepared_run_not_adoptable"
                            if current.phase is RecoveryRunPhase.PREPARED
                            else "recovery_dispatch_not_adoptable"
                        )
                        raise RecoveryResumeRejected(
                            code=code,
                            run_id=current.run_id,
                            epoch=current.epoch,
                        )
                    decision = RecoveryResumePolicy().evaluate(
                        RecoveryTerminalAttempt(
                            run_id=current.run_id,
                            epoch=current.epoch,
                            outcome=current.terminal_outcome,
                            reason_code=current.reason_code,
                            retryable=current.retryable,
                            active_elapsed_ms=current.active_elapsed_ms,
                            cumulative_active_ms=current.cumulative_active_ms,
                        )
                    )
                    if not decision.admitted:
                        raise RecoveryResumeRejected(
                            code=decision.reason_code,
                            run_id=current.run_id,
                            epoch=current.epoch,
                        )
                    previous = current
                    resumed = current.resume_terminal(
                        decision=decision,
                        requested_at=requested,
                        requested_by_actor_id=requested_by,
                        reason=audit_reason,
                    )

                if rejection is None:
                    next_epoch = current.epoch + 1
                    resumed_stage = (
                        RecoveryDispatchStage.PREPARATION
                        if resumed.phase
                        in {RecoveryRunPhase.QUEUED, RecoveryRunPhase.PREPARING}
                        else RecoveryDispatchStage.RECOVERY
                    )
                    if resumed_stage is RecoveryDispatchStage.RECOVERY:
                        if self._resume_input_handoff is None:
                            raise RecoveryStoreContractError(
                                code=(
                                    "global_discovery_recovery_resume_input_"
                                    "handoff_required"
                                )
                            )
                        self._resume_input_handoff(current, resumed)
                    previous = replace(
                        previous,
                        progress_seq=previous.progress_seq + 1,
                        updated_at=max(previous.updated_at, requested),
                        superseded_by_epoch=next_epoch,
                    )
                    if slot is not None:
                        self._lock_slot(
                            connection,
                            status=current,
                            at=requested,
                        )
                    if not self._write_cas(
                        connection,
                        current=current,
                        updated=previous,
                    ):
                        raise self._progress_conflict(connection, current)
                    connection.execute(
                        insert(_attempts).values(**_status_values(resumed))
                    )
                    self._transition_observer.record_in_transaction(
                        connection,
                        event=RecoveryTransitionEvent.from_status(resumed),
                        observed_at=resumed.updated_at,
                    )
                    if current.state is RecoveryRunState.RUNNING:
                        finished = connection.execute(
                            update(_dispatches)
                            .where(
                                and_(
                                    *self._dispatch_ownership_predicates(
                                        incumbent_dispatch
                                    )
                                )
                            )
                            .values(
                                state=RecoveryDispatchState.DONE.value,
                                completed_at=completed_at,
                                result_payload=self._recovery_result_payload(previous),
                            )
                        )
                        if finished.rowcount != 1:
                            raise RecoveryDispatchClaimConflict(
                                run_id=current.run_id,
                                epoch=current.epoch,
                            )
                    if slot is None:
                        connection.execute(
                            insert(_slots).values(
                                slot_id=GLOBAL_RECOVERY_SLOT_ID,
                                run_id=resumed.run_id,
                                attempt_id=resumed.attempt_id,
                                epoch=resumed.epoch,
                                actor_id=resumed.actor_id,
                                version=1,
                                acquired_at=requested,
                                updated_at=requested,
                            )
                        )
                    else:
                        transferred = connection.execute(
                            update(_slots)
                            .where(
                                and_(
                                    _slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID,
                                    _slots.c.run_id == current.run_id,
                                    _slots.c.attempt_id == current.attempt_id,
                                    _slots.c.epoch == current.epoch,
                                )
                            )
                            .values(
                                run_id=resumed.run_id,
                                attempt_id=resumed.attempt_id,
                                epoch=resumed.epoch,
                                actor_id=resumed.actor_id,
                                version=_slots.c.version + 1,
                                updated_at=requested,
                            )
                        )
                        if transferred.rowcount != 1:
                            raise RecoveryInProgress()
                    self._insert_dispatch(
                        connection,
                        status=resumed,
                        stage=resumed_stage,
                        available_at=requested,
                    )
                    return resumed, True
        except IntegrityError:
            actual = self.get_status(run_id=str(run_id))
            if actual is None:
                raise RecoveryRunNotFound(run_id=str(run_id)) from None
            if (
                actual.epoch == int(expected_epoch) + 1
                and actual.supersedes_epoch == int(expected_epoch)
                and actual.resume_requested_by_actor_id == requested_by
                and actual.resume_audit_reason == audit_reason
            ):
                return actual, False
            raise RecoveryCheckpointConflict(
                code="recovery_epoch_conflict",
                run_id=str(run_id),
                expected_epoch=expected_epoch,
                actual_epoch=actual.epoch,
                expected_progress_seq=0,
                actual_progress_seq=actual.progress_seq,
            ) from None
        if rejection is None:  # pragma: no cover - defensive exhaustiveness
            raise RuntimeError("recovery resume completed without a decision")
        raise rejection

    def list_attempts(self, *, run_id: str) -> tuple[RecoveryRunStatus, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(_attempts)
                    .where(_attempts.c.run_id == str(run_id))
                    .order_by(_attempts.c.epoch.asc())
                )
                .mappings()
                .all()
            )
        return tuple(_row_status(row) for row in rows)

    @staticmethod
    def _latest(
        connection: Connection,
        run_id: str,
    ) -> RecoveryRunStatus | None:
        row = (
            connection.execute(
                select(_attempts)
                .where(_attempts.c.run_id == str(run_id))
                .order_by(_attempts.c.epoch.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        return None if row is None else _row_status(row)

    def _require_latest(
        self,
        connection: Connection,
        run_id: str,
    ) -> RecoveryRunStatus:
        current = self._latest(connection, str(run_id))
        if current is None:
            raise RecoveryRunNotFound(run_id=str(run_id))
        return current

    @staticmethod
    def _require_epoch(
        current: RecoveryRunStatus,
        *,
        expected_epoch: int,
    ) -> None:
        if current.epoch != int(expected_epoch):
            raise RecoveryCheckpointConflict(
                code="recovery_epoch_conflict",
                run_id=current.run_id,
                expected_epoch=expected_epoch,
                actual_epoch=current.epoch,
                expected_progress_seq=current.progress_seq,
                actual_progress_seq=current.progress_seq,
            )

    def _write_cas(
        self,
        connection: Connection,
        *,
        current: RecoveryRunStatus,
        updated: RecoveryRunStatus,
    ) -> bool:
        result = connection.execute(
            update(_attempts)
            .where(
                and_(
                    _attempts.c.run_id == current.run_id,
                    _attempts.c.epoch == current.epoch,
                    _attempts.c.progress_seq == current.progress_seq,
                )
            )
            .values(**_status_values(updated))
        )
        if result.rowcount != 1:
            return False
        self._transition_observer.record_in_transaction(
            connection,
            event=RecoveryTransitionEvent.from_status(
                updated,
                previous=current,
            ),
            observed_at=updated.updated_at,
        )
        return True

    def _progress_conflict(
        self,
        connection: Connection,
        expected: RecoveryRunStatus,
    ) -> RecoveryCheckpointConflict:
        actual = self._require_latest(connection, expected.run_id)
        return RecoveryCheckpointConflict(
            code="recovery_progress_conflict",
            run_id=expected.run_id,
            expected_epoch=expected.epoch,
            actual_epoch=actual.epoch,
            expected_progress_seq=expected.progress_seq,
            actual_progress_seq=actual.progress_seq,
        )

    # R5 whole-lifecycle admission and durable dispatch -----------------

    def _require_r5_schema(self) -> None:
        if not self._has_r5_schema:
            raise RecoveryStoreSchemaError(
                code="global_discovery_recovery_r5_schema_missing"
            )

    @staticmethod
    def _bound_admission_lock_wait(connection: Connection) -> None:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA busy_timeout = 750")

    def _require_prepared_revoker(self) -> PreparedRecoveryRevoker:
        revoker = self._prepared_revoker
        if revoker is None:
            raise RecoveryPreparedRevokerUnavailable()
        return revoker

    def _schedule_pending_prepared_cancellations(self) -> None:
        """Resume durable PREPARED cancellation intents after a restart."""

        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(_attempts).where(
                        and_(
                            _attempts.c.state == RecoveryRunState.PENDING.value,
                            _attempts.c.phase == RecoveryRunPhase.PREPARED.value,
                            _attempts.c.cancel_requested_at.is_not(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            self._schedule_prepared_cancellation(_row_status(row))

    def _schedule_prepared_cancellation(
        self,
        status: RecoveryRunStatus,
    ) -> Thread | None:
        if (
            status.state is not RecoveryRunState.PENDING
            or status.phase is not RecoveryRunPhase.PREPARED
            or status.cancel_requested_at is None
            or status.cancel_requested_by_actor_id is None
        ):
            return None
        self._require_prepared_revoker()
        key = (status.run_id, status.epoch)
        with self._revocation_lock:
            existing = self._revocation_threads.get(key)
            if existing is not None and existing.is_alive():
                return existing
            thread = Thread(
                target=self._reconcile_prepared_cancellation,
                kwargs={"run_id": status.run_id, "epoch": status.epoch},
                name=f"pulse-recovery-revoke-{status.epoch}",
                daemon=True,
            )
            self._revocation_threads[key] = thread
            thread.start()
            return thread

    def _briefly_observe_prepared_cancellation(
        self,
        *,
        intent: RecoveryRunStatus,
        thread: Thread | None,
    ) -> RecoveryRunStatus:
        if thread is not None:
            thread.join(timeout=0.05)
        if thread is None or thread.is_alive():
            return intent
        observed = self.get_status(run_id=intent.run_id)
        if observed is None or observed.epoch != intent.epoch:
            return intent
        return observed

    def _reconcile_prepared_cancellation(
        self,
        *,
        run_id: str,
        epoch: int,
    ) -> None:
        key = (str(run_id), int(epoch))
        try:
            current = self.get_status(run_id=key[0])
            if (
                current is None
                or current.epoch != key[1]
                or current.state is not RecoveryRunState.PENDING
                or current.phase is not RecoveryRunPhase.PREPARED
                or current.cancel_requested_at is None
                or current.cancel_requested_by_actor_id is None
            ):
                return
            manifest_ref = current.binding.manifest_ref
            if manifest_ref is None:
                manifest_ref = self._resolve_attempt_manifest_ref(
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            if manifest_ref is None:
                raise RecoveryPreparedRevokerUnavailable()
            revoker = self._require_prepared_revoker()
            if not revoker.is_prepared_revoked(
                run_id=current.run_id,
                epoch=current.epoch,
                manifest_ref=manifest_ref,
            ):
                revoker.revoke_prepared(
                    run_id=current.run_id,
                    epoch=current.epoch,
                    manifest_ref=manifest_ref,
                    revoked_at=current.cancel_requested_at,
                    requested_by_actor_id=(current.cancel_requested_by_actor_id),
                    reason=current.audit_reason,
                )
            with self._engine.begin() as connection:
                latest = self._require_latest(connection, current.run_id)
                if latest.epoch != current.epoch or latest.state.is_terminal:
                    return
                if (
                    latest.phase is not RecoveryRunPhase.PREPARED
                    or latest.cancel_requested_at is None
                    or latest.cancel_requested_by_actor_id is None
                ):
                    return
                self._cancel_prepared_in_transaction(
                    connection,
                    current=latest,
                    requested_at=latest.cancel_requested_at,
                    requested_by_actor_id=(latest.cancel_requested_by_actor_id),
                    reason=latest.audit_reason,
                )
        except BaseException as exc:
            logger.warning(
                "global recovery prepared cancellation reconciliation failed: %s",
                type(exc).__name__,
                extra={"run_id": key[0], "epoch": key[1]},
            )
        finally:
            with self._revocation_lock:
                self._revocation_threads.pop(key, None)

    def _resolve_attempt_manifest_ref(
        self,
        *,
        run_id: str,
        epoch: int,
    ) -> str | None:
        revoker = self._prepared_revoker
        if revoker is None:
            return None
        resolver = getattr(
            revoker,
            "resolve_attempt_manifest_ref",
            None,
        )
        if not callable(resolver):
            return None
        resolved = resolver(run_id=str(run_id), epoch=int(epoch))
        if resolved is None:
            return None
        return self._require_bounded_text(
            resolved,
            field="manifest_ref",
            max_length=1_024,
        )

    @staticmethod
    def _normalize_stage(stage: RecoveryDispatchStage | str) -> RecoveryDispatchStage:
        return RecoveryDispatchStage(stage)

    @staticmethod
    def _require_dispatch_time(value: datetime, *, field: str) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError(f"{field} must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value

    @staticmethod
    def _require_non_empty_text(value: str, *, field: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty")
        return normalized

    @classmethod
    def _require_bounded_text(
        cls,
        value: str,
        *,
        field: str,
        max_length: int,
    ) -> str:
        normalized = cls._require_non_empty_text(value, field=field)
        if len(normalized) > int(max_length):
            raise ValueError(f"{field} must be within 1..{int(max_length)} chars")
        return normalized

    @staticmethod
    def _dispatch_row(
        connection: Connection,
        *,
        dispatch_id: str,
    ) -> Mapping[str, Any] | None:
        return (
            connection.execute(
                select(_dispatches).where(_dispatches.c.dispatch_id == str(dispatch_id))
            )
            .mappings()
            .first()
        )

    @staticmethod
    def _attempt_dispatch_row(
        connection: Connection,
        *,
        run_id: str,
        attempt_id: str,
        epoch: int,
        stage: RecoveryDispatchStage,
    ) -> Mapping[str, Any] | None:
        return (
            connection.execute(
                select(_dispatches).where(
                    and_(
                        _dispatches.c.run_id == str(run_id),
                        _dispatches.c.attempt_id == str(attempt_id),
                        _dispatches.c.epoch == int(epoch),
                        _dispatches.c.stage == stage.value,
                    )
                )
            )
            .mappings()
            .first()
        )

    @staticmethod
    def _slot_row(connection: Connection) -> Mapping[str, Any] | None:
        return (
            connection.execute(
                select(_slots).where(_slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID)
            )
            .mappings()
            .first()
        )

    def _require_slot(
        self,
        connection: Connection,
        *,
        status: RecoveryRunStatus,
    ) -> Mapping[str, Any]:
        slot = self._slot_row(connection)
        if slot is None or (
            str(slot["run_id"]),
            str(slot["attempt_id"]),
            int(slot["epoch"]),
        ) != (status.run_id, status.attempt_id, status.epoch):
            raise RecoveryInProgress()
        return slot

    def _lock_slot(
        self,
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        at: datetime,
    ) -> None:
        slot = self._require_slot(connection, status=status)
        result = connection.execute(
            update(_slots)
            .where(
                and_(
                    _slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID,
                    _slots.c.run_id == status.run_id,
                    _slots.c.attempt_id == status.attempt_id,
                    _slots.c.epoch == status.epoch,
                    _slots.c.version == int(slot["version"]),
                )
            )
            .values(
                version=_slots.c.version + 1,
                updated_at=at,
            )
        )
        if result.rowcount != 1:
            raise RecoveryInProgress()

    @staticmethod
    def _insert_dispatch(
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        stage: RecoveryDispatchStage,
        available_at: datetime,
    ) -> Mapping[str, Any]:
        existing = SQLAlchemyRecoveryRunStore._attempt_dispatch_row(
            connection,
            run_id=status.run_id,
            attempt_id=status.attempt_id,
            epoch=status.epoch,
            stage=stage,
        )
        if existing is not None:
            return existing
        dispatch_id = f"gdrd_{uuid.uuid4().hex}"
        connection.execute(
            insert(_dispatches).values(
                dispatch_id=dispatch_id,
                run_id=status.run_id,
                attempt_id=status.attempt_id,
                epoch=status.epoch,
                stage=stage.value,
                state=RecoveryDispatchState.READY.value,
                claim_token=None,
                worker_id=None,
                claimed_at=None,
                claim_expires_at=None,
                attempt_count=0,
                available_at=available_at,
                completed_at=None,
                result_payload=None,
                transition_event_id=None,
                transition_observed_at=None,
                transition_payload=None,
            )
        )
        created = SQLAlchemyRecoveryRunStore._dispatch_row(
            connection,
            dispatch_id=dispatch_id,
        )
        assert created is not None
        return created

    @staticmethod
    def _preflight_replayable(
        status: RecoveryRunStatus,
        *,
        observed_at: datetime,
    ) -> bool:
        if (
            status.state is RecoveryRunState.PENDING
            and status.phase is RecoveryRunPhase.QUEUED
        ) or (
            status.state is RecoveryRunState.RUNNING
            and status.phase is RecoveryRunPhase.PREPARING
        ):
            return True
        return bool(
            status.state is RecoveryRunState.PENDING
            and status.phase is RecoveryRunPhase.PREPARED
            and status.expires_at is not None
            and observed_at < status.expires_at
        )

    @staticmethod
    def _audit_preflight_requester(
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        requester_actor_id: str,
        requested_at: datetime,
        replay: bool,
    ) -> None:
        attempt_audit = (
            connection.execute(
                select(
                    _attempts.c.requester_actor_ids_json,
                    _attempts.c.request_count,
                    _attempts.c.replay_count,
                    _attempts.c.requester_actor_overflow_count,
                    _attempts.c.first_requested_at,
                    _attempts.c.last_requested_at,
                ).where(
                    and_(
                        _attempts.c.run_id == status.run_id,
                        _attempts.c.epoch == status.epoch,
                    )
                )
            )
            .mappings()
            .first()
        )
        if attempt_audit is None:
            raise RecoveryRunNotFound(run_id=status.run_id)
        try:
            decoded_ids = json.loads(str(attempt_audit["requester_actor_ids_json"]))
        except (TypeError, ValueError) as exc:
            raise RecoveryStoreSchemaError(
                code="global_discovery_recovery_requester_audit_corrupt"
            ) from exc
        if (
            not isinstance(decoded_ids, list)
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 255
                for value in decoded_ids
            )
            or len(decoded_ids) > 32
            or decoded_ids != sorted(set(decoded_ids))
        ):
            raise RecoveryStoreSchemaError(
                code="global_discovery_recovery_requester_audit_corrupt"
            )
        actor = str(requester_actor_id).strip()
        if not actor or len(actor) > 255:
            raise ValueError("requester_actor_id must be within 1..255 chars")
        actor_ids = sorted(set(decoded_ids))
        overflow_increment = 0
        if actor not in actor_ids:
            if len(actor_ids) < 32:
                actor_ids.append(actor)
                actor_ids.sort()
            else:
                overflow_increment = 1
        request_count = int(attempt_audit["request_count"])
        replay_count = int(attempt_audit["replay_count"])
        dispatch = SQLAlchemyRecoveryRunStore._attempt_dispatch_row(
            connection,
            run_id=status.run_id,
            attempt_id=status.attempt_id,
            epoch=status.epoch,
            stage=RecoveryDispatchStage.PREPARATION,
        )
        if dispatch is None:
            raise RecoveryDispatchClaimConflict(
                run_id=status.run_id,
                epoch=status.epoch,
            )
        current_payload = dispatch["transition_payload"]
        payload = dict(current_payload) if isinstance(current_payload, Mapping) else {}
        raw_requesters = payload.get("preflight_requesters", [])
        requesters = [
            dict(row)
            for row in raw_requesters
            if isinstance(row, Mapping) and row.get("actor_id")
        ][:32]
        actor = str(requester_actor_id)
        incumbent = next(
            (row for row in requesters if str(row.get("actor_id")) == actor),
            None,
        )
        if incumbent is None and len(requesters) < 32:
            requesters.append(
                {
                    "actor_id": actor,
                    "first_requested_at": requested_at.isoformat(),
                    "last_requested_at": requested_at.isoformat(),
                    "request_count": 1,
                    "replay_count": 1 if replay else 0,
                }
            )
        elif incumbent is not None:
            incumbent["last_requested_at"] = requested_at.isoformat()
            incumbent["request_count"] = int(incumbent.get("request_count", 0)) + 1
            if replay:
                incumbent["replay_count"] = int(incumbent.get("replay_count", 0)) + 1
        payload.update(
            {
                "preflight_requesters": requesters,
                "preflight_request_count": int(
                    payload.get("preflight_request_count", 0)
                )
                + 1,
                "preflight_requester_overflow": max(
                    0,
                    int(payload.get("preflight_request_count", 0))
                    + 1
                    - sum(int(row.get("request_count", 0)) for row in requesters),
                ),
            }
        )
        dispatch_audit_update = connection.execute(
            update(_dispatches)
            .where(_dispatches.c.dispatch_id == str(dispatch["dispatch_id"]))
            .values(
                transition_event_id=f"preflight-audit:{dispatch['dispatch_id']}",
                transition_observed_at=requested_at,
                transition_payload=payload,
            )
        )
        if dispatch_audit_update.rowcount != 1:
            raise RecoveryInProgress()

        # Keep the same dispatch -> attempt lock order used by heartbeats so
        # the database cannot deadlock replay-audit and claim-renewal writers.
        audit_update = connection.execute(
            update(_attempts)
            .where(
                and_(
                    _attempts.c.run_id == status.run_id,
                    _attempts.c.epoch == status.epoch,
                    _attempts.c.request_count == request_count,
                    _attempts.c.replay_count == replay_count,
                )
            )
            .values(
                requester_actor_ids_json=json.dumps(
                    actor_ids,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                request_count=request_count + 1,
                replay_count=replay_count + (1 if replay else 0),
                requester_actor_overflow_count=int(
                    attempt_audit["requester_actor_overflow_count"]
                )
                + overflow_increment,
                first_requested_at=(
                    attempt_audit["first_requested_at"] or requested_at
                ),
                last_requested_at=requested_at,
            )
        )
        if audit_update.rowcount != 1:
            raise RecoveryInProgress()

    def _revoke_expired_prepared_incumbent(
        self,
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        observed_at: datetime,
        requester_actor_id: str,
    ) -> None:
        if not (
            status.state is RecoveryRunState.PENDING
            and status.phase is RecoveryRunPhase.PREPARED
            and status.expires_at is not None
            and observed_at >= status.expires_at
            and status.binding.manifest_ref is not None
        ):
            raise RecoveryInProgress()
        self._lock_slot(connection, status=status, at=observed_at)
        has_cancel_intent = (
            status.cancel_requested_at is not None
            and status.cancel_requested_by_actor_id is not None
        )
        revocation_at = status.cancel_requested_at if has_cancel_intent else observed_at
        revocation_actor = (
            status.cancel_requested_by_actor_id
            if has_cancel_intent
            else requester_actor_id
        )
        revocation_reason = (
            status.audit_reason if has_cancel_intent else "prepared_manifest_expired"
        )
        revoker = self._require_prepared_revoker()
        revoker.revoke_prepared(
            run_id=status.run_id,
            epoch=status.epoch,
            manifest_ref=status.binding.manifest_ref,
            revoked_at=revocation_at,
            requested_by_actor_id=revocation_actor,
            reason=revocation_reason,
        )
        expired = status.cancel_prepared(
            requested_at=revocation_at,
            requested_by_actor_id=revocation_actor,
            reason=revocation_reason,
        )
        if not self._write_cas(
            connection,
            current=status,
            updated=expired,
        ):
            raise self._progress_conflict(connection, status)
        released = connection.execute(
            delete(_slots).where(
                and_(
                    _slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID,
                    _slots.c.run_id == status.run_id,
                    _slots.c.attempt_id == status.attempt_id,
                    _slots.c.epoch == status.epoch,
                )
            )
        )
        if released.rowcount != 1:
            raise RecoveryInProgress()

    def _release_revoked_prepared_incumbent(
        self,
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        observed_at: datetime,
        requester_actor_id: str,
    ) -> None:
        if not (
            status.state is RecoveryRunState.PENDING
            and status.phase is RecoveryRunPhase.PREPARED
            and status.binding.manifest_ref is not None
        ):
            raise RecoveryInProgress()
        has_cancel_intent = (
            status.cancel_requested_at is not None
            and status.cancel_requested_by_actor_id is not None
        )
        cancelled = status.cancel_prepared(
            requested_at=(
                status.cancel_requested_at if has_cancel_intent else observed_at
            ),
            requested_by_actor_id=(
                status.cancel_requested_by_actor_id
                if has_cancel_intent
                else requester_actor_id
            ),
            reason=(
                status.audit_reason
                if has_cancel_intent
                else "prepared_manifest_revoked"
            ),
        )
        if not self._write_cas(
            connection,
            current=status,
            updated=cancelled,
        ):
            raise self._progress_conflict(connection, status)
        released = connection.execute(
            delete(_slots).where(
                and_(
                    _slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID,
                    _slots.c.run_id == status.run_id,
                    _slots.c.attempt_id == status.attempt_id,
                    _slots.c.epoch == status.epoch,
                )
            )
        )
        if released.rowcount != 1:
            raise RecoveryInProgress()

    def admit_preparation(
        self,
        command: RecoveryPreparationCommand,
    ) -> tuple[RecoveryRunStatus, bool]:
        self._require_r5_schema()
        if not isinstance(command, RecoveryPreparationCommand):
            raise TypeError("command must be RecoveryPreparationCommand")
        self._require_bounded_text(
            command.binding.run_id,
            field="run_id",
            max_length=255,
        )
        self._require_bounded_text(
            command.binding.actor_id,
            field="actor_id",
            max_length=255,
        )
        for contention_attempt in range(8):
            observed_at = self._require_dispatch_time(
                self._wall_clock(),
                field="store wall clock",
            )
            try:
                with self._engine.begin() as connection:
                    self._bound_admission_lock_wait(connection)
                    slot = self._slot_row(connection)
                    if slot is not None:
                        incumbent = self._latest(connection, str(slot["run_id"]))
                        if incumbent is None:
                            raise RecoveryInProgress()
                        if self._preflight_replayable(
                            incumbent,
                            observed_at=observed_at,
                        ):
                            self._lock_slot(
                                connection,
                                status=incumbent,
                                at=observed_at,
                            )
                            if (
                                incumbent.phase is RecoveryRunPhase.PREPARED
                                and incumbent.binding.manifest_ref is not None
                                and self._require_prepared_revoker().is_prepared_revoked(
                                    run_id=incumbent.run_id,
                                    epoch=incumbent.epoch,
                                    manifest_ref=incumbent.binding.manifest_ref,
                                )
                            ):
                                self._release_revoked_prepared_incumbent(
                                    connection,
                                    status=incumbent,
                                    observed_at=observed_at,
                                    requester_actor_id=command.binding.actor_id,
                                )
                            else:
                                self._audit_preflight_requester(
                                    connection,
                                    status=incumbent,
                                    requester_actor_id=command.binding.actor_id,
                                    requested_at=observed_at,
                                    replay=True,
                                )
                                return incumbent, False
                        else:
                            self._revoke_expired_prepared_incumbent(
                                connection,
                                status=incumbent,
                                observed_at=observed_at,
                                requester_actor_id=command.binding.actor_id,
                            )

                    existing = self._latest(connection, command.binding.run_id)
                    if existing is not None:
                        if existing.actor_id != command.binding.actor_id:
                            raise RecoveryBindingConflict(run_id=command.binding.run_id)
                        return existing, False

                    board_total = int(
                        connection.scalar(
                            select(func.count())
                            .select_from(_boards)
                            .where(_boards.c.realm_id == self._realm_id)
                        )
                        or 0
                    )
                    authoritative_counts = replace(
                        command.counts,
                        boards_total=board_total,
                    )
                    desired = RecoveryRunStatus.initial_preparation(
                        replace(command, counts=authoritative_counts)
                    )
                    connection.execute(
                        insert(_attempts).values(**_status_values(desired))
                    )
                    self._transition_observer.record_in_transaction(
                        connection,
                        event=RecoveryTransitionEvent.from_status(desired),
                        observed_at=desired.updated_at,
                    )
                    connection.execute(
                        insert(_slots).values(
                            slot_id=GLOBAL_RECOVERY_SLOT_ID,
                            run_id=desired.run_id,
                            attempt_id=desired.attempt_id,
                            epoch=desired.epoch,
                            actor_id=desired.actor_id,
                            version=1,
                            acquired_at=command.admitted_at,
                            updated_at=observed_at,
                        )
                    )
                    self._insert_dispatch(
                        connection,
                        status=desired,
                        stage=RecoveryDispatchStage.PREPARATION,
                        available_at=command.admitted_at,
                    )
                    self._audit_preflight_requester(
                        connection,
                        status=desired,
                        requester_actor_id=command.binding.actor_id,
                        requested_at=observed_at,
                        replay=False,
                    )
                    return desired, True
            except (IntegrityError, RecoveryInProgress):
                if contention_attempt < 7:
                    continue
                raise
        raise RuntimeError("recovery admission contention retry exhausted")

    @staticmethod
    def _dispatch_is_authorized(
        *,
        status: RecoveryRunStatus,
        stage: RecoveryDispatchStage,
    ) -> bool:
        if stage is RecoveryDispatchStage.PREPARATION:
            return (
                status.confirmation_state is RecoveryConfirmationState.UNCONFIRMED
                and (
                    (
                        status.state is RecoveryRunState.PENDING
                        and status.phase is RecoveryRunPhase.QUEUED
                    )
                    or (
                        status.state is RecoveryRunState.RUNNING
                        and status.phase is RecoveryRunPhase.PREPARING
                    )
                )
            )
        return status.confirmation_state is RecoveryConfirmationState.CONSUMED and (
            (
                status.state is RecoveryRunState.PENDING
                and status.phase is RecoveryRunPhase.CONFIRMED
            )
            or (
                status.state is RecoveryRunState.RUNNING
                and status.phase is RecoveryRunPhase.CUTOVER
            )
        )

    def _terminalize_expired_recovery_dispatch(
        self,
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        dispatch: Mapping[str, Any],
        observed_at: datetime,
        deadline_exhausted: bool,
    ) -> RecoveryRunStatus:
        """Settle abandoned physical work without ever reclaiming its epoch."""

        running = status
        if (
            running.state is RecoveryRunState.PENDING
            and running.phase is RecoveryRunPhase.CONFIRMED
        ):
            running = running.mark_running(
                at=max(running.updated_at, min(observed_at, running.active_deadline_at))
            )
        if (
            running.state is not RecoveryRunState.RUNNING
            or running.phase is not RecoveryRunPhase.CUTOVER
        ):
            raise RecoveryDispatchClaimConflict(
                run_id=running.run_id,
                epoch=running.epoch,
            )
        charge_through = min(observed_at, running.active_deadline_at)
        crash_charge_ms = max(
            0,
            int((charge_through - running.heartbeat_at).total_seconds() * 1_000),
        )
        requested_elapsed_ms = (
            running.attempt_budget_ms
            if deadline_exhausted
            else running.active_elapsed_ms + crash_charge_ms
        )
        cancelled = running.cancel_requested_at is not None
        result = RecoveryWorkerResult(
            outcome=(
                RecoveryTerminalOutcome.CANCELLED
                if cancelled
                else RecoveryTerminalOutcome.TIMEOUT
            ),
            reason_code=(
                "recovery_cancelled"
                if cancelled
                else (
                    "recovery_attempt_budget_exhausted"
                    if deadline_exhausted
                    else "recovery_worker_claim_expired"
                )
            ),
            retryable=False,
            counts=running.counts,
        )
        return self._complete_recovery_in_transaction(
            connection,
            current=running,
            cas_current=status,
            dispatch=dispatch,
            claim_token=(
                None
                if dispatch["claim_token"] is None
                else str(dispatch["claim_token"])
            ),
            completed_at=max(running.updated_at, charge_through),
            active_elapsed_ms=requested_elapsed_ms,
            result=result,
            require_claimed=False,
        )

    def claim_next_dispatch(
        self,
        *,
        stage: RecoveryDispatchStage | str,
        worker_id: str,
        claimed_at: datetime,
        claim_expires_at: datetime,
    ) -> RecoveryDispatchClaim | None:
        self._require_r5_schema()
        normalized_stage = self._normalize_stage(stage)
        worker = self._require_bounded_text(
            worker_id,
            field="worker_id",
            max_length=255,
        )
        claimed = self._require_dispatch_time(claimed_at, field="claimed_at")
        expires = self._require_dispatch_time(
            claim_expires_at,
            field="claim_expires_at",
        )
        if expires <= claimed:
            raise ValueError("claim_expires_at must follow claimed_at")

        with self._engine.begin() as connection:
            candidates = (
                connection.execute(
                    select(_dispatches)
                    .where(
                        and_(
                            _dispatches.c.stage == normalized_stage.value,
                            _dispatches.c.available_at <= claimed,
                            or_(
                                _dispatches.c.state
                                == RecoveryDispatchState.READY.value,
                                and_(
                                    _dispatches.c.state
                                    == RecoveryDispatchState.CLAIMED.value,
                                    _dispatches.c.claim_expires_at <= claimed,
                                ),
                            ),
                        )
                    )
                    .order_by(
                        _dispatches.c.available_at.asc(),
                        _dispatches.c.dispatch_id.asc(),
                    )
                )
                .mappings()
                .all()
            )
            for candidate in candidates:
                reconciliation_only = False
                status = self._latest(connection, str(candidate["run_id"]))
                if status is None or (
                    status.attempt_id != str(candidate["attempt_id"])
                    or status.epoch != int(candidate["epoch"])
                    or not self._dispatch_is_authorized(
                        status=status,
                        stage=normalized_stage,
                    )
                ):
                    continue
                if status.active_deadline_at <= claimed:
                    # Admission owns the budget origin. A delayed worker may
                    # never reset or extend that authoritative deadline. It
                    # also must not leave an unclaimable singleton orphan.
                    if normalized_stage is RecoveryDispatchStage.PREPARATION:
                        outcome = (
                            RecoveryTerminalOutcome.CANCELLED
                            if status.cancel_requested_at is not None
                            else RecoveryTerminalOutcome.TIMEOUT
                        )
                        reason_code = (
                            _PREPARATION_CANCELLED_REASON
                            if outcome is RecoveryTerminalOutcome.CANCELLED
                            else _PREPARATION_BUDGET_EXHAUSTED_REASON
                        )
                        self._terminalize_preparation_in_transaction(
                            connection,
                            current=status,
                            dispatch=candidate,
                            completed_at=max(
                                status.updated_at,
                                status.active_deadline_at,
                            ),
                            active_elapsed_ms=status.attempt_budget_ms,
                            counts=status.counts,
                            outcome=outcome,
                            reason_code=reason_code,
                        )
                    else:
                        # Do not synthesize SQL timeout before reading the
                        # attempt journal.  A crash may have happened after the
                        # pointer/terminal journal write.  Claim the same epoch
                        # for reconciliation only; that worker is forbidden to
                        # start a fresh candidate.
                        reconciliation_only = True
                    if not reconciliation_only:
                        continue
                if (
                    normalized_stage is RecoveryDispatchStage.PREPARATION
                    and int(candidate["attempt_count"])
                    >= _DEFAULT_PREPARATION_MAX_ATTEMPTS
                ):
                    exhausted_outcome = (
                        RecoveryTerminalOutcome.CANCELLED
                        if status.cancel_requested_at is not None
                        else RecoveryTerminalOutcome.FAILED
                    )
                    self._terminalize_preparation_in_transaction(
                        connection,
                        current=status,
                        dispatch=candidate,
                        completed_at=max(status.updated_at, claimed),
                        active_elapsed_ms=status.active_elapsed_ms,
                        counts=status.counts,
                        outcome=exhausted_outcome,
                        reason_code=(
                            _PREPARATION_CANCELLED_REASON
                            if exhausted_outcome is RecoveryTerminalOutcome.CANCELLED
                            else _PREPARATION_RETRY_EXHAUSTED_REASON
                        ),
                    )
                    continue
                reclaim_settled = None
                if (
                    normalized_stage is RecoveryDispatchStage.RECOVERY
                    and str(candidate["state"])
                    == RecoveryDispatchState.CLAIMED.value
                    and status.state is RecoveryRunState.RUNNING
                ):
                    # A5R2: the crashed owner's lease window is charged
                    # exactly once, atomically with the winning claim, and
                    # liveness rebases on the new claimed_at.  Prospective
                    # compute only — the status CAS below runs solely for the
                    # dispatch-CAS winner inside this same transaction.
                    reclaim_settled = status.settle_expired_reclaim(
                        old_claim_expires_at=_aware_datetime(
                            candidate["claim_expires_at"]
                        ),
                        claimed_at=claimed,
                    )
                    if (
                        not reconciliation_only
                        and reclaim_settled.active_elapsed_ms
                        >= status.effective_active_cap_ms
                    ):
                        # The charged window exhausts the effective budget
                        # (attempt or cumulative allowance): no fresh physical
                        # work may start even before the wall deadline; the
                        # journal is read/reconciled first.
                        reconciliation_only = True
                effective_expires = (
                    expires
                    if reconciliation_only
                    else min(expires, status.active_deadline_at)
                )
                self._require_slot(connection, status=status)
                token = f"gdrclaim_{uuid.uuid4().hex}"
                ownership = [
                    _dispatches.c.dispatch_id == str(candidate["dispatch_id"]),
                    _dispatches.c.state == str(candidate["state"]),
                ]
                if str(candidate["state"]) == RecoveryDispatchState.READY.value:
                    ownership.append(_dispatches.c.claim_token.is_(None))
                else:
                    ownership.extend(
                        [
                            _dispatches.c.claim_token == str(candidate["claim_token"]),
                            _dispatches.c.claim_expires_at
                            == candidate["claim_expires_at"],
                        ]
                    )
                if reclaim_settled is not None:
                    # Canonical lock order for an already-RUNNING attempt:
                    # slot, then STATUS, then dispatch — heartbeat and
                    # complete already write status before dispatch, so the
                    # settle CAS here is the single race decider and no
                    # lock-order inversion exists.  A conflict raises and
                    # nothing else is written.
                    if not self._write_cas(
                        connection,
                        current=status,
                        updated=reclaim_settled,
                    ):
                        raise self._progress_conflict(connection, status)
                claimed_result = connection.execute(
                    update(_dispatches)
                    .where(and_(*ownership))
                    .values(
                        state=RecoveryDispatchState.CLAIMED.value,
                        claim_token=token,
                        worker_id=worker,
                        claimed_at=claimed,
                        claim_expires_at=effective_expires,
                        attempt_count=_dispatches.c.attempt_count + 1,
                    )
                )
                if claimed_result.rowcount != 1:
                    if reclaim_settled is not None:
                        # Defense-in-depth loser AFTER the status settle must
                        # abort the WHOLE transaction (charge, transition and
                        # claim roll back together); continuing would commit
                        # a charge without an owning claim.
                        raise RecoveryDispatchClaimConflict(
                            run_id=status.run_id,
                            epoch=status.epoch,
                        )
                    continue
                if reclaim_settled is None and (
                    normalized_stage is RecoveryDispatchStage.RECOVERY
                    and status.state is RecoveryRunState.PENDING
                ):
                    running = status.mark_running(at=claimed)
                    if not self._write_cas(
                        connection,
                        current=status,
                        updated=running,
                    ):
                        raise self._progress_conflict(connection, status)
                updated_row = self._dispatch_row(
                    connection,
                    dispatch_id=str(candidate["dispatch_id"]),
                )
                assert updated_row is not None
                return _dispatch_claim(
                    updated_row,
                    reconciliation_only=reconciliation_only,
                )
        return None

    @staticmethod
    def _require_live_claim(
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        stage: RecoveryDispatchStage,
        claim_token: str,
        observed_at: datetime,
    ) -> Mapping[str, Any]:
        row = SQLAlchemyRecoveryRunStore._attempt_dispatch_row(
            connection,
            run_id=status.run_id,
            attempt_id=status.attempt_id,
            epoch=status.epoch,
            stage=stage,
        )
        if (
            row is None
            or str(row["state"]) != RecoveryDispatchState.CLAIMED.value
            or str(row["claim_token"]) != str(claim_token)
            or row["claim_expires_at"] is None
            or _aware_datetime(row["claim_expires_at"]) <= observed_at
        ):
            raise RecoveryDispatchClaimConflict(
                run_id=status.run_id,
                epoch=status.epoch,
            )
        return row

    @staticmethod
    def _require_owned_preparation_claim(
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        dispatch_id: str,
        claim_token: str,
    ) -> Mapping[str, Any]:
        row = SQLAlchemyRecoveryRunStore._dispatch_row(
            connection,
            dispatch_id=dispatch_id,
        )
        if (
            row is None
            or str(row["stage"]) != RecoveryDispatchStage.PREPARATION.value
            or str(row["state"]) != RecoveryDispatchState.CLAIMED.value
            or str(row["claim_token"]) != str(claim_token)
            or str(row["run_id"]) != status.run_id
            or str(row["attempt_id"]) != status.attempt_id
            or int(row["epoch"]) != status.epoch
        ):
            raise RecoveryDispatchClaimConflict(
                run_id=status.run_id,
                epoch=status.epoch,
            )
        return row

    @staticmethod
    def _dispatch_ownership_predicates(
        dispatch: Mapping[str, Any],
    ) -> list[Any]:
        predicates: list[Any] = [
            _dispatches.c.dispatch_id == str(dispatch["dispatch_id"]),
            _dispatches.c.state == str(dispatch["state"]),
        ]
        if dispatch["claim_token"] is None:
            predicates.append(_dispatches.c.claim_token.is_(None))
        else:
            predicates.append(_dispatches.c.claim_token == str(dispatch["claim_token"]))
            predicates.append(
                _dispatches.c.claim_expires_at == dispatch["claim_expires_at"]
            )
        return predicates

    @staticmethod
    def _dispatch_published_manifest_ref(
        dispatch: Mapping[str, Any],
    ) -> str | None:
        payload = dispatch.get("result_payload")
        if payload is None or not isinstance(payload, Mapping):
            return None
        value = payload.get("published_manifest_ref")
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized or len(normalized) > 1024:
            raise RecoveryStoreSchemaError(
                code="global_discovery_recovery_dispatch_manifest_corrupt"
            )
        return normalized

    def _resolve_dispatch_manifest_ref(
        self,
        dispatch: Mapping[str, Any],
        manifest_ref: str | None,
    ) -> str | None:
        persisted = self._dispatch_published_manifest_ref(dispatch)
        explicit = (
            None
            if manifest_ref is None
            else self._require_bounded_text(
                manifest_ref,
                field="manifest_ref",
                max_length=1024,
            )
        )
        if persisted is not None and explicit is not None and persisted != explicit:
            raise RecoveryStoreContractError(
                code="global_discovery_recovery_dispatch_manifest_conflict"
            )
        resolved = explicit or persisted
        if resolved is not None:
            return resolved
        # A process can stop after Core atomically binds the manifest to the
        # attempt but before the first SQL heartbeat persists manifest_ref.
        # Off-request settlement recovers that create-only binding so timeout,
        # cancel, and failure cannot orphan a prepared candidate.
        return self._resolve_attempt_manifest_ref(
            run_id=str(dispatch["run_id"]),
            epoch=int(dispatch["epoch"]),
        )

    def _require_settleable_preparation_claim(
        self,
        connection: Connection,
        *,
        status: RecoveryRunStatus,
        claim_token: str,
        observed_at: datetime,
        owned: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Require a live lease, with one exact-token deadline exception."""

        if observed_at < status.active_deadline_at:
            return self._require_live_claim(
                connection,
                status=status,
                stage=RecoveryDispatchStage.PREPARATION,
                claim_token=claim_token,
                observed_at=observed_at,
            )
        expiry = owned.get("claim_expires_at")
        if expiry is None or _aware_datetime(expiry) != status.active_deadline_at:
            raise RecoveryDispatchClaimConflict(
                run_id=status.run_id,
                epoch=status.epoch,
            )
        # The lease is capped at the attempt deadline, so equality would make
        # normal liveness impossible exactly when the heartbeat must settle.
        # Exact dispatch/token ownership still serializes against reclamation.
        return owned

    @staticmethod
    def _advance_preparation_progress(
        status: RecoveryRunStatus,
        *,
        observed_at: datetime,
        active_elapsed_ms: int,
        counts: RecoveryProgressCounts,
    ) -> RecoveryRunStatus:
        running = status
        if (
            running.state is RecoveryRunState.PENDING
            and running.phase is RecoveryRunPhase.QUEUED
        ):
            running = running.mark_running(at=max(running.updated_at, observed_at))
        if (
            running.state is not RecoveryRunState.RUNNING
            or running.phase is not RecoveryRunPhase.PREPARING
        ):
            raise RecoveryDispatchClaimConflict(
                run_id=running.run_id,
                epoch=running.epoch,
            )
        elapsed = min(
            running.attempt_budget_ms,
            max(running.active_elapsed_ms, int(active_elapsed_ms)),
        )
        heartbeat_at = max(running.heartbeat_at, observed_at)
        if (
            counts == running.counts
            and elapsed == running.active_elapsed_ms
            and heartbeat_at == running.heartbeat_at
        ):
            return running
        return running.advance_checkpoint(
            RecoveryCheckpoint(
                run_id=running.run_id,
                epoch=running.epoch,
                expected_progress_seq=running.progress_seq,
                phase=RecoveryRunPhase.PREPARING,
                heartbeat_at=heartbeat_at,
                active_elapsed_ms=elapsed,
                counts=counts,
            )
        )

    def _terminalize_preparation_in_transaction(
        self,
        connection: Connection,
        *,
        current: RecoveryRunStatus,
        cas_current: RecoveryRunStatus | None = None,
        dispatch: Mapping[str, Any],
        completed_at: datetime,
        active_elapsed_ms: int,
        counts: RecoveryProgressCounts,
        outcome: RecoveryTerminalOutcome,
        reason_code: str,
        manifest_ref: str | None = None,
    ) -> RecoveryRunStatus:
        cas_base = current if cas_current is None else cas_current
        if (
            cas_base.run_id != current.run_id
            or cas_base.attempt_id != current.attempt_id
            or cas_base.epoch != current.epoch
        ):
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )
        observed = max(current.updated_at, completed_at)
        progressed = self._advance_preparation_progress(
            current,
            observed_at=observed,
            active_elapsed_ms=active_elapsed_ms,
            counts=counts,
        )
        self._lock_slot(connection, status=current, at=observed)
        # Win dispatch ownership against heartbeat renewal before any external
        # revocation side effect. The no-op UPDATE locks/revalidates the exact
        # state, token, and observed expiry in one database operation.
        ownership_lock = connection.execute(
            update(_dispatches)
            .where(and_(*self._dispatch_ownership_predicates(dispatch)))
            .values(state=str(dispatch["state"]))
        )
        if ownership_lock.rowcount != 1:
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )
        fresh_dispatch = self._dispatch_row(
            connection,
            dispatch_id=str(dispatch["dispatch_id"]),
        )
        if fresh_dispatch is None:
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )
        effective_manifest_ref = self._resolve_dispatch_manifest_ref(
            fresh_dispatch,
            manifest_ref,
        )
        if effective_manifest_ref is not None:
            self._require_prepared_revoker().revoke_prepared(
                run_id=current.run_id,
                epoch=current.epoch,
                manifest_ref=effective_manifest_ref,
                revoked_at=observed,
                requested_by_actor_id=current.actor_id,
                reason=reason_code,
            )
        terminal = progressed.complete(
            result=RecoveryWorkerResult(
                outcome=outcome,
                reason_code=reason_code,
                retryable=False,
                counts=progressed.counts,
            ),
            completed_at=observed,
            active_elapsed_ms=progressed.active_elapsed_ms,
        )
        acknowledged = connection.execute(
            update(_dispatches)
            .where(and_(*self._dispatch_ownership_predicates(fresh_dispatch)))
            .values(
                state=RecoveryDispatchState.DONE.value,
                completed_at=observed,
                result_payload={
                    "outcome": outcome.value,
                    "reason_code": reason_code,
                    "retryable": False,
                    "manifest_revoked": effective_manifest_ref is not None,
                    "counts": terminal.counts.to_dict(),
                },
            )
        )
        if acknowledged.rowcount != 1:
            raise RecoveryDispatchClaimConflict(
                run_id=current.run_id,
                epoch=current.epoch,
            )
        if not self._write_cas(
            connection,
            current=cas_base,
            updated=terminal,
        ):
            raise self._progress_conflict(connection, cas_base)
        released = connection.execute(
            delete(_slots).where(
                and_(
                    _slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID,
                    _slots.c.run_id == current.run_id,
                    _slots.c.attempt_id == current.attempt_id,
                    _slots.c.epoch == current.epoch,
                )
            )
        )
        if released.rowcount != 1:
            raise RecoveryInProgress()
        return terminal

    def heartbeat_preparation(
        self,
        *,
        dispatch_id: str,
        claim_token: str,
        observed_at: datetime,
        requested_expires_at: datetime,
        active_elapsed_ms: int,
        counts: RecoveryProgressCounts,
        manifest_ref: str | None = None,
    ) -> RecoveryRunStatus:
        """Atomically renew a preparation claim and CAS its checkpoint."""

        observed = self._require_dispatch_time(observed_at, field="observed_at")
        requested = self._require_dispatch_time(
            requested_expires_at,
            field="requested_expires_at",
        )
        if requested <= observed:
            raise ValueError("requested_expires_at must follow observed_at")
        if not isinstance(counts, RecoveryProgressCounts):
            raise TypeError("counts must be RecoveryProgressCounts")
        with self._engine.begin() as connection:
            dispatch = self._dispatch_row(connection, dispatch_id=dispatch_id)
            if dispatch is None:
                raise RecoveryDispatchClaimConflict(run_id="unknown", epoch=0)
            current = self._require_latest(connection, str(dispatch["run_id"]))
            owned = self._require_owned_preparation_claim(
                connection,
                status=current,
                dispatch_id=dispatch_id,
                claim_token=claim_token,
            )
            live_observed = max(current.updated_at, observed)
            owned = self._require_settleable_preparation_claim(
                connection,
                status=current,
                claim_token=claim_token,
                observed_at=live_observed,
                owned=owned,
            )
            effective_manifest_ref = self._resolve_dispatch_manifest_ref(
                owned,
                manifest_ref,
            )
            if current.cancel_requested_at is not None:
                return self._terminalize_preparation_in_transaction(
                    connection,
                    current=current,
                    dispatch=owned,
                    completed_at=observed,
                    active_elapsed_ms=active_elapsed_ms,
                    counts=counts,
                    outcome=RecoveryTerminalOutcome.CANCELLED,
                    reason_code=_PREPARATION_CANCELLED_REASON,
                    manifest_ref=effective_manifest_ref,
                )
            if (
                live_observed >= current.active_deadline_at
                or int(active_elapsed_ms) >= current.attempt_budget_ms
            ):
                return self._terminalize_preparation_in_transaction(
                    connection,
                    current=current,
                    dispatch=owned,
                    completed_at=max(
                        current.updated_at,
                        current.active_deadline_at,
                    ),
                    active_elapsed_ms=current.attempt_budget_ms,
                    counts=counts,
                    outcome=RecoveryTerminalOutcome.TIMEOUT,
                    reason_code=_PREPARATION_BUDGET_EXHAUSTED_REASON,
                    manifest_ref=effective_manifest_ref,
                )
            current_expiry = (
                None
                if owned["claim_expires_at"] is None
                else _aware_datetime(owned["claim_expires_at"])
            )
            if current_expiry is None or current_expiry <= observed:
                raise RecoveryDispatchClaimConflict(
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            updated = self._advance_preparation_progress(
                current,
                observed_at=observed,
                active_elapsed_ms=active_elapsed_ms,
                counts=counts,
            )
            effective_expiry = min(
                current.active_deadline_at,
                max(current_expiry, requested),
            )
            renewal_values: dict[str, object] = {
                "claim_expires_at": effective_expiry,
            }
            if manifest_ref is not None:
                payload = (
                    dict(owned["result_payload"])
                    if isinstance(owned["result_payload"], Mapping)
                    else {}
                )
                payload.update(
                    {
                        "outcome": "manifest_published",
                        "published_manifest_ref": effective_manifest_ref,
                    }
                )
                renewal_values["result_payload"] = payload
            renewed = connection.execute(
                update(_dispatches)
                .where(
                    and_(
                        _dispatches.c.dispatch_id == str(dispatch_id),
                        _dispatches.c.state == RecoveryDispatchState.CLAIMED.value,
                        _dispatches.c.claim_token == str(claim_token),
                        _dispatches.c.claim_expires_at == owned["claim_expires_at"],
                    )
                )
                .values(**renewal_values)
            )
            if renewed.rowcount != 1:
                raise RecoveryDispatchClaimConflict(
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            if updated is not current and not self._write_cas(
                connection,
                current=current,
                updated=updated,
            ):
                raise self._progress_conflict(connection, current)
            return updated

    def record_preparation_failure(
        self,
        *,
        dispatch_id: str,
        claim_token: str,
        failed_at: datetime,
        active_elapsed_ms: int,
        counts: RecoveryProgressCounts,
        reason_code: str,
        retryable: bool,
        retry_available_at: datetime,
        max_attempts: int,
        manifest_ref: str | None = None,
    ) -> RecoveryRunStatus:
        """Persist one sanitized failure, requeueing only explicit transients."""

        failed = self._require_dispatch_time(failed_at, field="failed_at")
        available = self._require_dispatch_time(
            retry_available_at,
            field="retry_available_at",
        )
        if not isinstance(counts, RecoveryProgressCounts):
            raise TypeError("counts must be RecoveryProgressCounts")
        if int(max_attempts) < 1 or int(max_attempts) > 3:
            raise ValueError("max_attempts must be within 1..3")
        normalized_reason = self._require_non_empty_text(
            reason_code,
            field="reason_code",
        )
        if len(normalized_reason) > 128:
            raise ValueError("reason_code must be within 1..128 chars")
        with self._engine.begin() as connection:
            dispatch = self._dispatch_row(connection, dispatch_id=dispatch_id)
            if dispatch is None:
                raise RecoveryDispatchClaimConflict(run_id="unknown", epoch=0)
            current = self._require_latest(connection, str(dispatch["run_id"]))
            observed = max(current.updated_at, failed)
            owned = self._require_owned_preparation_claim(
                connection,
                status=current,
                dispatch_id=dispatch_id,
                claim_token=claim_token,
            )
            owned = self._require_settleable_preparation_claim(
                connection,
                status=current,
                claim_token=claim_token,
                observed_at=observed,
                owned=owned,
            )
            effective_manifest_ref = self._resolve_dispatch_manifest_ref(
                owned,
                manifest_ref,
            )
            if current.cancel_requested_at is not None:
                return self._terminalize_preparation_in_transaction(
                    connection,
                    current=current,
                    dispatch=owned,
                    completed_at=observed,
                    active_elapsed_ms=active_elapsed_ms,
                    counts=counts,
                    outcome=RecoveryTerminalOutcome.CANCELLED,
                    reason_code=_PREPARATION_CANCELLED_REASON,
                    manifest_ref=effective_manifest_ref,
                )
            if observed >= current.active_deadline_at:
                return self._terminalize_preparation_in_transaction(
                    connection,
                    current=current,
                    dispatch=owned,
                    completed_at=max(
                        current.updated_at,
                        current.active_deadline_at,
                    ),
                    active_elapsed_ms=current.attempt_budget_ms,
                    counts=counts,
                    outcome=RecoveryTerminalOutcome.TIMEOUT,
                    reason_code=_PREPARATION_BUDGET_EXHAUSTED_REASON,
                    manifest_ref=effective_manifest_ref,
                )
            may_retry = bool(
                retryable
                and effective_manifest_ref is None
                and int(owned["attempt_count"]) < int(max_attempts)
                and available < current.active_deadline_at
            )
            if not may_retry:
                terminal_reason = (
                    _PREPARATION_RETRY_EXHAUSTED_REASON
                    if retryable
                    else normalized_reason
                )
                return self._terminalize_preparation_in_transaction(
                    connection,
                    current=current,
                    dispatch=owned,
                    completed_at=observed,
                    active_elapsed_ms=active_elapsed_ms,
                    counts=counts,
                    outcome=RecoveryTerminalOutcome.FAILED,
                    reason_code=terminal_reason,
                    manifest_ref=effective_manifest_ref,
                )
            progressed = self._advance_preparation_progress(
                current,
                observed_at=observed,
                active_elapsed_ms=active_elapsed_ms,
                counts=counts,
            )
            requeued = connection.execute(
                update(_dispatches)
                .where(and_(*self._dispatch_ownership_predicates(owned)))
                .values(
                    state=RecoveryDispatchState.READY.value,
                    claim_token=None,
                    worker_id=None,
                    claimed_at=None,
                    claim_expires_at=None,
                    available_at=available,
                    completed_at=None,
                    result_payload={
                        "outcome": "retry_scheduled",
                        "reason_code": normalized_reason,
                        "retryable": True,
                        "counts": progressed.counts.to_dict(),
                    },
                )
            )
            if requeued.rowcount != 1:
                raise RecoveryDispatchClaimConflict(
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            if progressed is not current and not self._write_cas(
                connection,
                current=current,
                updated=progressed,
            ):
                raise self._progress_conflict(connection, current)
            return progressed

    def mark_preparing(
        self,
        *,
        run_id: str,
        attempt_id: str,
        epoch: int,
        claim_token: str,
        at: datetime,
    ) -> RecoveryRunStatus:
        observed = self._require_dispatch_time(at, field="at")
        with self._engine.begin() as connection:
            current = self._require_latest(connection, run_id)
            self._require_epoch(current, expected_epoch=epoch)
            if current.attempt_id != str(attempt_id):
                raise RecoveryDispatchClaimConflict(
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            self._require_live_claim(
                connection,
                status=current,
                stage=RecoveryDispatchStage.PREPARATION,
                claim_token=claim_token,
                observed_at=observed,
            )
            if (
                current.state is RecoveryRunState.RUNNING
                and current.phase is RecoveryRunPhase.PREPARING
            ):
                return current
            updated = current.mark_running(at=observed)
            if not self._write_cas(
                connection,
                current=current,
                updated=updated,
            ):
                raise self._progress_conflict(connection, current)
            return updated

    def complete_preparation(
        self,
        *,
        run_id: str,
        attempt_id: str,
        epoch: int,
        claim_token: str,
        completed_at: datetime,
        result: RecoveryPreparedResult,
    ) -> RecoveryRunStatus:
        observed = self._require_dispatch_time(
            completed_at,
            field="completed_at",
        )
        if not isinstance(result, RecoveryPreparedResult):
            raise TypeError("result must be RecoveryPreparedResult")
        self._require_bounded_text(
            result.manifest_ref,
            field="manifest_ref",
            max_length=1024,
        )
        self._require_bounded_text(
            result.preflight_hash,
            field="preflight_hash",
            max_length=255,
        )
        self._require_bounded_text(
            result.snapshot_fingerprint,
            field="snapshot_fingerprint",
            max_length=255,
        )
        with self._engine.begin() as connection:
            current = self._require_latest(connection, run_id)
            self._require_epoch(current, expected_epoch=epoch)
            if current.attempt_id != str(attempt_id):
                raise RecoveryDispatchClaimConflict(
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            dispatch = self._attempt_dispatch_row(
                connection,
                run_id=current.run_id,
                attempt_id=current.attempt_id,
                epoch=current.epoch,
                stage=RecoveryDispatchStage.PREPARATION,
            )
            if dispatch is None:
                raise RecoveryDispatchClaimConflict(
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            dispatch = self._require_owned_preparation_claim(
                connection,
                status=current,
                dispatch_id=str(dispatch["dispatch_id"]),
                claim_token=claim_token,
            )
            live_observed = max(current.updated_at, observed)
            dispatch = self._require_settleable_preparation_claim(
                connection,
                status=current,
                claim_token=claim_token,
                observed_at=live_observed,
                owned=dispatch,
            )
            effective_manifest_ref = self._resolve_dispatch_manifest_ref(
                dispatch,
                result.manifest_ref,
            )
            elapsed_ms = min(
                current.attempt_budget_ms,
                max(
                    current.active_elapsed_ms,
                    int(
                        max(0.0, (observed - current.started_at).total_seconds())
                        * 1_000
                    ),
                ),
            )
            if current.cancel_requested_at is not None:
                return self._terminalize_preparation_in_transaction(
                    connection,
                    current=current,
                    dispatch=dispatch,
                    completed_at=observed,
                    active_elapsed_ms=elapsed_ms,
                    counts=result.counts,
                    outcome=RecoveryTerminalOutcome.CANCELLED,
                    reason_code=_PREPARATION_CANCELLED_REASON,
                    manifest_ref=effective_manifest_ref,
                )
            if live_observed >= current.active_deadline_at:
                return self._terminalize_preparation_in_transaction(
                    connection,
                    current=current,
                    dispatch=dispatch,
                    completed_at=current.active_deadline_at,
                    active_elapsed_ms=current.attempt_budget_ms,
                    counts=result.counts,
                    outcome=RecoveryTerminalOutcome.TIMEOUT,
                    reason_code=_PREPARATION_BUDGET_EXHAUSTED_REASON,
                    manifest_ref=effective_manifest_ref,
                )
            prepared = current.mark_prepared(
                prepared=result,
                expected_progress_seq=current.progress_seq,
            )
            acknowledged = connection.execute(
                update(_dispatches)
                .where(
                    and_(
                        _dispatches.c.dispatch_id == str(dispatch["dispatch_id"]),
                        _dispatches.c.state == RecoveryDispatchState.CLAIMED.value,
                        _dispatches.c.claim_token == str(claim_token),
                    )
                )
                .values(
                    state=RecoveryDispatchState.DONE.value,
                    completed_at=max(observed, result.prepared_at),
                    result_payload={
                        "manifest_ref": result.manifest_ref,
                        "preflight_hash": result.preflight_hash,
                        "snapshot_fingerprint": result.snapshot_fingerprint,
                        "prepared_at": result.prepared_at.isoformat(),
                        "expires_at": result.expires_at.isoformat(),
                        "counts": result.counts.to_dict(),
                    },
                )
            )
            if acknowledged.rowcount != 1:
                raise RecoveryDispatchClaimConflict(
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            if not self._write_cas(
                connection,
                current=current,
                updated=prepared,
            ):
                raise self._progress_conflict(connection, current)
            connection.execute(
                update(_slots)
                .where(
                    and_(
                        _slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID,
                        _slots.c.run_id == current.run_id,
                        _slots.c.attempt_id == current.attempt_id,
                        _slots.c.epoch == current.epoch,
                    )
                )
                .values(updated_at=observed)
            )
            return prepared

    def mark_prepared(
        self,
        *,
        run_id: str,
        epoch: int,
        expected_progress_seq: int,
        prepared: RecoveryPreparedResult,
    ) -> RecoveryRunStatus:
        """Core-port bridge; exact ownership remains the claimed SQL row."""

        with self._engine.connect() as connection:
            current = self._require_latest(connection, run_id)
            self._require_epoch(current, expected_epoch=epoch)
            if current.progress_seq != int(expected_progress_seq):
                raise self._progress_conflict(connection, current)
            dispatch = self._attempt_dispatch_row(
                connection,
                run_id=current.run_id,
                attempt_id=current.attempt_id,
                epoch=current.epoch,
                stage=RecoveryDispatchStage.PREPARATION,
            )
        if dispatch is None or dispatch["claim_token"] is None:
            raise RecoveryDispatchClaimConflict(run_id=current.run_id, epoch=epoch)
        return self.complete_preparation(
            run_id=current.run_id,
            attempt_id=current.attempt_id,
            epoch=current.epoch,
            claim_token=str(dispatch["claim_token"]),
            completed_at=prepared.prepared_at,
            result=prepared,
        )

    def _enqueue_execution(
        self,
        command: RecoveryStartCommand,
    ) -> tuple[RecoveryRunStatus, bool]:
        self._require_r5_schema()
        if not isinstance(command, RecoveryStartCommand):
            raise TypeError("command must be RecoveryStartCommand")
        self._require_bounded_text(
            command.binding.run_id,
            field="run_id",
            max_length=255,
        )
        self._require_bounded_text(
            command.binding.actor_id,
            field="actor_id",
            max_length=255,
        )
        self._require_bounded_text(
            command.confirmed_by_actor_id or command.binding.actor_id,
            field="confirmed_by_actor_id",
            max_length=255,
        )
        revoker = self._require_prepared_revoker()
        observed_at = self._require_dispatch_time(
            self._wall_clock(),
            field="store wall clock",
        )
        with self._engine.begin() as connection:
            current = self._require_latest(connection, command.binding.run_id)
            self._require_epoch(current, expected_epoch=command.expected_epoch)
            if current.confirmation_state is RecoveryConfirmationState.CONSUMED:
                if current.binding == command.binding:
                    self._insert_dispatch(
                        connection,
                        status=current,
                        stage=RecoveryDispatchStage.RECOVERY,
                        available_at=current.confirmation_consumed_at
                        or current.updated_at,
                    )
                    return current, False
                raise RecoveryBindingConflict(run_id=current.run_id)
            if (
                current.state is not RecoveryRunState.PENDING
                or current.phase is not RecoveryRunPhase.PREPARED
                or current.binding.manifest_ref is None
            ):
                raise ValueError("only a prepared recovery can consume confirmation")
            if current.cancel_requested_at is not None:
                raise RecoveryResumeRejected(
                    code="recovery_cancel_pending",
                    run_id=current.run_id,
                    epoch=current.epoch,
                )
            self._lock_slot(connection, status=current, at=observed_at)
            if revoker.is_prepared_revoked(
                run_id=current.run_id,
                epoch=current.epoch,
                manifest_ref=current.binding.manifest_ref,
            ):
                raise GlobalDiscoveryRecoveryError(
                    "prepared_revoked",
                    "prepared recovery has durable revocation evidence",
                )
            if current.expires_at is None or observed_at >= current.expires_at:
                raise GlobalDiscoveryRecoveryError(
                    "manifest_stale",
                    "prepared manifest expired before confirmation consumption",
                )
            confirmed = current.mark_confirmed(command)
            if not self._write_cas(
                connection,
                current=current,
                updated=confirmed,
            ):
                raise self._progress_conflict(connection, current)
            self._insert_dispatch(
                connection,
                status=confirmed,
                stage=RecoveryDispatchStage.RECOVERY,
                available_at=confirmed.confirmation_consumed_at or confirmed.updated_at,
            )
            return confirmed, True

    def enqueue_execution(self, command: RecoveryStartCommand) -> RecoveryRunStatus:
        status, _transitioned = self._enqueue_execution(command)
        return status

    def admit_prepared_start(
        self,
        command: RecoveryStartCommand,
    ) -> tuple[RecoveryRunStatus, bool]:
        return self._enqueue_execution(command)

    def cancel_prepared(
        self,
        *,
        run_id: str,
        expected_epoch: int,
        requested_at: datetime,
        requested_by_actor_id: str,
        reason: str | None,
    ) -> RecoveryRunStatus:
        self._require_r5_schema()
        requested = self._require_dispatch_time(
            requested_at,
            field="requested_at",
        )
        requested_by = self._require_non_empty_text(
            requested_by_actor_id,
            field="requested_by_actor_id",
        )
        if len(requested_by) > 255:
            raise ValueError("requested_by_actor_id must be within 1..255 chars")
        intent: RecoveryRunStatus
        with self._engine.begin() as connection:
            current = self._require_latest(connection, run_id)
            self._require_epoch(current, expected_epoch=expected_epoch)
            if current.state is RecoveryRunState.CANCELLED:
                return current
            if (
                current.state is not RecoveryRunState.PENDING
                or current.phase is not RecoveryRunPhase.PREPARED
                or current.binding.manifest_ref is None
            ):
                raise ValueError(
                    "only a prepared recovery can use prepared cancellation"
                )
            self._require_prepared_revoker()
            intent = current.request_cancel(
                requested_at=requested,
                requested_by_actor_id=requested_by,
                reason=reason,
            )
            if intent is not current:
                self._lock_slot(connection, status=current, at=requested)
                if not self._write_cas(
                    connection,
                    current=current,
                    updated=intent,
                ):
                    raise self._progress_conflict(connection, current)
        thread = self._schedule_prepared_cancellation(intent)
        return self._briefly_observe_prepared_cancellation(
            intent=intent,
            thread=thread,
        )

    def _cancel_prepared_in_transaction(
        self,
        connection: Connection,
        *,
        current: RecoveryRunStatus,
        requested_at: datetime,
        requested_by_actor_id: str,
        reason: str | None,
    ) -> RecoveryRunStatus:
        """Terminalize a PREPARED attempt after external revocation exists."""

        if (
            current.state is not RecoveryRunState.PENDING
            or current.phase is not RecoveryRunPhase.PREPARED
            or current.binding.manifest_ref is None
        ):
            raise ValueError("only a prepared recovery can use prepared cancellation")
        requested_by = self._require_bounded_text(
            requested_by_actor_id,
            field="requested_by_actor_id",
            max_length=255,
        )
        cancelled = current.cancel_prepared(
            requested_at=requested_at,
            requested_by_actor_id=requested_by,
            reason=reason,
        )
        self._lock_slot(connection, status=current, at=requested_at)
        if not self._write_cas(
            connection,
            current=current,
            updated=cancelled,
        ):
            raise self._progress_conflict(connection, current)
        released = connection.execute(
            delete(_slots).where(
                and_(
                    _slots.c.slot_id == GLOBAL_RECOVERY_SLOT_ID,
                    _slots.c.run_id == current.run_id,
                    _slots.c.attempt_id == current.attempt_id,
                    _slots.c.epoch == current.epoch,
                )
            )
        )
        if released.rowcount != 1:
            raise RecoveryInProgress()
        return cancelled

    def complete_dispatch(
        self,
        *,
        dispatch_id: str,
        claim_token: str,
        completed_at: datetime,
        result: Mapping[str, object],
    ) -> RecoveryDispatchReceipt:
        self._require_r5_schema()
        observed = self._require_dispatch_time(
            completed_at,
            field="completed_at",
        )
        token = self._require_non_empty_text(claim_token, field="claim_token")
        if not isinstance(result, Mapping):
            raise TypeError("result must be a mapping")
        with self._engine.begin() as connection:
            current = self._dispatch_row(connection, dispatch_id=dispatch_id)
            if current is None:
                raise RecoveryDispatchClaimConflict(run_id="unknown", epoch=0)
            if str(current["stage"]) != RecoveryDispatchStage.PREPARATION.value:
                # A5R2: RECOVERY completion must go exclusively through
                # complete_recovery, which terminalizes the attempt, marks
                # the dispatch DONE and releases the slot atomically.
                # Acknowledging a recovery row here would strand a RUNNING
                # status behind a DONE dispatch.
                raise RecoveryDispatchClaimConflict(
                    run_id=str(current["run_id"]),
                    epoch=int(current["epoch"]),
                )
            if (
                str(current["state"]) == RecoveryDispatchState.DONE.value
                and str(current["claim_token"]) == token
            ):
                return _dispatch_receipt(current)
            if (
                str(current["state"]) != RecoveryDispatchState.CLAIMED.value
                or str(current["claim_token"]) != token
            ):
                raise RecoveryDispatchClaimConflict(
                    run_id=str(current["run_id"]),
                    epoch=int(current["epoch"]),
                )
            acknowledged = connection.execute(
                update(_dispatches)
                .where(
                    and_(
                        _dispatches.c.dispatch_id == str(dispatch_id),
                        _dispatches.c.state == RecoveryDispatchState.CLAIMED.value,
                        _dispatches.c.claim_token == token,
                    )
                )
                .values(
                    state=RecoveryDispatchState.DONE.value,
                    completed_at=observed,
                    result_payload=dict(result),
                )
            )
            if acknowledged.rowcount != 1:
                raise RecoveryDispatchClaimConflict(
                    run_id=str(current["run_id"]),
                    epoch=int(current["epoch"]),
                )
            updated_row = self._dispatch_row(
                connection,
                dispatch_id=str(dispatch_id),
            )
            assert updated_row is not None
            return _dispatch_receipt(updated_row)

    def assert_dispatch_enqueued(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        kind: RecoveryDispatchKind | str,
    ) -> None:
        """Verify the control transaction already committed exact durable work."""

        self._require_r5_schema()
        stage = RecoveryDispatchStage(getattr(kind, "value", kind))
        with self._engine.connect() as connection:
            status = self._require_latest(connection, run_id)
            if (
                status.epoch != int(epoch)
                or status.attempt_id != str(attempt_id)
                or not self._dispatch_is_authorized(status=status, stage=stage)
            ):
                raise RecoveryDispatchClaimConflict(
                    run_id=status.run_id,
                    epoch=status.epoch,
                )
            dispatch = self._attempt_dispatch_row(
                connection,
                run_id=status.run_id,
                attempt_id=status.attempt_id,
                epoch=status.epoch,
                stage=stage,
            )
            if dispatch is None:
                raise RecoveryDispatchClaimConflict(
                    run_id=status.run_id,
                    epoch=status.epoch,
                )

    def assert_dispatch_claim(
        self,
        *,
        dispatch_id: str,
        claim_token: str,
        observed_at: datetime,
    ) -> RecoveryDispatchClaim:
        """Fence preparation artifact work to one live, full-budget claim."""

        observed = self._require_dispatch_time(observed_at, field="observed_at")
        with self._engine.connect() as connection:
            row = self._dispatch_row(connection, dispatch_id=dispatch_id)
            if row is None:
                raise RecoveryDispatchClaimConflict(run_id="unknown", epoch=0)
            status = self._latest(connection, str(row["run_id"]))
            if status is None:
                raise RecoveryDispatchClaimConflict(
                    run_id=str(row["run_id"]),
                    epoch=int(row["epoch"]),
                )
            if (
                str(row["stage"]) != RecoveryDispatchStage.PREPARATION.value
                or str(row["state"]) != RecoveryDispatchState.CLAIMED.value
                or str(row["claim_token"]) != str(claim_token)
                or row["claimed_at"] is None
                or row["claim_expires_at"] is None
                or _aware_datetime(row["claim_expires_at"]) <= observed
                or status.attempt_id != str(row["attempt_id"])
                or status.epoch != int(row["epoch"])
                or not self._dispatch_is_authorized(
                    status=status,
                    stage=RecoveryDispatchStage.PREPARATION,
                )
            ):
                raise RecoveryDispatchClaimConflict(
                    run_id=str(row["run_id"]),
                    epoch=int(row["epoch"]),
                )
            return _dispatch_claim(row)

    def renew_dispatch_claim(
        self,
        *,
        dispatch_id: str,
        claim_token: str,
        observed_at: datetime,
        requested_expires_at: datetime,
    ) -> RecoveryDispatchClaim:
        """Atomically validate and extend one live claim, capped by budget."""

        observed = self._require_dispatch_time(observed_at, field="observed_at")
        requested = self._require_dispatch_time(
            requested_expires_at,
            field="requested_expires_at",
        )
        if requested <= observed:
            raise ValueError("requested_expires_at must follow observed_at")
        with self._engine.begin() as connection:
            row = self._dispatch_row(connection, dispatch_id=dispatch_id)
            if row is None:
                raise RecoveryDispatchClaimConflict(run_id="unknown", epoch=0)
            status = self._latest(connection, str(row["run_id"]))
            current_expiry = (
                None
                if row["claim_expires_at"] is None
                else _aware_datetime(row["claim_expires_at"])
            )
            if (
                status is None
                or str(row["stage"]) != RecoveryDispatchStage.PREPARATION.value
                or str(row["state"]) != RecoveryDispatchState.CLAIMED.value
                or str(row["claim_token"]) != str(claim_token)
                or current_expiry is None
                or current_expiry <= observed
                or status.active_deadline_at <= observed
                or status.attempt_id != str(row["attempt_id"])
                or status.epoch != int(row["epoch"])
                or not self._dispatch_is_authorized(
                    status=status,
                    stage=RecoveryDispatchStage.PREPARATION,
                )
            ):
                raise RecoveryDispatchClaimConflict(
                    run_id=str(row["run_id"]),
                    epoch=int(row["epoch"]),
                )
            effective_expiry = min(requested, status.active_deadline_at)
            renewed = connection.execute(
                update(_dispatches)
                .where(
                    and_(
                        _dispatches.c.dispatch_id == str(dispatch_id),
                        _dispatches.c.state == RecoveryDispatchState.CLAIMED.value,
                        _dispatches.c.claim_token == str(claim_token),
                        _dispatches.c.claim_expires_at == row["claim_expires_at"],
                    )
                )
                .values(claim_expires_at=effective_expiry)
            )
            if renewed.rowcount != 1:
                raise RecoveryDispatchClaimConflict(
                    run_id=str(row["run_id"]),
                    epoch=int(row["epoch"]),
                )
            updated = self._dispatch_row(connection, dispatch_id=dispatch_id)
            assert updated is not None
            return _dispatch_claim(updated)

    def assert_recovery_dispatch_claim(
        self,
        *,
        dispatch_id: str,
        claim_token: str,
        observed_at: datetime,
    ) -> RecoveryDispatchClaim:
        """Fence every physical read/write to the exact live RECOVERY token."""

        observed = self._require_dispatch_time(observed_at, field="observed_at")
        token = self._require_non_empty_text(claim_token, field="claim_token")
        with self._engine.connect() as connection:
            row = self._dispatch_row(connection, dispatch_id=dispatch_id)
            if row is None:
                raise RecoveryDispatchClaimConflict(run_id="unknown", epoch=0)
            status = self._latest(connection, str(row["run_id"]))
            if status is None or (
                str(row["stage"]) != RecoveryDispatchStage.RECOVERY.value
                or str(row["state"]) != RecoveryDispatchState.CLAIMED.value
                or str(row["claim_token"]) != token
                or row["claim_expires_at"] is None
                or _aware_datetime(row["claim_expires_at"]) <= observed
                or status.run_id != str(row["run_id"])
                or status.attempt_id != str(row["attempt_id"])
                or status.epoch != int(row["epoch"])
                or not self._dispatch_is_authorized(
                    status=status,
                    stage=RecoveryDispatchStage.RECOVERY,
                )
            ):
                raise RecoveryDispatchClaimConflict(
                    run_id=str(row["run_id"]),
                    epoch=int(row["epoch"]),
                )
            self._require_slot(connection, status=status)
            return _dispatch_claim(row)

    def heartbeat_recovery(
        self,
        *,
        dispatch_id: str,
        claim_token: str,
        observed_at: datetime,
        requested_expires_at: datetime,
        active_elapsed_ms: int,
        counts: RecoveryProgressCounts,
    ) -> RecoveryRunStatus:
        """CAS progress and renew the exact physical claim in one transaction."""

        observed = self._require_dispatch_time(observed_at, field="observed_at")
        requested_expiry = self._require_dispatch_time(
            requested_expires_at,
            field="requested_expires_at",
        )
        if requested_expiry <= observed:
            raise ValueError("requested_expires_at must follow observed_at")
        if not isinstance(counts, RecoveryProgressCounts):
            raise TypeError("counts must be RecoveryProgressCounts")
        token = self._require_non_empty_text(claim_token, field="claim_token")
        with self._engine.begin() as connection:
            dispatch = self._dispatch_row(connection, dispatch_id=dispatch_id)
            if dispatch is None:
                raise RecoveryDispatchClaimConflict(run_id="unknown", epoch=0)
            status = self._require_latest(connection, str(dispatch["run_id"]))
            claim_expiry = (
                None
                if dispatch["claim_expires_at"] is None
                else _aware_datetime(dispatch["claim_expires_at"])
            )
            exact_deadline_settlement = bool(
                claim_expiry == status.active_deadline_at
                and observed >= status.active_deadline_at
            )
            if (
                str(dispatch["stage"]) != RecoveryDispatchStage.RECOVERY.value
                or str(dispatch["state"]) != RecoveryDispatchState.CLAIMED.value
                or str(dispatch["claim_token"]) != token
                or claim_expiry is None
                or (claim_expiry <= observed and not exact_deadline_settlement)
                or status.attempt_id != str(dispatch["attempt_id"])
                or status.epoch != int(dispatch["epoch"])
                or status.state is not RecoveryRunState.RUNNING
                or status.phase is not RecoveryRunPhase.CUTOVER
            ):
                raise RecoveryDispatchClaimConflict(
                    run_id=status.run_id,
                    epoch=status.epoch,
                )
            self._require_slot(connection, status=status)
            if status.cancel_requested_at is not None:
                raise RecoveryDispatchClaimConflict(
                    run_id=status.run_id,
                    epoch=status.epoch,
                )
            bounded_elapsed, exhausted, reason_code = self._bounded_terminal_elapsed_ms(
                status, active_elapsed_ms
            )
            if exhausted or observed >= status.active_deadline_at:
                return self._complete_recovery_in_transaction(
                    connection,
                    current=status,
                    dispatch=dispatch,
                    claim_token=token,
                    completed_at=max(
                        status.updated_at,
                        min(observed, status.active_deadline_at),
                    ),
                    active_elapsed_ms=bounded_elapsed,
                    result=RecoveryWorkerResult(
                        outcome=RecoveryTerminalOutcome.TIMEOUT,
                        reason_code=reason_code,
                        retryable=False,
                        counts=status.counts,
                    ),
                )
            checkpoint = RecoveryCheckpoint(
                run_id=status.run_id,
                epoch=status.epoch,
                expected_progress_seq=status.progress_seq,
                phase=status.phase,
                heartbeat_at=max(status.heartbeat_at, observed),
                active_elapsed_ms=bounded_elapsed,
                counts=counts,
            )
            updated = status.advance_checkpoint(checkpoint)
            if not self._write_cas(
                connection,
                current=status,
                updated=updated,
            ):
                raise self._progress_conflict(connection, status)
            renewed = connection.execute(
                update(_dispatches)
                .where(and_(*self._dispatch_ownership_predicates(dispatch)))
                .values(
                    claim_expires_at=min(
                        requested_expiry,
                        status.active_deadline_at,
                    )
                )
            )
            if renewed.rowcount != 1:
                raise RecoveryDispatchClaimConflict(
                    run_id=status.run_id,
                    epoch=status.epoch,
                )
            return updated


class RecoveryNativeOperation(Protocol):
    def __call__(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        fence_check: Callable[[], None],
        reconcile_only: bool = False,
    ) -> RecoveryWorkerResult: ...


class RecoveryPreparationFenceCheck(Protocol):
    def __call__(self, *, manifest_ref: str | None = None) -> None: ...


class RecoveryPreparationOperation(Protocol):
    """Bounded preparation work invoked only by the durable poller.

    Implementations must call ``fence_check`` before source scanning, after
    scanning, immediately before create-only artifact publication, immediately
    after publication with ``manifest_ref=...``, and once more before returning
    the complete result.
    """

    def __call__(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        actor_id: str,
        deadline_at_monotonic: float,
        fence_check: RecoveryPreparationFenceCheck,
        checkpoint: Callable[[RecoveryProgressCounts], RecoveryProgressCounts | None],
    ) -> RecoveryPreparedResult: ...


class _PreparationProgressTracker:
    def __init__(
        self,
        initial: RecoveryProgressCounts,
        *,
        replay_prefix: bool = False,
    ) -> None:
        self._counts = initial
        self._replay_prefix = bool(replay_prefix)
        self._attempt_counts = RecoveryProgressCounts(
            boards_total=initial.boards_total,
            sources_total=initial.sources_total,
        )
        self._lock = RLock()

    def record(self, counts: RecoveryProgressCounts) -> RecoveryProgressCounts:
        if not isinstance(counts, RecoveryProgressCounts):
            raise TypeError("checkpoint counts must be RecoveryProgressCounts")
        with self._lock:
            previous = self._counts.to_dict()
            proposed = counts.to_dict()
            if proposed["boards_total"] != previous["boards_total"]:
                raise ValueError("recovery total changed: boards_total")
            if (
                proposed["sources_total"] != previous["sources_total"]
                and previous["sources_total"] != 0
            ):
                raise ValueError("recovery total changed: sources_total")
            monotonic_fields = (
                "boards_scanned",
                "sources_processed",
                "nodes_written",
                "edges_written",
                "outbox_events_drained",
                "errors",
            )
            comparison = (
                self._attempt_counts.to_dict() if self._replay_prefix else previous
            )
            for field in monotonic_fields:
                if proposed[field] < comparison[field]:
                    raise ValueError(f"recovery progress regressed: {field}")
            if self._replay_prefix:
                # A reclaimed preparation dispatch recomputes its deterministic
                # board prefix from zero.  Keep that retry locally monotonic,
                # while projecting the public durable counters as a high-water
                # mark so observers never see a regression or double count.
                self._attempt_counts = counts
                merged = proposed.copy()
                for field in monotonic_fields:
                    merged[field] = max(previous[field], proposed[field])
                self._counts = RecoveryProgressCounts(**merged)
            else:
                self._counts = counts
            return self._counts

    def snapshot(self) -> RecoveryProgressCounts:
        with self._lock:
            return self._counts


class CommunityRecoveryPreparationPoller:
    """Owned PREPARATION-only consumer of restart-safe SQL dispatches."""

    def __init__(
        self,
        *,
        store: SQLAlchemyRecoveryRunStore,
        operation: RecoveryPreparationOperation,
        poll_interval_seconds: float = 0.25,
        heartbeat_interval_ms: int = 4_000,
        wall_clock: Callable[[], datetime] = _utc_now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        worker_id: str | None = None,
        max_retry_attempts: int = _DEFAULT_PREPARATION_MAX_ATTEMPTS,
        retry_backoff_seconds: float = _DEFAULT_PREPARATION_RETRY_BACKOFF_SECONDS,
    ) -> None:
        if not isinstance(store, SQLAlchemyRecoveryRunStore):
            raise TypeError("store must be SQLAlchemyRecoveryRunStore")
        if not callable(operation):
            raise TypeError("operation must be callable")
        interval = float(poll_interval_seconds)
        if interval <= 0 or interval > 5:
            raise ValueError("poll_interval_seconds must be within (0, 5]")
        if not callable(wall_clock):
            raise TypeError("wall_clock must be callable")
        heartbeat = int(heartbeat_interval_ms)
        if heartbeat <= 0 or heartbeat > RECOVERY_HEARTBEAT_INTERVAL_MS:
            raise ValueError("heartbeat_interval_ms must be in 1..5000")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        retries = int(max_retry_attempts)
        if retries < 1 or retries > _DEFAULT_PREPARATION_MAX_ATTEMPTS:
            raise ValueError("max_retry_attempts must be within 1..3")
        retry_backoff = float(retry_backoff_seconds)
        if retry_backoff <= 0 or retry_backoff > 30:
            raise ValueError("retry_backoff_seconds must be within (0, 30]")
        self._store = store
        self._operation = operation
        self._poll_interval_seconds = interval
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._heartbeat_interval_seconds = heartbeat / 1_000
        self._worker_id = store._require_bounded_text(  # noqa: SLF001
            worker_id or f"gdr-preparer-{uuid.uuid4().hex}",
            field="worker_id",
            max_length=255,
        )
        self._max_retry_attempts = retries
        self._retry_backoff_seconds = retry_backoff
        self._wake = Event()
        self._stop = Event()
        self._lock = RLock()
        self._thread: Thread | None = None
        self._poll_context = copy_context()
        self._operation_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pulse-recovery-preparation-op",
        )
        # A running ThreadPoolExecutor future cannot be cancelled.  Keep it as
        # an explicit drain fence after its durable attempt becomes terminal;
        # the poller must not claim and queue a later dispatch behind work that
        # may still be touching preparation dependencies.
        self._draining_operation: Future[object] | None = None
        self._failure: BaseException | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            if self._stop.is_set():
                raise RuntimeError("recovery preparation poller is closed")
            poll_context = self._poll_context.copy()
            thread = Thread(
                target=poll_context.run,
                args=(self._run,),
                name="pulse-recovery-preparation",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        # Reclaim committed/unnotified work immediately after restart.
        self._wake.set()

    def notify(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("recovery preparation poller is closed")
        self._wake.set()

    def close(self, *, timeout_seconds: float = 10.0) -> None:
        close_timeout = max(0.0, float(timeout_seconds))
        close_deadline = time.monotonic() + close_timeout
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=close_timeout)
            if thread.is_alive():
                # Python cannot terminate a running executor thread.  Stop
                # accepting queued work and return a fail-closed signal; the
                # process must be restarted if the operation never cooperates.
                self._operation_executor.shutdown(
                    wait=False,
                    cancel_futures=True,
                )
                raise TimeoutError(
                    "recovery preparation poller did not drain before close "
                    "deadline; process restart is required for non-cooperative "
                    "preparation work"
                )
        draining = self._draining_operation
        if draining is not None and not draining.done():
            try:
                draining.result(timeout=max(0.0, close_deadline - time.monotonic()))
            except TimeoutError as exc:
                self._operation_executor.shutdown(
                    wait=False,
                    cancel_futures=True,
                )
                raise TimeoutError(
                    "recovery preparation operation remained active after its "
                    "durable attempt ended; process restart is required"
                ) from exc
            except BaseException:
                pass
        self._operation_executor.shutdown(wait=True, cancel_futures=False)
        failure = self._failure
        if failure is not None:
            raise RuntimeError("recovery preparation poller failed") from failure

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._claim_and_prepare_one():
                    continue
                self._wake.wait(self._poll_interval_seconds)
                self._wake.clear()
        except BaseException as exc:  # pragma: no cover - catastrophic guard
            self._failure = exc
            logger.critical(
                "global recovery preparation poller stopped unexpectedly",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _claim_and_prepare_one(self) -> bool:
        draining = self._draining_operation
        if draining is not None:
            if not draining.done():
                return False
            # Retrieve the result/exception so the completed future is fully
            # observed before the sole executor is reused.
            with suppress(BaseException):
                draining.result()
            self._draining_operation = None

        claimed_at = self._wall_clock()
        try:
            claim = self._store.claim_next_dispatch(
                stage=RecoveryDispatchStage.PREPARATION,
                worker_id=self._worker_id,
                claimed_at=claimed_at,
                claim_expires_at=claimed_at
                + timedelta(milliseconds=RECOVERY_WORKER_LEASE_MS),
            )
        except SQLAlchemyError as exc:
            logger.warning(
                "global recovery preparation claim temporarily unavailable: %s",
                type(exc).__name__,
            )
            return False
        if claim is None:
            return False
        preparing: RecoveryRunStatus | None = None
        progress: _PreparationProgressTracker | None = None
        started_monotonic: float | None = None
        published_manifest_ref: str | None = None
        future: Future[object] | None = None
        try:
            preparing = self._store.mark_preparing(
                run_id=claim.run_id,
                attempt_id=claim.attempt_id,
                epoch=claim.epoch,
                claim_token=claim.claim_token,
                at=claimed_at,
            )
            progress = _PreparationProgressTracker(
                preparing.counts,
                replay_prefix=claim.attempt_count > 1,
            )
            started_monotonic = self._monotonic_clock()
            operation_observed_at = max(claimed_at, self._wall_clock())
            remaining_active_ms = max(
                0,
                preparing.attempt_budget_ms - preparing.active_elapsed_ms,
            )
            remaining_wall_ms = max(
                0,
                int(
                    (
                        preparing.active_deadline_at - operation_observed_at
                    ).total_seconds()
                    * 1_000
                ),
            )
            remaining_attempt_ms = min(
                remaining_active_ms,
                remaining_wall_ms,
            )
            deadline_at_monotonic = started_monotonic + remaining_attempt_ms / 1_000
            heartbeat_lock = RLock()

            def fence_check(*, manifest_ref: str | None = None) -> None:
                nonlocal published_manifest_ref
                with heartbeat_lock:
                    if manifest_ref is not None:
                        normalized_manifest = str(manifest_ref).strip()
                        if not normalized_manifest:
                            raise ValueError("manifest_ref must be non-empty")
                        if (
                            published_manifest_ref is not None
                            and published_manifest_ref != normalized_manifest
                        ):
                            raise RecoveryPreparationTerminalError(
                                "global_discovery_recovery_preparation_manifest_conflict",
                                manifest_ref=published_manifest_ref,
                            )
                        published_manifest_ref = normalized_manifest
                    status = self._persist_preparation_heartbeat(
                        claim=claim,
                        started_monotonic=started_monotonic,
                        baseline_elapsed_ms=preparing.active_elapsed_ms,
                        deadline_at_monotonic=deadline_at_monotonic,
                        attempt_budget_ms=preparing.attempt_budget_ms,
                        counts=progress.snapshot(),
                        manifest_ref=published_manifest_ref,
                    )
                    if status.state.is_terminal:
                        raise RecoveryPreparationFenceError(status.reason_code)

            fence_check()
            operation_context = copy_context()
            future = self._operation_executor.submit(
                operation_context.run,
                self._operation,
                run_id=claim.run_id,
                epoch=claim.epoch,
                attempt_id=claim.attempt_id,
                actor_id=preparing.actor_id,
                deadline_at_monotonic=deadline_at_monotonic,
                fence_check=fence_check,
                checkpoint=progress.record,
            )
            operation_error: BaseException | None = None
            prepared: object | None = None
            while True:
                remaining_seconds = max(
                    0.0,
                    deadline_at_monotonic - self._monotonic_clock(),
                )
                try:
                    prepared = future.result(
                        timeout=min(
                            self._heartbeat_interval_seconds,
                            remaining_seconds,
                        )
                    )
                    break
                except TimeoutError:
                    if future.done():
                        try:
                            prepared = future.result()
                        except BaseException as completed_error:
                            operation_error = completed_error
                        break
                    with heartbeat_lock:
                        status = self._persist_preparation_heartbeat(
                            claim=claim,
                            started_monotonic=started_monotonic,
                            baseline_elapsed_ms=preparing.active_elapsed_ms,
                            deadline_at_monotonic=deadline_at_monotonic,
                            attempt_budget_ms=preparing.attempt_budget_ms,
                            counts=progress.snapshot(),
                            manifest_ref=published_manifest_ref,
                        )
                    if status.state.is_terminal:
                        future.cancel()
                        if not future.done():
                            self._draining_operation = future
                        return True
                except BaseException as exc:  # operation failure is data
                    operation_error = exc
                    break
            if operation_error is None:
                try:
                    if not isinstance(prepared, RecoveryPreparedResult):
                        raise TypeError(
                            "preparation operation must return RecoveryPreparedResult"
                        )
                    progress.record(prepared.counts)
                    # A reclaimed attempt replays its deterministic prefix from
                    # zero, while the durable run exposes per-field high-water
                    # counters from every prior attempt.  Complete with that
                    # merged snapshot too; passing the raw retry-local result
                    # would make mark_prepared reject a durable regression
                    # (notably a prior retryable error returning to zero).
                    prepared = replace(prepared, counts=progress.snapshot())
                    if published_manifest_ref is None:
                        raise RecoveryPreparationTerminalError(
                            "global_discovery_recovery_preparation_manifest_fence_missing",
                            manifest_ref=prepared.manifest_ref,
                        )
                    if published_manifest_ref != prepared.manifest_ref:
                        raise RecoveryPreparationTerminalError(
                            "global_discovery_recovery_preparation_manifest_conflict",
                            manifest_ref=published_manifest_ref,
                        )
                except BaseException as exc:
                    operation_error = exc
            if operation_error is not None:
                if (
                    isinstance(operation_error, RecoveryPreparationError)
                    and operation_error.manifest_ref is not None
                ):
                    if (
                        published_manifest_ref is not None
                        and published_manifest_ref != operation_error.manifest_ref
                    ):
                        operation_error = RecoveryPreparationTerminalError(
                            "global_discovery_recovery_preparation_manifest_conflict",
                            manifest_ref=published_manifest_ref,
                        )
                    else:
                        published_manifest_ref = operation_error.manifest_ref
                self._record_preparation_operation_failure(
                    claim=claim,
                    preparing=preparing,
                    progress=progress,
                    started_monotonic=started_monotonic,
                    error=operation_error,
                    published_manifest_ref=published_manifest_ref,
                )
                return True
            assert isinstance(prepared, RecoveryPreparedResult)
            published_manifest_ref = prepared.manifest_ref
            try:
                self._store.complete_preparation(
                    run_id=claim.run_id,
                    attempt_id=claim.attempt_id,
                    epoch=claim.epoch,
                    claim_token=claim.claim_token,
                    completed_at=self._wall_clock(),
                    result=prepared,
                )
            except ValueError:
                self._record_preparation_operation_failure(
                    claim=claim,
                    preparing=preparing,
                    progress=progress,
                    started_monotonic=started_monotonic,
                    error=RecoveryPreparationTerminalError(
                        "global_discovery_recovery_preparation_result_invalid",
                        manifest_ref=prepared.manifest_ref,
                    ),
                    published_manifest_ref=prepared.manifest_ref,
                )
        except (
            RecoveryDispatchClaimConflict,
            RecoveryCheckpointConflict,
            RecoveryPreparationFenceError,
            RecoveryRunNotFound,
        ):
            logger.info(
                "global recovery preparation claim fenced run=%s epoch=%d",
                claim.run_id,
                claim.epoch,
            )
        except SQLAlchemyError as exc:
            # Database availability errors are not adapter-result failures. The
            # exact durable claim remains fenced and becomes reclaimable.
            logger.warning(
                "global recovery preparation persistence unavailable: %s",
                type(exc).__name__,
                extra={
                    "run_id": claim.run_id,
                    "attempt_id": claim.attempt_id,
                    "epoch": claim.epoch,
                },
            )
        except BaseException as exc:  # deterministic poller/adapter fault
            logger.error(
                "global recovery preparation poller fault: %s",
                type(exc).__name__,
                extra={
                    "run_id": claim.run_id,
                    "attempt_id": claim.attempt_id,
                    "epoch": claim.epoch,
                },
            )
            self._terminalize_unexpected_preparation_fault(
                claim=claim,
                preparing=preparing,
                progress=progress,
                started_monotonic=started_monotonic,
                manifest_ref=published_manifest_ref,
            )
        finally:
            # Any early persistence/fencing exit must retain the same drain
            # gate as a normal terminal heartbeat.  Otherwise a recovered
            # poller could claim a later dispatch and merely queue it behind a
            # still-running sole-executor operation.
            if future is not None and not future.done():
                future.cancel()
                self._draining_operation = future
        return True

    def _terminalize_unexpected_preparation_fault(
        self,
        *,
        claim: RecoveryDispatchClaim,
        preparing: RecoveryRunStatus | None,
        progress: _PreparationProgressTracker | None,
        started_monotonic: float | None,
        manifest_ref: str | None,
    ) -> None:
        current = preparing or self._store.get_status(run_id=claim.run_id)
        if current is None or current.state.is_terminal:
            return
        current_counts = current.counts if progress is None else progress.snapshot()
        counts_payload = current_counts.to_dict()
        counts_payload["errors"] = int(counts_payload["errors"]) + 1
        elapsed_ms = current.active_elapsed_ms
        if started_monotonic is not None:
            elapsed_ms += max(
                0,
                int((self._monotonic_clock() - started_monotonic) * 1_000),
            )
        failed_at = self._wall_clock()
        try:
            self._store.record_preparation_failure(
                dispatch_id=claim.dispatch_id,
                claim_token=claim.claim_token,
                failed_at=failed_at,
                active_elapsed_ms=min(current.attempt_budget_ms, elapsed_ms),
                counts=RecoveryProgressCounts(**counts_payload),
                reason_code=_PREPARATION_FAILURE_REASON,
                retryable=False,
                retry_available_at=failed_at
                + timedelta(seconds=self._retry_backoff_seconds),
                max_attempts=self._max_retry_attempts,
                manifest_ref=manifest_ref,
            )
        except (
            RecoveryDispatchClaimConflict,
            RecoveryCheckpointConflict,
            RecoveryRunNotFound,
        ):
            return
        except BaseException as settlement_error:
            logger.error(
                "global recovery preparation terminal settlement failed: %s",
                type(settlement_error).__name__,
                extra={
                    "run_id": claim.run_id,
                    "attempt_id": claim.attempt_id,
                    "epoch": claim.epoch,
                },
            )

    def _record_preparation_operation_failure(
        self,
        *,
        claim: RecoveryDispatchClaim,
        preparing: RecoveryRunStatus,
        progress: _PreparationProgressTracker,
        started_monotonic: float,
        error: BaseException,
        published_manifest_ref: str | None,
    ) -> RecoveryRunStatus | None:
        if isinstance(
            error,
            (
                RecoveryDispatchClaimConflict,
                RecoveryCheckpointConflict,
                RecoveryPreparationFenceError,
                RecoveryRunNotFound,
            ),
        ):
            return None
        if isinstance(error, RecoveryPreparationError):
            reason_code = error.code
            manifest_ref = published_manifest_ref or error.manifest_ref
            retryable = error.retryable and manifest_ref is None
        else:
            reason_code = _PREPARATION_FAILURE_REASON
            retryable = False
            manifest_ref = published_manifest_ref
        counts_payload = progress.snapshot().to_dict()
        counts_payload["errors"] = int(counts_payload["errors"]) + 1
        failed_counts = RecoveryProgressCounts(**counts_payload)
        failed_at = self._wall_clock()
        backoff_seconds = min(
            30.0,
            self._retry_backoff_seconds * (2 ** max(0, int(claim.attempt_count) - 1)),
        )
        elapsed_ms = min(
            preparing.attempt_budget_ms,
            preparing.active_elapsed_ms
            + max(
                0,
                int((self._monotonic_clock() - started_monotonic) * 1_000),
            ),
        )
        try:
            updated = self._store.record_preparation_failure(
                dispatch_id=claim.dispatch_id,
                claim_token=claim.claim_token,
                failed_at=failed_at,
                active_elapsed_ms=elapsed_ms,
                counts=failed_counts,
                reason_code=reason_code,
                retryable=retryable,
                retry_available_at=failed_at + timedelta(seconds=backoff_seconds),
                max_attempts=self._max_retry_attempts,
                manifest_ref=manifest_ref,
            )
        except (
            RecoveryDispatchClaimConflict,
            RecoveryCheckpointConflict,
            RecoveryRunNotFound,
        ):
            return None
        logger.warning(
            "global recovery preparation operation failed type=%s code=%s retryable=%s",
            type(error).__name__,
            reason_code,
            retryable,
            extra={
                "run_id": claim.run_id,
                "attempt_id": claim.attempt_id,
                "epoch": claim.epoch,
                "reason_code": reason_code,
                "retryable": retryable,
            },
        )
        return updated

    def _persist_preparation_heartbeat(
        self,
        *,
        claim: RecoveryDispatchClaim,
        started_monotonic: float,
        baseline_elapsed_ms: int,
        deadline_at_monotonic: float,
        attempt_budget_ms: int,
        counts: RecoveryProgressCounts,
        manifest_ref: str | None = None,
    ) -> RecoveryRunStatus:
        observed_monotonic = self._monotonic_clock()
        elapsed_ms = baseline_elapsed_ms + max(
            0,
            int((observed_monotonic - started_monotonic) * 1_000),
        )
        if observed_monotonic >= deadline_at_monotonic:
            elapsed_ms = max(elapsed_ms, int(attempt_budget_ms))
        observed_at = self._wall_clock()
        return self._store.heartbeat_preparation(
            dispatch_id=claim.dispatch_id,
            claim_token=claim.claim_token,
            observed_at=observed_at,
            requested_expires_at=observed_at
            + timedelta(milliseconds=RECOVERY_WORKER_LEASE_MS),
            active_elapsed_ms=elapsed_ms,
            counts=counts,
            manifest_ref=manifest_ref,
        )


class CommunityDurableRecoveryDispatcher:
    """Control-facing wakeup adapter; never executes recovery work inline."""

    def __init__(
        self,
        *,
        store: SQLAlchemyRecoveryRunStore,
        preparation_poller: CommunityRecoveryPreparationPoller,
        recovery_worker: CommunityRecoveryWorker | None = None,
    ) -> None:
        self._store = store
        self._preparation_poller = preparation_poller
        self._recovery_worker = recovery_worker

    def dispatch(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        kind: RecoveryDispatchKind,
    ) -> None:
        normalized_kind = RecoveryDispatchKind(kind)
        self._store.assert_dispatch_enqueued(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            kind=normalized_kind,
        )
        if normalized_kind is RecoveryDispatchKind.PREPARATION:
            self._preparation_poller.notify()
        elif self._recovery_worker is None:
            raise RecoveryStoreContractError(
                code="global_discovery_recovery_consumer_unavailable"
            )
        else:
            self._recovery_worker.notify()


class RecoveryRuntimeConfigurationError(RuntimeError):
    def __init__(self, *, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def synchronous_recovery_database_url(database_url: str) -> str:
    """Derive the one synchronous, file-backed Community recovery URL."""

    url = make_url(str(database_url))
    if url.drivername not in {"sqlite", "sqlite+aiosqlite"}:
        raise RecoveryRuntimeConfigurationError(
            code="recovery_store_database_driver_unsupported"
        )
    if not url.database or url.database == ":memory:":
        raise RecoveryRuntimeConfigurationError(
            code="recovery_store_requires_file_backed_sqlite"
        )
    return url.set(drivername="sqlite").render_as_string(hide_password=False)


@dataclass(frozen=True, slots=True)
class RecoveryNativeInputs:
    """Durable-bound physical inputs loaded by the owned worker."""

    expected_live_sha256: str
    boards: tuple[GlobalDiscoveryBoardSeed, ...]
    terminal_counts: RecoveryProgressCounts

    def __post_init__(self) -> None:
        digest = str(self.expected_live_sha256).strip().lower()
        if len(digest) != 64:
            raise ValueError("expected_live_sha256 must be a sha256 hex digest")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError(
                "expected_live_sha256 must be a sha256 hex digest"
            ) from exc
        object.__setattr__(self, "expected_live_sha256", digest)
        boards = tuple(self.boards)
        if not boards or any(
            not isinstance(board, GlobalDiscoveryBoardSeed) for board in boards
        ):
            raise ValueError("boards must contain GlobalDiscoveryBoardSeed values")
        if len({board.board_id for board in boards}) != len(boards):
            raise ValueError("boards must have unique board_id values")
        object.__setattr__(self, "boards", boards)
        if not isinstance(self.terminal_counts, RecoveryProgressCounts):
            raise TypeError("terminal_counts must be RecoveryProgressCounts")


class RecoveryNativeInputProvider(Protocol):
    def __call__(
        self,
        *,
        run_id: str,
        epoch: int,
    ) -> RecoveryNativeInputs: ...


class CommunityDurableRecoveryInputProvider:
    """Load the create-only Core worker bundle through the edition store."""

    def __init__(self, *, artifact_store: object) -> None:
        self._store = GlobalDiscoveryRecoveryWorkerInputStore(artifact_store)

    def __call__(
        self,
        *,
        run_id: str,
        epoch: int,
    ) -> RecoveryNativeInputs:
        if int(epoch) < 1:
            raise ValueError("epoch must be at least 1")
        try:
            stored = self._store.load(str(run_id), epoch=int(epoch))
        except GlobalDiscoveryRecoveryError as exc:
            raise RecoveryRuntimeConfigurationError(code=exc.code) from exc
        except (OSError, UnicodeError, ValueError) as exc:
            raise RecoveryRuntimeConfigurationError(
                code="recovery_worker_inputs_invalid"
            ) from exc
        if stored is None:
            raise RecoveryRuntimeConfigurationError(
                code="recovery_worker_inputs_missing"
            )
        return RecoveryNativeInputs(
            expected_live_sha256=stored.expected_live_sha256,
            boards=stored.boards,
            terminal_counts=stored.terminal_counts,
        )

    def handoff_resume_inputs(
        self,
        previous: RecoveryRunStatus,
        resumed: RecoveryRunStatus,
    ) -> None:
        """Create-only, hash-validated physical input binding for epoch N+1."""

        if (
            previous.run_id != resumed.run_id
            or resumed.epoch != previous.epoch + 1
            or resumed.supersedes_epoch != previous.epoch
            or resumed.confirmation_state is not RecoveryConfirmationState.CONSUMED
            or resumed.phase
            not in {RecoveryRunPhase.CONFIRMED, RecoveryRunPhase.CUTOVER}
        ):
            raise RecoveryRuntimeConfigurationError(
                code="recovery_resume_input_handoff_invalid"
            )
        try:
            source = self._store.load(
                previous.run_id,
                epoch=previous.epoch,
            )
        except GlobalDiscoveryRecoveryError as exc:
            raise RecoveryRuntimeConfigurationError(code=exc.code) from exc
        if source is None:
            raise RecoveryRuntimeConfigurationError(
                code="recovery_resume_source_inputs_missing"
            )
        source_binding = source.command.binding
        if (
            source.command.expected_epoch != previous.epoch
            or source_binding.run_id != previous.run_id
            or source_binding.actor_id != resumed.binding.actor_id
            or source_binding.confirmation_fingerprint
            != resumed.binding.confirmation_fingerprint
            or source_binding.manifest_ref != resumed.binding.manifest_ref
            or source_binding.preflight_hash != resumed.binding.preflight_hash
        ):
            raise RecoveryRuntimeConfigurationError(
                code="recovery_resume_source_inputs_binding_conflict"
            )
        rebound = GlobalDiscoveryRecoveryWorkerInputs(
            command=RecoveryStartCommand(
                binding=resumed.binding,
                started_at=resumed.started_at,
                counts=resumed.counts,
                attempt_budget_ms=resumed.attempt_budget_ms,
                expected_epoch=resumed.epoch,
                confirmed_by_actor_id=(
                    resumed.confirmed_by_actor_id or resumed.actor_id
                ),
                confirmation_consumed_at=resumed.started_at,
            ),
            expected_live_sha256=source.expected_live_sha256,
            boards=source.boards,
            terminal_counts=source.terminal_counts,
        )
        try:
            persisted = self._store.put(rebound)
        except GlobalDiscoveryRecoveryError as exc:
            raise RecoveryRuntimeConfigurationError(code=exc.code) from exc
        if (
            persisted.command.expected_epoch != resumed.epoch
            or persisted.semantic_payload() != rebound.semantic_payload()
        ):
            raise RecoveryRuntimeConfigurationError(
                code="recovery_resume_input_handoff_invalid"
            )


class CommunityGlobalDiscoveryRecoveryNativeOperation:
    """Bridge the owned worker to the existing fenced physical cutover port."""

    def __init__(
        self,
        *,
        recovery: GlobalDiscoveryRecovery,
        input_provider: RecoveryNativeInputProvider,
    ) -> None:
        if not isinstance(recovery, GlobalDiscoveryRecovery):
            raise TypeError("recovery must implement GlobalDiscoveryRecovery")
        if not callable(input_provider):
            raise TypeError("input_provider must be callable")
        self._recovery = recovery
        self._input_provider = input_provider

    def __call__(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        fence_check: Callable[[], None],
        reconcile_only: bool = False,
        predecessor_reconcile: "RecoveryPredecessorReconcilePlan | None" = None,
    ) -> RecoveryWorkerResult:
        fence_check()
        # B6.4: validate the bound plan identity BEFORE the writer lease / any
        # physical mutation.  A plan that does not match this exact run/successor
        # epoch or whose immediate ancestry identity is wrong is corruption and
        # must fail closed with zero mutation (the resolver already fails closed;
        # this is the native-operation defence-in-depth layer).
        if predecessor_reconcile is not None and not reconcile_only:
            plan = predecessor_reconcile
            if (
                str(plan.run_id) != str(run_id)
                or int(plan.successor_epoch) != int(epoch)
                or not plan.ancestry
                or len(plan.ancestry) > _MAX_PENDING_ANCESTRY_HOPS
            ):
                raise RecoveryPendingAncestryError(
                    "recovery_pending_ancestry_plan_mismatch"
                )
            # Validate the FULL ancestry: the first entry MUST be the exact
            # immediate predecessor (successor_epoch - 1); every tuple has the exact
            # canonical attempt id; strict descending order; no duplicate epoch.
            prev = int(epoch)
            plan_seen: set[int] = set()
            for _idx, (_pe, _pa) in enumerate(plan.ancestry):
                _pe = int(_pe)
                if _idx == 0 and _pe != int(epoch) - 1:
                    raise RecoveryPendingAncestryError(
                        "recovery_pending_ancestry_plan_mismatch"
                    )
                if _pe < 1 or _pe >= prev or _pe in plan_seen:
                    raise RecoveryPendingAncestryError(
                        "recovery_pending_ancestry_plan_mismatch"
                    )
                if _pa != recovery_attempt_id(str(run_id), _pe):
                    raise RecoveryPendingAncestryError(
                        "recovery_pending_ancestry_plan_mismatch"
                    )
                plan_seen.add(_pe)
                prev = _pe
        inputs = self._input_provider(run_id=str(run_id), epoch=int(epoch))
        if not isinstance(inputs, RecoveryNativeInputs):
            raise TypeError("input_provider must return RecoveryNativeInputs")
        fence_check()
        lease = GlobalDiscoveryWriterLease.acquire(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:{attempt_id}",
            # This lease expires before the durable SQL dispatch claim.  Both
            # are renewed by physical checkpoints, so a process crash leaves
            # no hour-long writer tombstone blocking same-epoch adoption.
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        )
        renew_stop = Event()
        renew_failure: list[BaseException] = []

        def renew_writer_lease() -> None:
            while not renew_stop.wait(_RECOVERY_WRITER_RENEW_INTERVAL_SECONDS):
                try:
                    # Keep the durable writer lane owned while an indivisible
                    # native query/fsync is draining.  The exact SQL claim is
                    # still reasserted before the next physical operation.
                    lease.renew()
                except BaseException as exc:  # pragma: no cover - exercised via fence
                    renew_failure.append(exc)
                    return

        # Coordination providers are runtime-context scoped.  A raw thread
        # would lose the edition-owned write-lock port and make the first real
        # lease renewal fail with ``CoordinationProviderMissing``.  Enter one
        # captured context for the whole renewal loop so every iteration sees
        # the same composition as the native recovery attempt.
        renew_context = copy_context()
        renew_thread = Thread(
            target=renew_context.run,
            args=(renew_writer_lease,),
            name="pulse-recovery-writer-lease-renewal",
            daemon=True,
        )
        renew_thread.start()
        cutover = None
        try:
            with lease.guard():

                def physical_fence_check() -> None:
                    if renew_failure:
                        raise renew_failure[0]
                    fence_check()
                    lease.renew()
                    fence_check()
                    lease.assert_fenced()

                cutover = None
                # B6.2/B6.3 reconcile-before-mutate: when an EXPLICIT, persisted-
                # chain-validated predecessor plan is bound (resolved in
                # ``_run_attempt`` from the exact PARTIAL/reconciliation_pending
                # predecessor — never epoch arithmetic), reconcile that BOUND
                # predecessor's crossed journal/pointer under the NEW fence BEFORE
                # ANY other physical operation, including retention/pruning.  If it
                # already crossed the pointer boundary, finish the already-crossed
                # physical truth idempotently and adopt it as this run's terminal
                # result — no fresh candidate/pointer/retention write precedes it.
                if predecessor_reconcile is not None and not reconcile_only:
                    reconcile_and_complete = getattr(
                        self._recovery,
                        "reconcile_predecessor_and_complete",
                        None,
                    )
                    if not callable(reconcile_and_complete):
                        raise RecoveryRuntimeConfigurationError(
                            code="recovery_predecessor_reconciler_unavailable"
                        )
                    # B6.4/B6.5: reconcile the bound pending ANCESTRY (immediate
                    # predecessor -> source) AND write the successor's OWN
                    # completed-first journal; the returned cutover is bound to the
                    # successor's own journal (not the predecessor's result).
                    cutover = reconcile_and_complete(
                        run_id=str(run_id),
                        epoch=int(epoch),
                        attempt_id=str(attempt_id),
                        ancestry=tuple(
                            (int(pe), str(pa))
                            for pe, pa in predecessor_reconcile.ancestry
                        ),
                        expected_live_sha256=inputs.expected_live_sha256,
                        boards=inputs.boards,
                        fence_check=physical_fence_check,
                    )

                reconcile_artifacts = getattr(
                    self._recovery,
                    "reconcile_attempt_artifacts",
                    None,
                )
                if not callable(reconcile_artifacts):
                    raise RecoveryRuntimeConfigurationError(
                        code="recovery_attempt_reconciler_unavailable"
                    )
                # Retention/pruning is itself filesystem mutation and MUST NOT
                # precede predecessor reconciliation (B6.2).
                reconcile_artifacts(
                    run_id=str(run_id),
                    known_attempt_ids=tuple(
                        recovery_attempt_id(str(run_id), known_epoch)
                        for known_epoch in range(1, int(epoch) + 1)
                    ),
                    now=datetime.now(timezone.utc),
                    fence_check=physical_fence_check,
                )

                if cutover is None:
                    if reconcile_only:
                        reconcile = getattr(
                            self._recovery,
                            "reconcile_attempt_terminal_truth",
                            None,
                        )
                        if not callable(reconcile):
                            raise RecoveryRuntimeConfigurationError(
                                code="recovery_terminal_reconciler_unavailable"
                            )
                        cutover = reconcile(
                            run_id=str(run_id),
                            epoch=int(epoch),
                            attempt_id=str(attempt_id),
                            expected_live_sha256=inputs.expected_live_sha256,
                            boards=inputs.boards,
                            fence_check=physical_fence_check,
                        )
                    else:
                        # No bound predecessor truth to heal: normal unified
                        # recovery (adopt complete primary or authoritative-seed
                        # rebuild) with the exact current run/epoch/attempt identity.
                        cutover = self._recovery.recover_and_cutover(
                            run_id=str(run_id),
                            epoch=int(epoch),
                            attempt_id=str(attempt_id),
                            expected_live_sha256=inputs.expected_live_sha256,
                            boards=inputs.boards,
                            fence_check=physical_fence_check,
                        )
        except CommunityGlobalDiscoveryRecoveryFenceError as exc:
            # Preserve the worker's typed stale-token/deadline signal.  The
            # adapter wraps it only to prevent its broad recovery handler from
            # writing under a lost fence.
            raise exc.original
        except GlobalDiscoveryWriterFenceLost:
            if cutover is None:
                raise
            # ``guard`` reasserts the durable lease on exit.  Once the adapter
            # returned a terminal journal result, a loss at that final check
            # cannot erase the durable pointer/journal truth.
        finally:
            renew_stop.set()
            renew_thread.join(timeout=5.0)
            lease.release()
        if cutover is None:
            return RecoveryWorkerResult(
                outcome=RecoveryTerminalOutcome.TIMEOUT,
                reason_code="recovery_attempt_budget_exhausted",
                retryable=False,
                counts=inputs.terminal_counts,
            )
        physical_truth = RecoveryPhysicalTruth(
            attempt_id=str(attempt_id),
            journal_phase=str(cutover.outcome),
            # ``rolled_back`` is only emitted after the candidate pointer was
            # durably installed and the previous pointer was restored.  The
            # irreversible cutover boundary therefore happened for both
            # terminal journal outcomes; cancellation/timeout must not erase
            # that physical fact.
            pointer_replaced=cutover.outcome in {"completed", "rolled_back"},
            rollback_performed=bool(cutover.rollback_performed),
            evidence_ref=(cutover.recovery_journal_ref or cutover.quarantine_ref),
        )
        if cutover.outcome == "completed":
            return RecoveryWorkerResult(
                outcome=RecoveryTerminalOutcome.SUCCESS,
                reason_code="global_discovery_recovery_completed",
                retryable=False,
                counts=inputs.terminal_counts,
                physical_truth=physical_truth,
            )
        if cutover.outcome == "rolled_back":
            return RecoveryWorkerResult(
                outcome=RecoveryTerminalOutcome.PARTIAL,
                reason_code=(
                    cutover.failure_code or "global_discovery_recovery_rolled_back"
                ),
                retryable=False,
                counts=inputs.terminal_counts,
                physical_truth=physical_truth,
            )
        raise RuntimeError(f"unexpected_global_discovery_cutover:{cutover.outcome}")


# B6.1: the EXACT unknown post-pointer reconciliation-pending sentinel.  Both the
# worker-side ``_complete`` override layer and the SQL-store
# ``_complete_recovery_in_transaction`` override layer must preserve THIS tuple
# verbatim (never rewrite to CANCELLED/TIMEOUT) so a late cancel/deadline cannot
# erase an unknown physical outcome after the writer fence was lost mid-cutover.
# No OTHER PARTIAL is exempt.
RECOVERY_PENDING_RECONCILIATION_REASON = "recovery_physical_reconciliation_pending"
# B6.4: hard bound on the persisted pending-ancestry walk (the epoch chain is
# finite; this only guards against a corrupt cycle / runaway chain).
_MAX_PENDING_ANCESTRY_HOPS = 64


def _is_pending_reconciliation_result(result: "RecoveryWorkerResult") -> bool:
    return (
        result.outcome is RecoveryTerminalOutcome.PARTIAL
        and result.reason_code == RECOVERY_PENDING_RECONCILIATION_REASON
        and result.retryable is False
        and result.physical_truth is None
    )


@dataclass(frozen=True, slots=True)
class RecoveryPredecessorReconcilePlan:
    """B6.2/B6.3/B6.4: an EXPLICIT, persisted-chain-validated plan binding the exact
    pending ancestry a resuming epoch must reconcile BEFORE any physical mutation.
    Resolved in ``_run_attempt`` by walking the persisted supersedes chain from the
    immediate predecessor through consecutive EXACT
    PARTIAL/recovery_physical_reconciliation_pending records (validating every link,
    bounded) down to the deepest unresolved source — never inferred from epoch
    arithmetic in the native op.  ``ancestry`` is ordered immediate-predecessor →
    source, each ``(epoch, attempt_id)``."""

    run_id: str
    successor_epoch: int
    ancestry: tuple[tuple[int, str], ...]


class RecoveryPendingAncestryError(RuntimeError):
    """B6.4 fail-closed: the persisted pending ancestry is corrupt/anomalous (a
    missing row, broken supersedes link, cycle, impossible ordering, or hop-limit
    overflow).  This must terminalize the attempt with ZERO native/physical call —
    never a silent fall-through to a fresh ``recover_and_cutover``."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class RecoveryWorkerFenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class CommunityRecoveryWorker:
    """Owned background dispatcher with heartbeat and native-drain separation."""

    def __init__(
        self,
        *,
        store: RecoveryWorkerRunStore,
        native_operation: RecoveryNativeOperation,
        heartbeat_interval_ms: int = 4_000,
        poll_interval_seconds: float = 0.25,
        wall_clock: Callable[[], datetime] = _utc_now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        worker_id: str | None = None,
    ) -> None:
        if not isinstance(store, RecoveryWorkerRunStore):
            raise TypeError("store must implement RecoveryWorkerRunStore")
        interval = int(heartbeat_interval_ms)
        if interval <= 0 or interval > RECOVERY_HEARTBEAT_INTERVAL_MS:
            raise ValueError("heartbeat_interval_ms must be in 1..5000")
        poll_interval = float(poll_interval_seconds)
        if poll_interval <= 0 or poll_interval > 5:
            raise ValueError("poll_interval_seconds must be within (0, 5]")
        if not callable(native_operation):
            raise TypeError("native_operation must be callable")
        self._store = store
        self._native_operation = native_operation
        self._heartbeat_interval_ms = interval
        self._poll_interval_seconds = poll_interval
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._worker_id = str(worker_id or f"gdr-recovery-{uuid.uuid4().hex}").strip()
        if not self._worker_id or len(self._worker_id) > 255:
            raise ValueError("worker_id must be within 1..255 chars")
        self._worker_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pulse-recovery-worker",
        )
        self._native_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pulse-recovery-native",
        )
        self._lock = RLock()
        self._attempt_futures: dict[tuple[str, int], Future[None]] = {}
        self._wake = Event()
        self._stop = Event()
        self._poll_thread: Thread | None = None
        self._poll_context = copy_context()
        self._failure: BaseException | None = None
        self._closed = False
        self._executors_shutdown = False

    def start(self) -> None:
        with self._lock:
            if self._poll_thread is not None:
                return
            if self._closed or self._stop.is_set():
                raise RuntimeError("recovery worker is closed")
            poll_context = self._poll_context.copy()
            thread = Thread(
                target=poll_context.run,
                args=(self._poll,),
                name="pulse-recovery-dispatch",
                daemon=False,
            )
            self._poll_thread = thread
            thread.start()
        # Adopt committed/unnotified RECOVERY work immediately after restart.
        self._wake.set()

    def notify(self) -> None:
        if self._closed:
            raise RuntimeError("recovery worker is closed")
        self._wake.set()

    def dispatch(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str | None = None,
        kind: RecoveryDispatchKind = RecoveryDispatchKind.RECOVERY,
    ) -> None:
        if RecoveryDispatchKind(kind) is not RecoveryDispatchKind.RECOVERY:
            raise RecoveryStoreContractError(
                code="global_discovery_recovery_two_stage_required"
            )
        if attempt_id is None:
            current = self._store.get_status(run_id=str(run_id))
            if current is None:
                raise RecoveryRunNotFound(run_id=str(run_id))
            attempt_id = current.attempt_id
        assertion = getattr(self._store, "assert_dispatch_enqueued", None)
        if not callable(assertion):
            raise RecoveryStoreContractError(
                code="global_discovery_recovery_durable_dispatch_required"
            )
        assertion(
            run_id=str(run_id),
            epoch=int(epoch),
            attempt_id=str(attempt_id),
            kind=RecoveryDispatchKind.RECOVERY,
        )
        self.start()
        self.notify()

    def _poll(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(timeout=self._poll_interval_seconds)
                self._wake.clear()
                if self._stop.is_set():
                    break
                with self._lock:
                    active = any(
                        not future.done() for future in self._attempt_futures.values()
                    )
                if active:
                    continue
                claimed_at = self._wall_clock()
                claim = self._store.claim_next_dispatch(  # type: ignore[attr-defined]
                    stage=RecoveryDispatchStage.RECOVERY,
                    worker_id=self._worker_id,
                    claimed_at=claimed_at,
                    claim_expires_at=claimed_at
                    + timedelta(milliseconds=RECOVERY_WORKER_LEASE_MS),
                )
                if claim is None:
                    continue
                self._submit_claim(claim)
        except BaseException as exc:
            self._failure = exc
            self._stop.set()
            self._wake.set()

    def _submit_claim(self, claim: RecoveryDispatchClaim) -> None:
        key = (claim.run_id, claim.epoch)
        with self._lock:
            if self._closed:
                return
            incumbent = self._attempt_futures.get(key)
            if incumbent is not None and not incumbent.done():
                return
            worker_context = copy_context()
            future = self._worker_executor.submit(
                worker_context.run,
                self._run_attempt,
                claim=claim,
            )
            self._attempt_futures[key] = future
            future.add_done_callback(lambda _future: self._wake.set())

    def close(self, *, timeout_seconds: float = 10.0) -> None:
        close_timeout = max(0.0, float(timeout_seconds))
        deadline = time.monotonic() + close_timeout
        with self._lock:
            self._closed = True
            self._stop.set()
            self._wake.set()
            poll_thread = self._poll_thread
            futures = tuple(self._attempt_futures.values())
        if poll_thread is not None:
            poll_thread.join(timeout=close_timeout)
            if poll_thread.is_alive():
                raise TimeoutError(
                    "recovery dispatch poller did not stop before close deadline"
                )
        _, pending = wait(
            futures,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        if pending:
            raise TimeoutError("recovery worker did not drain before close deadline")
        if self._failure is not None:
            raise self._failure
        try:
            for future in futures:
                future.result()
        finally:
            if not self._executors_shutdown:
                self._worker_executor.shutdown(wait=True, cancel_futures=False)
                self._native_executor.shutdown(wait=True, cancel_futures=False)
                self._executors_shutdown = True

    def _resolve_predecessor_reconcile_plan(
        self, running: RecoveryRunStatus
    ) -> "RecoveryPredecessorReconcilePlan | None":
        """B6.2/B6.3/B6.4: resolve the EXACT pending ancestry from persisted
        authoritative state.  Returns a bound plan ONLY when the IMMEDIATE
        predecessor is an EXACT PARTIAL/recovery_physical_reconciliation_pending
        record; then walks the persisted supersedes chain through consecutive such
        records (validating every link both ways, bounded and cycle-guarded) down
        to the deepest unresolved source.  Returns None so a normal resume from
        cancelled / rolled-back / retryable-failed never adopts unrelated old
        physical truth.  No epoch arithmetic drives the native op."""

        predecessor_epoch = running.supersedes_epoch
        if predecessor_epoch is None:
            # A true first attempt (epoch 1) legitimately has no predecessor; any
            # epoch > 1 with no supersedes metadata is corrupt running-chain state.
            if int(running.epoch) > 1:
                raise RecoveryPendingAncestryError(
                    "recovery_pending_ancestry_missing_supersedes"
                )
            return None
        if int(predecessor_epoch) < 1 or int(predecessor_epoch) >= int(running.epoch):
            raise RecoveryPendingAncestryError(
                "recovery_pending_ancestry_bad_ordering"
            )
        reader = getattr(self._store, "get_status_at_epoch", None)
        if not callable(reader):
            # An unavailable persisted exact-epoch reader is an anomaly, not a
            # normal resume — never silently fresh-recover.
            raise RecoveryPendingAncestryError(
                "recovery_pending_ancestry_reader_unavailable"
            )
        ancestry: list[tuple[int, str]] = []
        seen: set[int] = set()
        successor_epoch = int(running.epoch)
        cursor = int(predecessor_epoch)
        while True:
            if cursor in seen:
                raise RecoveryPendingAncestryError(
                    "recovery_pending_ancestry_cycle"
                )
            if len(ancestry) >= _MAX_PENDING_ANCESTRY_HOPS:
                raise RecoveryPendingAncestryError(
                    "recovery_pending_ancestry_over_bound"
                )
            seen.add(cursor)
            predecessor = reader(run_id=running.run_id, epoch=cursor)
            if predecessor is None:
                # A referenced epoch that does not exist is corruption, NOT a
                # normal resume — fail closed rather than fresh-recover.
                raise RecoveryPendingAncestryError(
                    "recovery_pending_ancestry_missing_row"
                )
            # Validate the exact persisted supersedes link both directions, even
            # for the first non-pending boundary.
            if (
                predecessor.epoch != cursor
                or predecessor.superseded_by_epoch != successor_epoch
                or predecessor.attempt_id
                != recovery_attempt_id(running.run_id, cursor)
            ):
                raise RecoveryPendingAncestryError(
                    "recovery_pending_ancestry_broken_link"
                )
            # The EXACT B6.1 pending sentinel: state + terminal outcome PARTIAL,
            # the reserved reason, retryable is False, and NO asserted physical
            # truth.  A retryable=True record with the reserved reason is NOT an
            # unresolved source and must never be bridged.
            is_pending = (
                predecessor.state is RecoveryRunState.PARTIAL
                and predecessor.terminal_outcome is RecoveryTerminalOutcome.PARTIAL
                and predecessor.reason_code == RECOVERY_PENDING_RECONCILIATION_REASON
                and predecessor.retryable is False
                and predecessor.physical_truth is None
            )
            if not is_pending:
                # A valid NON-pending boundary.  If it is the immediate predecessor
                # there is no plan (normal resume from cancelled / rolled-back /
                # retryable-failed); otherwise STOP SUCCESSFULLY with the pending
                # ancestry accumulated so far (the boundary is not part of it).
                if not ancestry:
                    return None
                break
            ancestry.append((cursor, predecessor.attempt_id))
            if predecessor.supersedes_epoch is None:
                break  # deepest unresolved pending record (the source); no boundary
            next_epoch = int(predecessor.supersedes_epoch)
            if next_epoch < 1 or next_epoch >= cursor:
                # Supersedes chain must strictly decrease toward epoch 1.
                raise RecoveryPendingAncestryError(
                    "recovery_pending_ancestry_bad_ordering"
                )
            successor_epoch = cursor
            cursor = next_epoch
        if not ancestry:
            return None
        return RecoveryPredecessorReconcilePlan(
            run_id=running.run_id,
            successor_epoch=int(running.epoch),
            ancestry=tuple(ancestry),
        )

    def _run_attempt(self, *, claim: RecoveryDispatchClaim) -> None:
        run_id = claim.run_id
        epoch = claim.epoch
        started_monotonic = self._monotonic_clock()
        running = self._store.get_status(run_id=run_id)
        if (
            running is None
            or running.epoch != epoch
            or running.attempt_id != claim.attempt_id
            or running.state is not RecoveryRunState.RUNNING
            or running.phase is not RecoveryRunPhase.CUTOVER
        ):
            return
        attempt_id = running.attempt_id
        baseline_elapsed_ms = running.active_elapsed_ms
        previous_attempts_ms = running.cumulative_active_ms - running.active_elapsed_ms
        active_cap_ms = min(
            running.attempt_budget_ms,
            max(
                0,
                MAX_RECOVERY_CUMULATIVE_BUDGET_MS - previous_attempts_ms,
            ),
        )
        remaining_active_ms = max(0, active_cap_ms - baseline_elapsed_ms)
        remaining_wall_ms = max(
            0,
            int(
                (running.active_deadline_at - claim.claimed_at).total_seconds() * 1_000
            ),
        )
        deadline_at_monotonic = started_monotonic + (
            min(remaining_active_ms, remaining_wall_ms) / 1_000
        )

        # B6.4: resolve the persisted pending ancestry BEFORE any native/physical
        # call.  A corrupt/anomalous chain is a typed fail-closed terminal with
        # ZERO native operation — never a silent fresh recover_and_cutover.
        if claim.reconciliation_only:
            predecessor_plan = None
        else:
            try:
                predecessor_plan = self._resolve_predecessor_reconcile_plan(running)
            except RecoveryPendingAncestryError as exc:
                self._complete(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    epoch=epoch,
                    started_monotonic=started_monotonic,
                    baseline_elapsed_ms=baseline_elapsed_ms,
                    result=RecoveryWorkerResult(
                        outcome=RecoveryTerminalOutcome.FAILED,
                        reason_code=exc.code,
                        retryable=False,
                        counts=running.counts,
                    ),
                    dispatch_id=claim.dispatch_id,
                    claim_token=claim.claim_token,
                    wall_started_at=claim.claimed_at,
                    deadline_at_monotonic=deadline_at_monotonic,
                )
                return

        # The native graph work uses a second pool. Copy again from the worker
        # context so settings/auth/runtime providers survive both executor hops.
        native_context = copy_context()
        native_kwargs: dict[str, object] = {
            "run_id": run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "fence_check": lambda: self._assert_fenced(
                run_id=run_id,
                attempt_id=attempt_id,
                epoch=epoch,
                dispatch_id=claim.dispatch_id,
                claim_token=claim.claim_token,
                wall_started_at=claim.claimed_at,
                started_monotonic=started_monotonic,
                deadline_at_monotonic=deadline_at_monotonic,
                allow_deadline=claim.reconciliation_only,
            ),
        }
        if claim.reconciliation_only:
            native_kwargs["reconcile_only"] = True
        elif predecessor_plan is not None:
            native_kwargs["predecessor_reconcile"] = predecessor_plan
        native_future = self._native_executor.submit(
            native_context.run,
            self._native_operation,
            **native_kwargs,
        )
        may_checkpoint = True
        result: RecoveryWorkerResult
        while True:
            try:
                result = native_future.result(
                    timeout=self._heartbeat_interval_ms / 1_000
                )
                if not isinstance(result, RecoveryWorkerResult):
                    raise TypeError("native_operation must return RecoveryWorkerResult")
                break
            except TimeoutError:
                if may_checkpoint and not claim.reconciliation_only:
                    may_checkpoint = self._heartbeat(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        epoch=epoch,
                        started_monotonic=started_monotonic,
                        baseline_elapsed_ms=baseline_elapsed_ms,
                        dispatch_id=claim.dispatch_id,
                        claim_token=claim.claim_token,
                        wall_started_at=claim.claimed_at,
                        deadline_at_monotonic=deadline_at_monotonic,
                    )
                continue
            except RecoveryWorkerFenceError as exc:
                current = self._store.get_status(run_id=run_id)
                if current is None or current.epoch != epoch:
                    return
                if exc.code == "cancel_requested":
                    result = RecoveryWorkerResult(
                        outcome=RecoveryTerminalOutcome.CANCELLED,
                        reason_code="recovery_cancelled",
                        retryable=False,
                        counts=current.counts,
                    )
                elif exc.code == "attempt_deadline_exhausted":
                    result = RecoveryWorkerResult(
                        outcome=RecoveryTerminalOutcome.TIMEOUT,
                        reason_code="recovery_attempt_budget_exhausted",
                        retryable=False,
                        counts=current.counts,
                    )
                else:
                    return
                break
            except RecoveryRuntimeConfigurationError as exc:
                current = self._store.get_status(run_id=run_id)
                if current is None or current.epoch != epoch:
                    return
                logger.warning(
                    "global discovery recovery worker adoption rejected "
                    "run=%s epoch=%d reason=%s",
                    run_id,
                    epoch,
                    exc.code,
                    extra={
                        "event": ("global_discovery_recovery.worker_adoption_rejected"),
                        "run_id": run_id,
                        "epoch": epoch,
                        "reason_code": exc.code,
                    },
                )
                result = RecoveryWorkerResult(
                    outcome=RecoveryTerminalOutcome.FAILED,
                    reason_code=exc.code,
                    retryable=False,
                    counts=RecoveryProgressCounts(
                        **{
                            **current.counts.to_dict(),
                            "errors": current.counts.errors + 1,
                        }
                    ),
                )
                break
            except GlobalDiscoveryWriterContention:
                # Another canonical writer (or a just-crashed recovery lease)
                # still owns the shared lane.  Keep the exact SQL attempt and
                # claim nonterminal; after claim expiry the poller retries the
                # same epoch instead of manufacturing a retry epoch.
                return
            except GlobalDiscoveryWriterFenceLost as exc:
                # B6 (Option A): the durable writer fence was lost DURING the
                # physical cutover — possibly AFTER the pointer already crossed.
                # This route must NEVER be hidden as FAILED/native_operation_failed
                # without physical truth (that would fabricate SQL truth while a
                # new physical generation may already be active and block same-run
                # reconciliation).  Terminalize epoch N as PARTIAL with the
                # registered reconciliation-pending reason and NO asserted physical
                # truth; the fence-lost type/code stays visible as the causal
                # evidence.  A real epoch N+1 owner reconciles the predecessor
                # journal/pointer (reconcile-before-mutate) and heals the truth.
                current = self._store.get_status(run_id=run_id)
                if current is None or current.epoch != epoch:
                    return
                logger.warning(
                    "global discovery recovery writer fence lost after physical "
                    "cutover boundary run=%s epoch=%d attempt=%s dispatch=%s "
                    "cause=%s",
                    run_id,
                    epoch,
                    attempt_id,
                    claim.dispatch_id,
                    getattr(exc, "code", type(exc).__name__),
                    extra={
                        "event": (
                            "global_discovery_recovery.writer_fence_lost_pending"
                        ),
                        "run_id": run_id,
                        "epoch": epoch,
                        "attempt_id": attempt_id,
                        "dispatch_id": claim.dispatch_id,
                        "reconciliation_only": claim.reconciliation_only,
                        "error_type": type(exc).__name__,
                        "cause_code": getattr(exc, "code", None),
                        "reason_code": "recovery_physical_reconciliation_pending",
                        "retryable": False,
                    },
                )
                result = RecoveryWorkerResult(
                    outcome=RecoveryTerminalOutcome.PARTIAL,
                    reason_code="recovery_physical_reconciliation_pending",
                    retryable=False,
                    counts=current.counts,
                    physical_truth=None,
                )
                break
            except RecoveryPendingAncestryError as exc:
                # B6.4: the native-operation defence-in-depth plan validation
                # rejected a corrupt/forged plan BEFORE any input/lease/physical
                # call.  Preserve the EXACT typed reason (never rewrite it to
                # native_operation_failed) and terminalize fail-closed.
                current = self._store.get_status(run_id=run_id)
                if current is None or current.epoch != epoch:
                    return
                result = RecoveryWorkerResult(
                    outcome=RecoveryTerminalOutcome.FAILED,
                    reason_code=exc.code,
                    retryable=False,
                    counts=current.counts,
                )
                break
            except Exception as exc:
                logger.exception(
                    "global discovery recovery native operation failed "
                    "run=%s epoch=%d attempt=%s dispatch=%s error_type=%s",
                    run_id,
                    epoch,
                    attempt_id,
                    claim.dispatch_id,
                    type(exc).__name__,
                    extra={
                        "event": ("global_discovery_recovery.native_operation_failed"),
                        "run_id": run_id,
                        "epoch": epoch,
                        "attempt_id": attempt_id,
                        "dispatch_id": claim.dispatch_id,
                        "reconciliation_only": claim.reconciliation_only,
                        "error_type": type(exc).__name__,
                        "reason_code": "native_operation_failed",
                        "retryable": True,
                    },
                )
                current = self._store.get_status(run_id=run_id)
                if current is None or current.epoch != epoch:
                    return
                result = RecoveryWorkerResult(
                    outcome=RecoveryTerminalOutcome.FAILED,
                    reason_code="native_operation_failed",
                    retryable=True,
                    counts=RecoveryProgressCounts(
                        **{
                            **current.counts.to_dict(),
                            "errors": current.counts.errors + 1,
                        }
                    ),
                )
                break

        self._complete(
            run_id=run_id,
            attempt_id=attempt_id,
            epoch=epoch,
            started_monotonic=started_monotonic,
            baseline_elapsed_ms=baseline_elapsed_ms,
            result=result,
            dispatch_id=claim.dispatch_id,
            claim_token=claim.claim_token,
            wall_started_at=claim.claimed_at,
            deadline_at_monotonic=deadline_at_monotonic,
        )

    def _heartbeat(
        self,
        *,
        run_id: str,
        attempt_id: str,
        epoch: int,
        started_monotonic: float,
        baseline_elapsed_ms: int,
        dispatch_id: str | None = None,
        claim_token: str | None = None,
        wall_started_at: datetime | None = None,
        deadline_at_monotonic: float | None = None,
    ) -> bool:
        current = self._store.get_status(run_id=run_id)
        if (
            current is None
            or current.epoch != epoch
            or current.attempt_id != str(attempt_id)
        ):
            return False
        if current.state is not RecoveryRunState.RUNNING:
            return False
        if current.cancel_requested_at is not None:
            return False
        observed_monotonic = self._monotonic_clock()
        monotonic_delta_ms = max(
            0,
            int((observed_monotonic - started_monotonic) * 1_000),
        )
        elapsed_ms = max(
            current.active_elapsed_ms,
            int(baseline_elapsed_ms) + monotonic_delta_ms,
        )
        previous_attempts_ms = current.cumulative_active_ms - current.active_elapsed_ms
        cumulative_allowance_ms = max(
            0,
            MAX_RECOVERY_CUMULATIVE_BUDGET_MS - previous_attempts_ms,
        )
        active_cap_ms = min(
            current.attempt_budget_ms,
            cumulative_allowance_ms,
        )
        deadline_reached = (
            deadline_at_monotonic is not None
            and observed_monotonic >= deadline_at_monotonic
        )
        if deadline_reached:
            elapsed_ms = max(elapsed_ms, active_cap_ms)
        bounded_elapsed_ms = min(elapsed_ms, active_cap_ms)
        observed_at = (
            self._wall_clock()
            if wall_started_at is None
            else wall_started_at + timedelta(milliseconds=monotonic_delta_ms)
        )
        heartbeat_at = max(current.heartbeat_at, observed_at)
        durable_heartbeat = getattr(self._store, "heartbeat_recovery", None)
        if (
            callable(durable_heartbeat)
            and dispatch_id is not None
            and claim_token is not None
        ):
            try:
                updated = durable_heartbeat(
                    dispatch_id=dispatch_id,
                    claim_token=claim_token,
                    observed_at=heartbeat_at,
                    requested_expires_at=heartbeat_at
                    + timedelta(milliseconds=RECOVERY_WORKER_LEASE_MS),
                    active_elapsed_ms=bounded_elapsed_ms,
                    counts=current.counts,
                )
                return bool(
                    updated.state is RecoveryRunState.RUNNING
                    and updated.cancel_requested_at is None
                )
            except (
                RecoveryCheckpointConflict,
                RecoveryDispatchClaimConflict,
                RecoveryRunNotFound,
            ):
                return False
        if elapsed_ms >= active_cap_ms or deadline_reached:
            reason_code = (
                "recovery_cumulative_budget_exhausted"
                if cumulative_allowance_ms < current.attempt_budget_ms
                else "recovery_attempt_budget_exhausted"
            )
            try:
                self._store.complete(
                    run_id=run_id,
                    epoch=epoch,
                    expected_progress_seq=current.progress_seq,
                    completed_at=max(current.updated_at, heartbeat_at),
                    active_elapsed_ms=active_cap_ms,
                    result=RecoveryWorkerResult(
                        outcome=RecoveryTerminalOutcome.TIMEOUT,
                        reason_code=reason_code,
                        retryable=False,
                        counts=current.counts,
                    ),
                )
            except RecoveryCheckpointConflict:
                pass
            return False
        checkpoint = RecoveryCheckpoint(
            run_id=run_id,
            epoch=epoch,
            expected_progress_seq=current.progress_seq,
            phase=current.phase,
            heartbeat_at=heartbeat_at,
            active_elapsed_ms=bounded_elapsed_ms,
            counts=current.counts,
        )
        try:
            self._store.compare_and_set_checkpoint(checkpoint)
            return True
        except RecoveryCheckpointConflict:
            refreshed = self._store.get_status(run_id=run_id)
            return bool(
                refreshed is not None
                and refreshed.epoch == epoch
                and refreshed.state is RecoveryRunState.RUNNING
                and refreshed.cancel_requested_at is None
            )

    def _assert_fenced(
        self,
        *,
        run_id: str,
        attempt_id: str,
        epoch: int,
        dispatch_id: str | None = None,
        claim_token: str | None = None,
        wall_started_at: datetime | None = None,
        started_monotonic: float | None = None,
        deadline_at_monotonic: float | None = None,
        allow_deadline: bool = False,
    ) -> None:
        current = self._store.get_status(run_id=run_id)
        if (
            current is None
            or current.epoch != epoch
            or current.attempt_id != str(attempt_id)
        ):
            raise RecoveryWorkerFenceError("stale_epoch")
        if current.cancel_requested_at is not None and not allow_deadline:
            raise RecoveryWorkerFenceError("cancel_requested")
        if current.state is not RecoveryRunState.RUNNING:
            raise RecoveryWorkerFenceError("attempt_not_running")
        observed_monotonic = self._monotonic_clock()
        if (
            not allow_deadline
            and deadline_at_monotonic is not None
            and observed_monotonic >= deadline_at_monotonic
        ):
            raise RecoveryWorkerFenceError("attempt_deadline_exhausted")
        assertion = getattr(self._store, "assert_recovery_dispatch_claim", None)
        if callable(assertion) and dispatch_id is not None and claim_token is not None:
            monotonic_delta_ms = (
                0
                if started_monotonic is None
                else max(
                    0,
                    int((observed_monotonic - started_monotonic) * 1_000),
                )
            )
            observed_at = (
                self._wall_clock()
                if wall_started_at is None
                else wall_started_at + timedelta(milliseconds=monotonic_delta_ms)
            )
            try:
                assertion(
                    dispatch_id=dispatch_id,
                    claim_token=claim_token,
                    observed_at=max(current.updated_at, observed_at),
                )
            except RecoveryDispatchClaimConflict as exc:
                raise RecoveryWorkerFenceError("stale_dispatch_claim") from exc

    def _complete(
        self,
        *,
        run_id: str,
        attempt_id: str,
        epoch: int,
        started_monotonic: float,
        baseline_elapsed_ms: int,
        result: RecoveryWorkerResult,
        dispatch_id: str | None = None,
        claim_token: str | None = None,
        wall_started_at: datetime | None = None,
        deadline_at_monotonic: float | None = None,
    ) -> None:
        for _ in range(8):
            current = self._store.get_status(run_id=run_id)
            if (
                current is None
                or current.epoch != epoch
                or current.attempt_id != str(attempt_id)
            ):
                return
            if current.state.is_terminal:
                return
            terminal = result
            # B6.1: the EXACT unknown post-pointer reconciliation-pending sentinel
            # is preserved through this worker-side override layer too, so a late
            # cancel/deadline cannot erase the unknown physical outcome before it
            # even reaches the SQL override layer.
            pending_truth = _is_pending_reconciliation_result(result)
            if (
                current.cancel_requested_at is not None
                and result.physical_truth is None
                and not pending_truth
            ):
                terminal = RecoveryWorkerResult(
                    outcome=RecoveryTerminalOutcome.CANCELLED,
                    reason_code="recovery_cancelled",
                    retryable=False,
                    counts=current.counts,
                )
            observed_monotonic = self._monotonic_clock()
            monotonic_delta_ms = max(
                0,
                int((observed_monotonic - started_monotonic) * 1_000),
            )
            requested_elapsed_ms = max(
                current.active_elapsed_ms,
                int(baseline_elapsed_ms) + monotonic_delta_ms,
            )
            previous_attempts_ms = (
                current.cumulative_active_ms - current.active_elapsed_ms
            )
            cumulative_allowance_ms = max(
                0,
                MAX_RECOVERY_CUMULATIVE_BUDGET_MS - previous_attempts_ms,
            )
            active_cap_ms = min(
                current.attempt_budget_ms,
                cumulative_allowance_ms,
            )
            deadline_reached = (
                deadline_at_monotonic is not None
                and observed_monotonic >= deadline_at_monotonic
            )
            if deadline_reached:
                requested_elapsed_ms = max(
                    requested_elapsed_ms,
                    active_cap_ms,
                )
            elapsed_ms = min(requested_elapsed_ms, active_cap_ms)
            if (
                terminal.physical_truth is None
                and (deadline_reached or requested_elapsed_ms >= active_cap_ms)
                and terminal.outcome is not RecoveryTerminalOutcome.CANCELLED
                and not pending_truth
            ):
                terminal = RecoveryWorkerResult(
                    outcome=RecoveryTerminalOutcome.TIMEOUT,
                    reason_code=(
                        "recovery_cumulative_budget_exhausted"
                        if cumulative_allowance_ms < current.attempt_budget_ms
                        else "recovery_attempt_budget_exhausted"
                    ),
                    retryable=False,
                    counts=current.counts,
                )
            completed_at = (
                self._wall_clock()
                if wall_started_at is None
                else wall_started_at + timedelta(milliseconds=monotonic_delta_ms)
            )
            try:
                durable_complete = getattr(
                    self._store,
                    "complete_recovery",
                    None,
                )
                if (
                    callable(durable_complete)
                    and dispatch_id is not None
                    and claim_token is not None
                ):
                    durable_complete(
                        dispatch_id=dispatch_id,
                        claim_token=claim_token,
                        expected_progress_seq=current.progress_seq,
                        completed_at=max(current.updated_at, completed_at),
                        active_elapsed_ms=elapsed_ms,
                        result=terminal,
                    )
                else:
                    self._store.complete(
                        run_id=run_id,
                        epoch=epoch,
                        expected_progress_seq=current.progress_seq,
                        completed_at=max(current.updated_at, completed_at),
                        active_elapsed_ms=elapsed_ms,
                        result=terminal,
                    )
                return
            except RecoveryCheckpointConflict:
                # A concurrent cancel/checkpoint won. Reread and preserve its
                # cancel request before attempting the terminal transition.
                continue
            except RecoveryDispatchClaimConflict:
                return
        raise RecoveryCheckpointConflict(
            code="recovery_progress_conflict",
            run_id=run_id,
            expected_epoch=epoch,
            actual_epoch=epoch,
            expected_progress_seq=current.progress_seq,
            actual_progress_seq=current.progress_seq + 1,
        )


@dataclass(slots=True)
class CommunityRecoveryRuntime:
    """Owned two-stage runtime with ordered durable/native drain."""

    store: SQLAlchemyRecoveryRunStore
    preparation_poller: CommunityRecoveryPreparationPoller
    recovery_worker: CommunityRecoveryWorker
    dispatcher: CommunityDurableRecoveryDispatcher
    control: RecoveryControlPlane

    @property
    def worker(self) -> CommunityRecoveryWorker:
        """Compatibility name for the runtime's physical worker."""

        return self.recovery_worker

    def start(self) -> None:
        self.preparation_poller.start()
        self.recovery_worker.start()

    def close(self, *, timeout_seconds: float = 10.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        failure: BaseException | None = None
        try:
            self.recovery_worker.close(
                timeout_seconds=max(0.0, deadline - time.monotonic())
            )
        except BaseException as exc:
            failure = exc
        try:
            self.preparation_poller.close(
                timeout_seconds=max(0.0, deadline - time.monotonic())
            )
        except BaseException as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            raise failure
        self.store.engine.dispose()


def build_community_recovery_runtime(
    *,
    database_url: str,
    recovery: GlobalDiscoveryRecovery,
    input_provider: RecoveryNativeInputProvider,
    prepared_revoker: PreparedRecoveryRevoker,
    preparation_operation: RecoveryPreparationOperation,
    heartbeat_interval_ms: int = 4_000,
) -> CommunityRecoveryRuntime:
    """Compose/start both durable stages after the schema lifecycle ran."""

    if not isinstance(recovery, GlobalDiscoveryRecovery):
        raise TypeError("recovery must implement GlobalDiscoveryRecovery")
    if not callable(input_provider):
        raise TypeError("input_provider must be callable")
    if not callable(preparation_operation):
        raise TypeError("preparation_operation must be callable")
    interval = int(heartbeat_interval_ms)
    if interval <= 0 or interval > RECOVERY_HEARTBEAT_INTERVAL_MS:
        raise ValueError("heartbeat_interval_ms must be in 1..5000")
    store = SQLAlchemyRecoveryRunStore(
        database_url=synchronous_recovery_database_url(database_url),
        prepared_revoker=prepared_revoker,
        resume_input_handoff=getattr(
            input_provider,
            "handoff_resume_inputs",
            None,
        ),
    )
    preparation_poller: CommunityRecoveryPreparationPoller | None = None
    recovery_worker: CommunityRecoveryWorker | None = None
    try:
        preparation_poller = CommunityRecoveryPreparationPoller(
            store=store,
            operation=preparation_operation,
            poll_interval_seconds=interval / 1_000,
            heartbeat_interval_ms=interval,
        )
        native_operation = CommunityGlobalDiscoveryRecoveryNativeOperation(
            recovery=recovery,
            input_provider=input_provider,
        )
        recovery_worker = CommunityRecoveryWorker(
            store=store,
            native_operation=native_operation,
            heartbeat_interval_ms=interval,
            poll_interval_seconds=interval / 1_000,
        )
        dispatcher = CommunityDurableRecoveryDispatcher(
            store=store,
            preparation_poller=preparation_poller,
            recovery_worker=recovery_worker,
        )
        control = RecoveryControlPlane(store=store, dispatcher=dispatcher)
        runtime = CommunityRecoveryRuntime(
            store=store,
            preparation_poller=preparation_poller,
            recovery_worker=recovery_worker,
            dispatcher=dispatcher,
            control=control,
        )
        runtime.start()
    except Exception:
        if recovery_worker is not None:
            with suppress(BaseException):
                recovery_worker.close(timeout_seconds=1.0)
        if preparation_poller is not None:
            with suppress(BaseException):
                preparation_poller.close(timeout_seconds=1.0)
        store.engine.dispose()
        raise
    return runtime


__all__ = [
    "CommunityDurableRecoveryInputProvider",
    "CommunityGlobalDiscoveryRecoveryNativeOperation",
    "CommunityDurableRecoveryDispatcher",
    "CommunityRecoveryPreparationPoller",
    "CommunityRecoveryRuntime",
    "CommunityRecoveryWorker",
    "RecoveryNativeInputProvider",
    "RecoveryNativeInputs",
    "RecoveryNativeOperation",
    "RecoveryPreparationError",
    "RecoveryPreparationFenceCheck",
    "RecoveryPreparationFenceError",
    "RecoveryPreparationOperation",
    "RecoveryPreparationRetryableError",
    "RecoveryPreparationTerminalError",
    "RecoveryRequesterAudit",
    "PreparedRecoveryRevoker",
    "RecoveryDispatchClaim",
    "RecoveryDispatchReceipt",
    "RecoveryDispatchStage",
    "RecoveryDispatchState",
    "RecoveryPreparedRevokerUnavailable",
    "RecoveryRuntimeConfigurationError",
    "RecoveryStoreSchemaError",
    "RecoveryStoreContractError",
    "RecoveryWorkerFenceError",
    "SQLAlchemyRecoveryRunStore",
    "build_community_recovery_runtime",
    "synchronous_recovery_database_url",
]
