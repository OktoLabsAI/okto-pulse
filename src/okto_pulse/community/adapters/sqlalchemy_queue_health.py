"""Community SQLAlchemy queue-health read adapter."""

from __future__ import annotations

from typing import Any

from sqlalchemy import distinct, func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ConsolidationDeadLetter,
    ConsolidationQueue,
    GlobalUpdateOutbox,
)
from okto_pulse.core.ports.queue_health import (
    ActiveQueueStorageSnapshot,
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
        )


__all__ = ["CommunitySqlAlchemyQueueHealthReader"]
