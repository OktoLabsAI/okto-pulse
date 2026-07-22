"""Community SQLAlchemy persistence adapter for consolidation processing."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision,
    ArtifactDeletionTombstone,
    Board,
    CanonicalDebt,
    Card,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    Ideation,
    Refinement,
    Spec,
    Sprint,
    Story,
)
from okto_pulse.core.ports.consolidation import (
    ConsolidationPoisonRow,
    ConsolidationQueueRecord,
)
from okto_pulse.core.ports.reconcile_intent import (
    ReconcileIntentCreate,
    ReconcileIntentReceipt,
)
from okto_pulse.core.ports.tombstone import (
    DeletionTombstoneAdvance,
    DeletionTombstoneReceipt,
)
from okto_pulse.core.ports.takedown_telemetry import (
    TakedownState,
    TakedownTransition,
)
from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
    stage_takedown_transition,
)
from okto_pulse.core.ports.stale_sweep import (
    STALE_SWEEP_ARTIFACT_TYPE,
    STALE_SWEEP_CATCHUP_EPOCH,
    STALE_SWEEP_WORK_KIND,
    StaleSweepBatchRequest,
    StaleSweepClaimConflict,
    StaleSweepRescheduleRequest,
    StaleSweepRunAction,
    StaleSweepRunReceipt,
    StaleSweepScheduleReceipt,
    StaleSweepScheduleRequest,
)


_MODELS = {
    "story": Story,
    "ideation": Ideation,
    "refinement": Refinement,
    "spec": Spec,
    "sprint": Sprint,
    "card": Card,
    "amendment_hotfix_revision": AmendmentHotfixRevision,
}

_DELETION_INTENT_SCHEMA_VERSION = 1
_GOVERNED_DELETION_ARTIFACT_TYPES = frozenset(
    {"card", "spec", "ideation", "refinement"}
)


def _event_trigger_marker(event_id: str) -> str:
    """Keep queue trace markers within the legacy 100-character column."""

    if len(event_id) <= 100:
        return event_id
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"catchup:{digest}"


def _stale_sweep_payload(*, cursor: str, budget: int, attempt: int) -> dict[str, Any]:
    return {
        "cursor": cursor,
        "budget": budget,
        # Immutable zero-based catch-up epoch. Retry accounting lives in the
        # queue's ``attempts`` column so replay never changes synthetic IDs.
        "attempt": attempt,
    }


def _parse_stale_sweep_payload(payload: Any) -> tuple[str, int, int]:
    if not isinstance(payload, dict):
        raise RuntimeError("stale_sweep_payload_invalid")
    cursor = payload.get("cursor")
    budget = payload.get("budget")
    attempt = payload.get("attempt", 0)
    if (
        not isinstance(cursor, str)
        or isinstance(budget, bool)
        or not isinstance(budget, int)
        or budget < 1
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 0
        or not set(payload).issubset({"cursor", "budget", "attempt"})
    ):
        raise RuntimeError("stale_sweep_payload_invalid")
    return cursor, budget, attempt


def _validate_deletion_identity(
    *,
    board_id: str,
    artifact_type: str,
    artifact_id: str,
    delete_event_id: str,
) -> None:
    if (
        not board_id
        or artifact_type not in _GOVERNED_DELETION_ARTIFACT_TYPES
        or not artifact_id
        or not delete_event_id
        or len(delete_event_id) > 255
    ):
        raise ValueError("governed_deletion_identity_invalid")


def _queue_record(row: Any) -> ConsolidationQueueRecord:
    return ConsolidationQueueRecord(
        id=str(row.id),
        board_id=str(row.board_id),
        artifact_type=str(row.artifact_type),
        artifact_id=str(row.artifact_id),
        status=str(row.status),
        attempts=int(row.attempts or 0),
        last_error=row.last_error,
        next_retry_at=row.next_retry_at,
        claimed_at=row.claimed_at,
        claim_timeout_at=row.claim_timeout_at,
        worker_id=row.worker_id,
        claimed_by_session_id=row.claimed_by_session_id,
        triggered_at=row.triggered_at,
        priority=str(getattr(row.priority, "value", row.priority)),
        work_kind=str(row.work_kind),
        generation=int(row.generation or 0),
        payload=row.payload,
        delete_event_id=row.delete_event_id,
        claim_token=row.claim_token,
    )


async def _stage_intent_created_transition(
    context: Any,
    request: ReconcileIntentCreate,
    *,
    occurred_at: Any,
) -> None:
    await stage_takedown_transition(
        context,
        TakedownTransition(
            delete_event_id=request.delete_event_id,
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            generation=request.generation,
            state=TakedownState.INTENT_CREATED,
            occurred_at=occurred_at,
            details={
                "source": (
                    "stale_sweep_catchup"
                    if request.delete_event_id.startswith("catchup:")
                    else "governed_delete"
                )
            },
        ),
    )


def _apply_queue(row: Any, record: ConsolidationQueueRecord) -> None:
    for field_name in (
        "status", "attempts", "last_error", "next_retry_at", "claimed_at",
        "claim_timeout_at", "worker_id", "claimed_by_session_id", "claim_token",
    ):
        setattr(row, field_name, getattr(record, field_name))


class CommunitySqlAlchemyConsolidationPersistence:
    async def load_artifact(
        self, context: Any, *, artifact_type: str, artifact_id: str
    ) -> Any | None:
        model = _MODELS.get(artifact_type)
        if model is None:
            return None
        statement = select(model).where(model.id == artifact_id)
        if artifact_type == "ideation":
            statement = statement.options(selectinload(Ideation.story_links))
        elif artifact_type == "spec":
            statement = statement.options(selectinload(Spec.architecture_designs))
        elif artifact_type == "sprint":
            statement = statement.options(selectinload(Sprint.spec))
        elif artifact_type == "card":
            statement = statement.options(selectinload(Card.architecture_designs))
        return (await context.execute(statement)).scalars().first()

    async def list_artifacts(
        self,
        context: Any,
        *,
        artifact_type: str,
        artifact_ids: Sequence[str],
        board_id: str | None = None,
    ) -> tuple[Any, ...]:
        model = _MODELS.get(artifact_type)
        if model is None or not artifact_ids:
            return ()
        statement = select(model).where(model.id.in_(tuple(artifact_ids)))
        if board_id is not None and hasattr(model, "board_id"):
            statement = statement.where(model.board_id == board_id)
        return tuple((await context.execute(statement)).scalars().all())

    async def list_stale_claims(
        self, context: Any, *, now, legacy_cutoff
    ) -> tuple[ConsolidationQueueRecord, ...]:
        rows = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.status == "claimed",
                    or_(
                        # Rows claimed before the claim-token migration cannot
                        # prove ownership and must be recovered immediately.
                        ConsolidationQueue.claim_token.is_(None),
                        (
                            ConsolidationQueue.claim_timeout_at.is_not(None)
                            & (ConsolidationQueue.claim_timeout_at < now)
                        ),
                        (
                            ConsolidationQueue.claim_timeout_at.is_(None)
                            & ConsolidationQueue.claimed_at.is_not(None)
                            & (ConsolidationQueue.claimed_at < legacy_cutoff)
                        ),
                    ),
                )
            )
        ).scalars().all()
        return tuple(_queue_record(row) for row in rows)

    async def count_pending(self, context: Any) -> int:
        value = await context.scalar(
            select(func.count()).where(ConsolidationQueue.status == "pending")
        )
        return int(value or 0)

    async def list_claimed_board_ids(self, context: Any) -> frozenset[str]:
        rows = (
            await context.execute(
                select(ConsolidationQueue.board_id).where(
                    ConsolidationQueue.status == "claimed"
                )
            )
        ).scalars().all()
        return frozenset(str(value) for value in rows)

    async def list_ready_pending(
        self, context: Any, *, now
    ) -> tuple[ConsolidationQueueRecord, ...]:
        rows = (
            await context.execute(
                select(ConsolidationQueue)
                .where(
                    ConsolidationQueue.status == "pending",
                    or_(
                        ConsolidationQueue.next_retry_at.is_(None),
                        ConsolidationQueue.next_retry_at <= now,
                    ),
                )
                .order_by(
                    ConsolidationQueue.priority.asc(),
                    ConsolidationQueue.triggered_at.asc(),
                )
            )
        ).scalars().all()
        return tuple(_queue_record(row) for row in rows)

    async def get_queue_entry(
        self, context: Any, *, entry_id: str
    ) -> ConsolidationQueueRecord | None:
        row = await context.get(ConsolidationQueue, entry_id)
        return _queue_record(row) if row is not None else None

    async def queue_claim_is_current_and_unfenced(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        work_kind: str,
        generation: int,
        delete_event_id: str | None,
    ) -> bool:
        """Atomically re-check claim ownership and its deletion fence."""

        if not entry_id or not claim_token:
            return False

        claim_predicates = (
            ConsolidationQueue.id == entry_id,
            ConsolidationQueue.status == "claimed",
            ConsolidationQueue.claim_token == claim_token,
            ConsolidationQueue.board_id == board_id,
            ConsolidationQueue.artifact_type == artifact_type,
            ConsolidationQueue.artifact_id == artifact_id,
            ConsolidationQueue.work_kind == work_kind,
            ConsolidationQueue.generation == generation,
            (
                ConsolidationQueue.delete_event_id.is_(None)
                if delete_event_id is None
                else ConsolidationQueue.delete_event_id == delete_event_id
            ),
        )
        tombstone_key = (
            ArtifactDeletionTombstone.board_id == board_id,
            ArtifactDeletionTombstone.artifact_type == artifact_type,
            ArtifactDeletionTombstone.artifact_id == artifact_id,
        )

        if work_kind == "consolidate":
            if generation != 0 or delete_event_id is not None:
                return False
            deletion_fence = ~exists(select(1).where(*tombstone_key))
        elif work_kind == "stale_reconcile":
            if generation < 1 or delete_event_id is None:
                return False
            deletion_fence = exists(
                select(1).where(
                    *tombstone_key,
                    ArtifactDeletionTombstone.generation == generation,
                    ArtifactDeletionTombstone.delete_event_id == delete_event_id,
                )
            )
        elif work_kind == STALE_SWEEP_WORK_KIND:
            if (
                artifact_type != STALE_SWEEP_ARTIFACT_TYPE
                or artifact_id != board_id
                or generation != 0
                or delete_event_id is not None
            ):
                return False
            deletion_fence = exists(select(1).where(Board.id == board_id))
        else:
            return False

        # A read-only SELECT is not a sufficient linearization point in WAL
        # mode: a governed delete could commit after the read and before the
        # external graph mutation.  This conditional no-op UPDATE acquires the
        # SQLite writer slot, evaluates claim + tombstone in that same write
        # statement, and keeps the delete UoW serialized until the worker ACK
        # commits.  A concurrent delete that already won makes the predicate
        # return no row (or the write upgrade fail), both fail-closed outcomes.
        statement = (
            update(ConsolidationQueue)
            .where(*claim_predicates, deletion_fence)
            .values(claim_token=ConsolidationQueue.claim_token)
            .returning(ConsolidationQueue.id)
            .execution_options(synchronize_session=False)
        )
        matched = (await context.execute(statement)).scalar_one_or_none()
        return matched is not None

    async def ack_claimed_queue_entry(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        generation: int,
        delete_event_id: str | None,
    ) -> bool:
        """Delete exactly the generation still owned by ``claim_token``."""

        if not entry_id or not claim_token:
            return False
        delete_event_predicate = (
            ConsolidationQueue.delete_event_id.is_(None)
            if delete_event_id is None
            else ConsolidationQueue.delete_event_id == delete_event_id
        )
        result = await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.id == entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == claim_token,
                ConsolidationQueue.generation == generation,
                delete_event_predicate,
            )
        )
        return int(result.rowcount or 0) == 1

    async def save_queue_entries(
        self, context: Any, entries: Sequence[ConsolidationQueueRecord]
    ) -> None:
        for entry in entries:
            row = await context.get(ConsolidationQueue, entry.id)
            if row is not None:
                _apply_queue(row, entry)
        await context.flush()

    async def delete_queue_entry(self, context: Any, *, entry_id: str) -> None:
        row = await context.get(ConsolidationQueue, entry_id)
        if row is not None:
            await context.delete(row)
            await context.flush()

    async def discard_artifact_work(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
    ) -> None:
        """Remove operational rows made obsolete by a governed hard delete."""

        await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.artifact_type == artifact_type,
                ConsolidationQueue.artifact_id == artifact_id,
                ConsolidationQueue.work_kind == "consolidate",
            )
        )
        for model in (ConsolidationDeadLetter, CanonicalDebt):
            await context.execute(
                delete(model).where(
                    model.board_id == board_id,
                    model.artifact_type == artifact_type,
                    model.artifact_id == artifact_id,
                )
            )
        await context.flush()

    async def advance_deletion_tombstone(
        self,
        context: Any,
        request: DeletionTombstoneAdvance,
    ) -> DeletionTombstoneReceipt:
        """Atomically create or advance the artifact's permanent fence."""

        _validate_deletion_identity(
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            delete_event_id=request.delete_event_id,
        )
        statement = (
            sqlite_insert(ArtifactDeletionTombstone)
            .values(
                id=str(uuid.uuid4()),
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=1,
                delete_event_id=request.delete_event_id,
            )
            .on_conflict_do_update(
                index_elements=["board_id", "artifact_type", "artifact_id"],
                set_={
                    "generation": case(
                        (
                            ArtifactDeletionTombstone.delete_event_id
                            == request.delete_event_id,
                            ArtifactDeletionTombstone.generation,
                        ),
                        else_=ArtifactDeletionTombstone.generation + 1,
                    ),
                    "delete_event_id": request.delete_event_id,
                    "updated_at": func.now(),
                },
            )
            .returning(
                ArtifactDeletionTombstone.generation,
                ArtifactDeletionTombstone.delete_event_id,
            )
        )
        try:
            row = (await context.execute(statement)).one()
        except IntegrityError as exc:
            # ``delete_event_id`` is globally unique.  Reusing it for a
            # different artifact is divergent history, never a replay.
            raise RuntimeError(
                "artifact_deletion_tombstone_delete_event_conflict"
            ) from exc
        await context.flush()
        return DeletionTombstoneReceipt(
            generation=int(row.generation),
            delete_event_id=str(row.delete_event_id),
        )

    async def persist_reconcile_intent(
        self,
        context: Any,
        request: ReconcileIntentCreate,
    ) -> ReconcileIntentReceipt:
        """Insert or replay the immutable stale-reconcile queue intent."""

        _validate_deletion_identity(
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            delete_event_id=request.delete_event_id,
        )
        source_refs = tuple(str(ref) for ref in request.source_refs)
        expected_refs = (f"{request.artifact_type}:{request.artifact_id}",)
        if request.generation < 1 or source_refs != expected_refs:
            raise ValueError("governed_reconcile_intent_invalid")
        if request.occurred_at is not None and not isinstance(
            request.occurred_at,
            datetime,
        ):
            raise ValueError("governed_reconcile_intent_occurred_at_invalid")

        tombstone = (
            await context.execute(
                select(ArtifactDeletionTombstone).where(
                    ArtifactDeletionTombstone.board_id == request.board_id,
                    ArtifactDeletionTombstone.artifact_type
                    == request.artifact_type,
                    ArtifactDeletionTombstone.artifact_id == request.artifact_id,
                )
            )
        ).scalar_one_or_none()
        if (
            tombstone is None
            or int(tombstone.generation) != request.generation
            or str(tombstone.delete_event_id) != request.delete_event_id
        ):
            raise RuntimeError("governed_reconcile_intent_tombstone_mismatch")

        payload = {
            "schema_version": _DELETION_INTENT_SCHEMA_VERSION,
            "delete_event_id": request.delete_event_id,
            "source_refs": list(source_refs),
        }
        intent_id = str(uuid.uuid4())
        intent_values: dict[str, object] = {
            "id": intent_id,
            "board_id": request.board_id,
            "artifact_type": request.artifact_type,
            "artifact_id": request.artifact_id,
            "work_kind": "stale_reconcile",
            "generation": request.generation,
            "payload": payload,
            "delete_event_id": request.delete_event_id,
            "priority": "high",
            "source": "governed_delete",
            "status": "pending",
            "triggered_by_event": _event_trigger_marker(
                request.delete_event_id
            ),
        }
        if request.occurred_at is not None:
            intent_values["triggered_at"] = request.occurred_at
        statement = (
            sqlite_insert(ConsolidationQueue)
            .values(**intent_values)
            .on_conflict_do_nothing(
                index_elements=[
                    "board_id",
                    "artifact_type",
                    "artifact_id",
                    "work_kind",
                    "generation",
                ],
                index_where=ConsolidationQueue.work_kind == "stale_reconcile",
            )
            .returning(
                ConsolidationQueue.id,
                ConsolidationQueue.generation,
                ConsolidationQueue.delete_event_id,
                ConsolidationQueue.triggered_at,
            )
        )
        inserted = (await context.execute(statement)).first()
        if inserted is not None:
            await _stage_intent_created_transition(
                context,
                request,
                occurred_at=inserted.triggered_at,
            )
            await context.flush()
            return ReconcileIntentReceipt(
                intent_id=str(inserted.id),
                generation=int(inserted.generation),
                delete_event_id=str(inserted.delete_event_id),
                created=True,
            )

        existing = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.board_id == request.board_id,
                    ConsolidationQueue.artifact_type == request.artifact_type,
                    ConsolidationQueue.artifact_id == request.artifact_id,
                    ConsolidationQueue.work_kind == "stale_reconcile",
                    ConsolidationQueue.generation == request.generation,
                )
            )
        ).scalar_one_or_none()
        if (
            existing is None
            or str(existing.delete_event_id) != request.delete_event_id
            or existing.payload != payload
        ):
            raise RuntimeError("governed_reconcile_intent_replay_conflict")
        await _stage_intent_created_transition(
            context,
            request,
            occurred_at=existing.triggered_at,
        )
        return ReconcileIntentReceipt(
            intent_id=str(existing.id),
            generation=int(existing.generation),
            delete_event_id=str(existing.delete_event_id),
            created=False,
        )

    async def schedule_stale_sweep(
        self,
        context: Any,
        request: StaleSweepScheduleRequest,
    ) -> StaleSweepScheduleReceipt:
        """Insert one low-priority coordinator without resetting active work."""

        board_present = await context.scalar(
            select(exists(select(1).where(Board.id == request.board_id)))
        )
        if not bool(board_present):
            return StaleSweepScheduleReceipt(
                board_id=request.board_id,
                sweep_id=None,
                scheduled=False,
                board_present=False,
                cursor="",
                budget=request.budget,
                attempt=0,
            )

        sweep_id = str(uuid.uuid4())
        payload = _stale_sweep_payload(
            cursor="",
            budget=request.budget,
            attempt=0,
        )
        inserted = (
            await context.execute(
                sqlite_insert(ConsolidationQueue)
                .values(
                    id=sweep_id,
                    board_id=request.board_id,
                    artifact_type=STALE_SWEEP_ARTIFACT_TYPE,
                    artifact_id=request.board_id,
                    work_kind=STALE_SWEEP_WORK_KIND,
                    generation=0,
                    payload=payload,
                    delete_event_id=None,
                    priority="low",
                    source="kg_tick",
                    status="pending",
                    triggered_at=request.now,
                    triggered_by_event=f"stale-sweep:{sweep_id}",
                )
                .on_conflict_do_nothing(
                    index_elements=["board_id", "work_kind"],
                    index_where=(
                        ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND
                    ),
                )
                .returning(ConsolidationQueue.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            await context.flush()
            return StaleSweepScheduleReceipt(
                board_id=request.board_id,
                sweep_id=str(inserted),
                scheduled=True,
                board_present=True,
                cursor="",
                budget=request.budget,
                attempt=0,
            )

        existing = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.board_id == request.board_id,
                    ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise RuntimeError("stale_sweep_schedule_conflict_without_row")
        cursor, budget, attempt = _parse_stale_sweep_payload(existing.payload)
        return StaleSweepScheduleReceipt(
            board_id=request.board_id,
            sweep_id=str(existing.id),
            scheduled=False,
            board_present=True,
            cursor=cursor,
            budget=budget,
            attempt=attempt,
        )

    async def _lock_stale_sweep_claim(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        expected_cursor: str,
        expected_budget: int,
        expected_attempt: int,
    ) -> Any:
        """Acquire SQLite's writer slot and validate the exact checkpoint."""

        matched = (
            await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == claim_token,
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.artifact_type == STALE_SWEEP_ARTIFACT_TYPE,
                    ConsolidationQueue.artifact_id == board_id,
                    ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND,
                    ConsolidationQueue.generation == 0,
                    ConsolidationQueue.delete_event_id.is_(None),
                )
                .values(claim_token=ConsolidationQueue.claim_token)
                .returning(ConsolidationQueue.id)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if matched is None:
            raise StaleSweepClaimConflict(
                f"stale_sweep_claim_lost entry_id={entry_id}"
            )
        row = (
            await context.execute(
                select(ConsolidationQueue).where(ConsolidationQueue.id == entry_id)
            )
        ).scalar_one()
        cursor, budget, attempt = _parse_stale_sweep_payload(row.payload)
        if (
            cursor != expected_cursor
            or budget != expected_budget
            or attempt != expected_attempt
        ):
            raise StaleSweepClaimConflict(
                f"stale_sweep_checkpoint_changed entry_id={entry_id}"
            )
        return row

    async def _ensure_catchup_tombstone(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        synthetic_event_id: str,
    ) -> DeletionTombstoneReceipt:
        """Insert generation one once, preserving any real tombstone."""

        inserted = (
            await context.execute(
                sqlite_insert(ArtifactDeletionTombstone)
                .values(
                    id=str(uuid.uuid4()),
                    board_id=board_id,
                    artifact_type=artifact_type,
                    artifact_id=artifact_id,
                    generation=1,
                    delete_event_id=synthetic_event_id,
                )
                .on_conflict_do_nothing()
                .returning(
                    ArtifactDeletionTombstone.generation,
                    ArtifactDeletionTombstone.delete_event_id,
                )
            )
        ).first()
        if inserted is not None:
            return DeletionTombstoneReceipt(
                generation=int(inserted.generation),
                delete_event_id=str(inserted.delete_event_id),
            )
        existing = (
            await context.execute(
                select(ArtifactDeletionTombstone).where(
                    ArtifactDeletionTombstone.board_id == board_id,
                    ArtifactDeletionTombstone.artifact_type == artifact_type,
                    ArtifactDeletionTombstone.artifact_id == artifact_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            # The deterministic event key collided with a different artifact.
            raise RuntimeError("stale_sweep_synthetic_event_identity_conflict")
        return DeletionTombstoneReceipt(
            generation=int(existing.generation),
            delete_event_id=str(existing.delete_event_id),
        )

    async def stage_stale_sweep_batch(
        self,
        context: Any,
        request: StaleSweepBatchRequest,
    ) -> StaleSweepRunReceipt:
        """Stage identities/intents before the exact checkpoint CAS."""

        await self._lock_stale_sweep_claim(
            context,
            entry_id=request.entry_id,
            claim_token=request.claim_token,
            board_id=request.board_id,
            expected_cursor=request.cursor,
            expected_budget=request.budget,
            expected_attempt=request.attempt,
        )

        ensured = 0
        for candidate in request.candidates:
            model = _MODELS[candidate.artifact_type]
            source_exists = await context.scalar(
                select(
                    exists(
                        select(1).where(
                            model.id == candidate.artifact_id,
                            model.board_id == request.board_id,
                        )
                    )
                )
            )
            # Close snapshot->checkpoint TOCTOU: a recreated/live source must
            # never receive a synthetic deletion fence.
            if bool(source_exists):
                continue
            synthetic_event_id = candidate.synthetic_delete_event_id(
                board_id=request.board_id,
                # Catch-up identity is independent from queue retry accounting.
                # ``attempt`` remains in the durable coordinator payload for
                # observability/forward compatibility, never as an ID input.
                epoch=STALE_SWEEP_CATCHUP_EPOCH,
            )
            tombstone = await self._ensure_catchup_tombstone(
                context,
                board_id=request.board_id,
                artifact_type=candidate.artifact_type,
                artifact_id=candidate.artifact_id,
                synthetic_event_id=synthetic_event_id,
            )
            intent = await self.persist_reconcile_intent(
                context,
                ReconcileIntentCreate(
                    board_id=request.board_id,
                    artifact_type=candidate.artifact_type,
                    artifact_id=candidate.artifact_id,
                    generation=tombstone.generation,
                    delete_event_id=tombstone.delete_event_id,
                    source_refs=(candidate.source_ref,),
                    occurred_at=request.now,
                ),
            )
            if intent.created:
                ensured += 1

        if request.has_more:
            checkpoint = await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == request.entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == request.claim_token,
                )
                .values(
                    status="pending",
                    payload=_stale_sweep_payload(
                        cursor=request.next_cursor,
                        budget=request.budget,
                        attempt=request.attempt,
                    ),
                    attempts=0,
                    last_error=None,
                    next_retry_at=None,
                    claimed_at=None,
                    claim_timeout_at=None,
                    worker_id=None,
                    claimed_by_session_id=None,
                    claim_token=None,
                    triggered_at=request.now,
                )
                .execution_options(synchronize_session=False)
            )
            if int(checkpoint.rowcount or 0) != 1:
                raise StaleSweepClaimConflict(
                    f"stale_sweep_checkpoint_cas_lost entry_id={request.entry_id}"
                )
            action = StaleSweepRunAction.ADVANCED
        else:
            completed = await context.execute(
                delete(ConsolidationQueue).where(
                    ConsolidationQueue.id == request.entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == request.claim_token,
                )
            )
            if int(completed.rowcount or 0) != 1:
                raise StaleSweepClaimConflict(
                    f"stale_sweep_complete_cas_lost entry_id={request.entry_id}"
                )
            action = StaleSweepRunAction.COMPLETED
        await context.flush()
        return StaleSweepRunReceipt(
            entry_id=request.entry_id,
            board_id=request.board_id,
            action=action,
            cursor=request.next_cursor,
            budget=request.budget,
            attempt=request.attempt,
            enqueued=ensured,
            has_more=request.has_more,
        )

    async def reschedule_stale_sweep(
        self,
        context: Any,
        request: StaleSweepRescheduleRequest,
    ) -> StaleSweepRunReceipt:
        """Preserve cursor/epoch and defer degraded work without legacy DLQ."""

        await self._lock_stale_sweep_claim(
            context,
            entry_id=request.entry_id,
            claim_token=request.claim_token,
            board_id=request.board_id,
            expected_cursor=request.cursor,
            expected_budget=request.budget,
            expected_attempt=request.attempt,
        )
        result = await context.execute(
            update(ConsolidationQueue)
            .where(
                ConsolidationQueue.id == request.entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == request.claim_token,
            )
            .values(
                status="pending",
                payload=_stale_sweep_payload(
                    cursor=request.cursor,
                    budget=request.budget,
                    attempt=request.attempt,
                ),
                attempts=ConsolidationQueue.attempts + 1,
                last_error=request.reason,
                next_retry_at=request.retry_at,
                claimed_at=None,
                claim_timeout_at=None,
                worker_id=None,
                claimed_by_session_id=None,
                claim_token=None,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            raise StaleSweepClaimConflict(
                f"stale_sweep_reschedule_cas_lost entry_id={request.entry_id}"
            )
        await context.flush()
        return StaleSweepRunReceipt(
            entry_id=request.entry_id,
            board_id=request.board_id,
            action=StaleSweepRunAction.RESCHEDULED,
            cursor=request.cursor,
            budget=request.budget,
            attempt=request.attempt,
            enqueued=0,
            has_more=True,
            reason=request.reason,
        )

    async def board_exists(self, context: Any, *, board_id: str) -> bool:
        return await context.get(Board, board_id) is not None

    async def list_dlq_auto_drain_board_ids(self, context: Any) -> tuple[str, ...]:
        rows = (await context.execute(select(Board))).scalars().all()
        return tuple(
            str(row.id)
            for row in rows
            if isinstance(row.settings, dict)
            and row.settings.get("dlq_auto_drain_enabled")
        )

    async def count_dead_letters(self, context: Any, *, board_id: str) -> int:
        value = await context.scalar(
            select(func.count()).where(ConsolidationDeadLetter.board_id == board_id)
        )
        return int(value or 0)

    async def delete_poison_dead_letters(
        self, context: Any, *, board_id: str, max_attempts: int
    ) -> tuple[ConsolidationPoisonRow, ...]:
        rows = (
            await context.execute(
                select(ConsolidationDeadLetter).where(
                    ConsolidationDeadLetter.board_id == board_id,
                    ConsolidationDeadLetter.attempts >= max_attempts,
                )
            )
        ).scalars().all()
        result = tuple(
            ConsolidationPoisonRow(id=str(row.id), attempts=int(row.attempts))
            for row in rows
        )
        for row in rows:
            await context.delete(row)
        if rows:
            await context.flush()
        return result

    async def commit(self, context: Any) -> None:
        await context.commit()

    async def rollback(self, context: Any) -> None:
        await context.rollback()


__all__ = ["CommunitySqlAlchemyConsolidationPersistence"]
