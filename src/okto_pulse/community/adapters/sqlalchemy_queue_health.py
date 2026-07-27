"""Community SQLAlchemy queue-health read adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, distinct, func, or_, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ConsolidationDeadLetter,
    ConsolidationQueue,
    GlobalUpdateOutbox,
)
from okto_pulse.core.ports.queue_health import (
    ActiveConsolidationWorkItemSnapshot,
    ActiveQueueStorageSnapshot,
    GlobalOutboxDeadLetterRowSnapshot,
    GlobalOutboxDeadLetterStorageSnapshot,
    QueueHealthStorageSnapshot,
)


class CommunitySqlAlchemyQueueHealthReader:
    async def health_snapshot(
        self, context: Any
    ) -> QueueHealthStorageSnapshot:
        queue_depth = await context.scalar(
            select(func.count()).where(ConsolidationQueue.status == "pending")
        )
        oldest = await context.scalar(
            select(func.min(ConsolidationQueue.triggered_at)).where(
                ConsolidationQueue.status == "pending"
            )
        )
        claimed = await context.scalar(
            select(func.count()).where(ConsolidationQueue.status == "claimed")
        )
        boards = await context.execute(
            select(distinct(ConsolidationQueue.board_id)).where(
                ConsolidationQueue.status == "claimed"
            )
        )
        dead_letter = await context.scalar(
            select(func.count()).select_from(ConsolidationDeadLetter)
        )
        return QueueHealthStorageSnapshot(
            queue_depth=int(queue_depth or 0),
            oldest_pending_at=oldest,
            claimed_count=int(claimed or 0),
            claimed_boards=tuple(
                sorted(value for value in boards.scalars().all() if value)
            ),
            dead_letter_count=int(dead_letter or 0),
        )

    async def active_snapshot(
        self,
        context: Any,
        *,
        board_id: str | None,
        active_statuses: tuple[str, ...],
        max_outbox_retries: int,
        dead_letter_retry_sentinel: int,
        now: datetime,
        stuck_before: datetime,
        item_limit: int,
    ) -> ActiveQueueStorageSnapshot:
        def queue_filters(*extra: Any) -> list[Any]:
            filters = list(extra)
            if board_id is not None:
                filters.append(ConsolidationQueue.board_id == board_id)
            return filters

        by_status: dict[str, int] = {}
        for status in active_statuses:
            count = await context.scalar(
                select(func.count()).where(
                    *queue_filters(ConsolidationQueue.status == status)
                )
            )
            by_status[status] = int(count or 0)
        category_rows = (
            await context.execute(
                select(ConsolidationQueue.artifact_type, func.count())
                .where(
                    *queue_filters(
                        ConsolidationQueue.status.in_(active_statuses)
                    )
                )
                .group_by(ConsolidationQueue.artifact_type)
            )
        ).all()
        oldest = await context.scalar(
            select(func.min(ConsolidationQueue.triggered_at)).where(
                *queue_filters(ConsolidationQueue.status.in_(active_statuses))
            )
        )
        pending = ConsolidationQueue.status == "pending"
        claimed = ConsolidationQueue.status == "claimed"
        retry_eligible = or_(
            ConsolidationQueue.next_retry_at.is_(None),
            ConsolidationQueue.next_retry_at <= now,
        )
        scheduled_retry = and_(
            pending,
            ConsolidationQueue.next_retry_at > now,
        )
        overdue_claim = and_(
            claimed,
            or_(
                ConsolidationQueue.claim_timeout_at <= now,
                and_(
                    ConsolidationQueue.claim_timeout_at.is_(None),
                    func.coalesce(
                        ConsolidationQueue.claimed_at,
                        ConsolidationQueue.triggered_at,
                    )
                    <= stuck_before,
                ),
            ),
        )
        ready_count = await context.scalar(
            select(func.count()).where(
                *queue_filters(pending, retry_eligible)
            )
        )
        scheduled_count = await context.scalar(
            select(func.count()).where(*queue_filters(scheduled_retry))
        )
        claimed_count = await context.scalar(
            select(func.count()).where(*queue_filters(claimed))
        )
        overdue_claimed_count = await context.scalar(
            select(func.count()).where(*queue_filters(overdue_claim))
        )
        ready_oldest = await context.scalar(
            select(func.min(ConsolidationQueue.triggered_at)).where(
                *queue_filters(pending, retry_eligible)
            )
        )
        overdue_claimed_oldest = await context.scalar(
            select(
                func.min(
                    func.coalesce(
                        ConsolidationQueue.claimed_at,
                        ConsolidationQueue.triggered_at,
                    )
                )
            ).where(*queue_filters(overdue_claim))
        )
        next_retry = await context.scalar(
            select(func.min(ConsolidationQueue.next_retry_at)).where(
                *queue_filters(scheduled_retry)
            )
        )
        work_kind_rows = (
            await context.execute(
                select(ConsolidationQueue.work_kind, func.count())
                .where(
                    *queue_filters(
                        ConsolidationQueue.status.in_(active_statuses)
                    )
                )
                .group_by(ConsolidationQueue.work_kind)
            )
        ).all()
        max_attempts = await context.scalar(
            select(func.max(ConsolidationQueue.attempts)).where(
                *queue_filters(
                    ConsolidationQueue.status.in_(active_statuses)
                )
            )
        )
        active_rows = []
        if item_limit > 0:
            active_rows = (
                (
                    await context.execute(
                        select(ConsolidationQueue)
                        .where(
                            *queue_filters(
                                ConsolidationQueue.status.in_(active_statuses)
                            )
                        )
                        .order_by(
                            ConsolidationQueue.triggered_at.asc(),
                            ConsolidationQueue.id.asc(),
                        )
                        .limit(item_limit)
                    )
                )
                .scalars()
                .all()
            )
        outbox_filters = [
            GlobalUpdateOutbox.processed_at.is_(None),
            GlobalUpdateOutbox.retry_count >= 0,
            GlobalUpdateOutbox.retry_count < max_outbox_retries,
            GlobalUpdateOutbox.retry_count != dead_letter_retry_sentinel,
        ]
        if board_id is not None:
            outbox_filters.append(GlobalUpdateOutbox.board_id == board_id)
        outbox_depth = await context.scalar(
            select(func.count()).where(*outbox_filters)
        )
        outbox_oldest = await context.scalar(
            select(func.min(GlobalUpdateOutbox.created_at)).where(*outbox_filters)
        )
        return ActiveQueueStorageSnapshot(
            consolidation_by_status=by_status,
            consolidation_by_category={
                str(category or "unknown"): int(count)
                for category, count in category_rows
            },
            consolidation_oldest_at=oldest,
            outbox_depth=int(outbox_depth or 0),
            outbox_oldest_at=outbox_oldest,
            consolidation_ready_count=int(ready_count or 0),
            consolidation_scheduled_retry_count=int(scheduled_count or 0),
            consolidation_claimed_count=int(claimed_count or 0),
            consolidation_overdue_claimed_count=int(
                overdue_claimed_count or 0
            ),
            consolidation_ready_oldest_at=ready_oldest,
            consolidation_overdue_claimed_oldest_at=overdue_claimed_oldest,
            consolidation_next_retry_at=next_retry,
            consolidation_by_work_kind={
                str(work_kind or "unknown"): int(count)
                for work_kind, count in work_kind_rows
            },
            consolidation_max_attempts=int(max_attempts or 0),
            consolidation_items=tuple(
                ActiveConsolidationWorkItemSnapshot(
                    queue_id=str(row.id),
                    status=str(row.status),
                    work_kind=str(row.work_kind),
                    artifact_type=str(row.artifact_type),
                    artifact_id=str(row.artifact_id),
                    attempts=int(row.attempts or 0),
                    triggered_at=row.triggered_at,
                    claimed_at=row.claimed_at,
                    claim_timeout_at=row.claim_timeout_at,
                    next_retry_at=row.next_retry_at,
                    last_error=row.last_error,
                )
                for row in active_rows
            ),
        )

    async def global_outbox_dead_letter_snapshot(
        self,
        context: Any,
        *,
        board_id: str | None,
        limit: int,
        max_outbox_retries: int,
        dead_letter_retry_sentinel: int,
    ) -> GlobalOutboxDeadLetterStorageSnapshot:
        filters = [
            GlobalUpdateOutbox.processed_at.is_(None),
            or_(
                GlobalUpdateOutbox.retry_count >= max_outbox_retries,
                GlobalUpdateOutbox.retry_count == dead_letter_retry_sentinel,
            ),
        ]
        if board_id is not None:
            filters.append(GlobalUpdateOutbox.board_id == board_id)

        total = await context.scalar(
            select(func.count()).select_from(GlobalUpdateOutbox).where(*filters)
        )
        oldest = await context.scalar(
            select(func.min(GlobalUpdateOutbox.created_at)).where(*filters)
        )
        rows = []
        if limit > 0:
            rows = (
                (
                    await context.execute(
                        select(GlobalUpdateOutbox)
                        .where(*filters)
                        .order_by(
                            GlobalUpdateOutbox.created_at.asc(),
                            GlobalUpdateOutbox.id.asc(),
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return GlobalOutboxDeadLetterStorageSnapshot(
            total_count=int(total or 0),
            oldest_created_at=oldest,
            rows=tuple(
                GlobalOutboxDeadLetterRowSnapshot(
                    event_id=str(row.event_id),
                    board_id=str(row.board_id),
                    event_type=str(row.event_type),
                    retry_count=int(row.retry_count),
                    created_at=row.created_at,
                    last_error=row.last_error,
                )
                for row in rows
            ),
        )


__all__ = ["CommunitySqlAlchemyQueueHealthReader"]
