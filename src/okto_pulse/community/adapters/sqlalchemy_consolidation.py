"""Community SQLAlchemy persistence adapter for consolidation processing."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision,
    Board,
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


_MODELS = {
    "story": Story,
    "ideation": Ideation,
    "refinement": Refinement,
    "spec": Spec,
    "sprint": Sprint,
    "card": Card,
    "amendment_hotfix_revision": AmendmentHotfixRevision,
}


def _queue_record(row: Any) -> ConsolidationQueueRecord:
    return ConsolidationQueueRecord(
        id=str(row.id), board_id=str(row.board_id),
        artifact_type=str(row.artifact_type), artifact_id=str(row.artifact_id),
        status=str(row.status), attempts=int(row.attempts or 0),
        last_error=row.last_error, next_retry_at=row.next_retry_at,
        claimed_at=row.claimed_at, claim_timeout_at=row.claim_timeout_at,
        worker_id=row.worker_id, claimed_by_session_id=row.claimed_by_session_id,
        triggered_at=row.triggered_at,
        priority=str(getattr(row.priority, "value", row.priority)),
    )


def _apply_queue(row: Any, record: ConsolidationQueueRecord) -> None:
    for field_name in (
        "status", "attempts", "last_error", "next_retry_at", "claimed_at",
        "claim_timeout_at", "worker_id", "claimed_by_session_id",
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
