"""Community SQLAlchemy KG health read adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import case, func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    CanonicalDebt,
    ConsolidationAudit,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    DomainEventHandlerExecution,
    DomainEventRow,
    KGTickRun,
    KuzuNodeRef,
)
from okto_pulse.core.ports.kg_health import (
    KGHealthQueueSnapshot,
    KGTickRunFact,
)

_POLICY_CONSTRAINT_HANDLER = "PolicyConstraintProjectionHandler"


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CommunitySqlAlchemyKGHealthReader:
    async def queue_snapshot(
        self, context: Any, *, board_id: str
    ) -> KGHealthQueueSnapshot:
        active = (
            ConsolidationQueue.board_id == board_id,
            ConsolidationQueue.status.in_(("pending", "claimed")),
        )
        queue_depth = await context.scalar(select(func.count()).where(*active))
        oldest = await context.scalar(
            select(func.min(ConsolidationQueue.triggered_at)).where(*active)
        )
        dead_letters = await context.scalar(
            select(func.count()).where(ConsolidationDeadLetter.board_id == board_id)
        )
        execution = DomainEventHandlerExecution
        event = DomainEventRow
        policy_statuses = ("pending", "processing", "dlq")
        policy_row = (
            await context.execute(
                select(
                    func.sum(
                        case(
                            (
                                (
                                    (execution.status == "pending")
                                    & execution.next_attempt_at.is_(None)
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(
                        case(
                            ((execution.status == "processing"), 1),
                            else_=0,
                        )
                    ),
                    func.sum(
                        case(
                            (
                                (
                                    (execution.status == "pending")
                                    & execution.next_attempt_at.is_not(None)
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(
                        case(
                            ((execution.status == "dlq"), 1),
                            else_=0,
                        )
                    ),
                    func.max(execution.attempts),
                    func.min(
                        case(
                            (
                                (
                                    (execution.status == "pending")
                                    & execution.next_attempt_at.is_(None)
                                ),
                                event.occurred_at,
                            ),
                            else_=None,
                        )
                    ),
                    func.min(
                        case(
                            (
                                execution.status == "processing",
                                event.occurred_at,
                            ),
                            else_=None,
                        )
                    ),
                    func.min(
                        case(
                            (
                                (
                                    (execution.status == "pending")
                                    & execution.next_attempt_at.is_not(None)
                                ),
                                event.occurred_at,
                            ),
                            else_=None,
                        )
                    ),
                    func.min(
                        case(
                            (
                                (
                                    (execution.status == "pending")
                                    & execution.next_attempt_at.is_not(None)
                                ),
                                execution.next_attempt_at,
                            ),
                            else_=None,
                        )
                    ),
                    func.min(
                        case(
                            (
                                execution.status == "dlq",
                                event.occurred_at,
                            ),
                            else_=None,
                        )
                    ),
                )
                .select_from(execution)
                .join(event, event.id == execution.event_id)
                .where(
                    event.board_id == board_id,
                    execution.handler_name == _POLICY_CONSTRAINT_HANDLER,
                    execution.status.in_(policy_statuses),
                )
            )
        ).one()
        return KGHealthQueueSnapshot(
            board_exists=await context.get(Board, board_id) is not None,
            queue_depth=int(queue_depth or 0),
            oldest_triggered_at=_aware_utc(oldest),
            dead_letter_count=int(dead_letters or 0),
            policy_constraint_projection_pending_count=int(
                policy_row[0] or 0
            ),
            policy_constraint_projection_processing_count=int(
                policy_row[1] or 0
            ),
            policy_constraint_projection_retry_scheduled_count=int(
                policy_row[2] or 0
            ),
            policy_constraint_projection_dlq_count=int(policy_row[3] or 0),
            policy_constraint_projection_max_attempt_count=int(
                policy_row[4] or 0
            ),
            policy_constraint_projection_oldest_pending_at=_aware_utc(
                policy_row[5]
            ),
            policy_constraint_projection_oldest_processing_at=_aware_utc(
                policy_row[6]
            ),
            policy_constraint_projection_oldest_retry_scheduled_at=(
                _aware_utc(policy_row[7])
            ),
            policy_constraint_projection_oldest_retry_due_at=_aware_utc(
                policy_row[8]
            ),
            policy_constraint_projection_oldest_dlq_at=_aware_utc(
                policy_row[9]
            ),
        )

    async def list_tick_runs(self, context: Any) -> tuple[KGTickRunFact, ...]:
        rows = (await context.execute(select(KGTickRun))).scalars().all()
        return tuple(
            KGTickRunFact(
                started_at=row.started_at,
                completed_at=row.completed_at,
                nodes_recomputed=int(row.nodes_recomputed or 0),
                boards_processed=int(row.boards_processed or 0),
                boards_failed=int(row.boards_failed or 0),
                error=row.error,
            )
            for row in rows
        )

    async def has_materialized_history(
        self, context: Any, *, board_id: str
    ) -> bool:
        refs = await context.scalar(
            select(func.count()).where(KuzuNodeRef.board_id == board_id)
        )
        if int(refs or 0) > 0:
            return True
        nodes = await context.scalar(
            select(func.coalesce(func.sum(ConsolidationAudit.nodes_added), 0)).where(
                ConsolidationAudit.board_id == board_id
            )
        )
        return int(nodes or 0) > 0

    async def count_partition_debt(
        self,
        context: Any,
        *,
        board_id: str,
        target_status: str,
        open_states: Sequence[str],
    ) -> int:
        value = await context.scalar(
            select(func.count()).select_from(CanonicalDebt).where(
                CanonicalDebt.board_id == board_id,
                CanonicalDebt.target_status == target_status,
                CanonicalDebt.canonical_state.in_(tuple(open_states)),
            )
        )
        return int(value or 0)


__all__ = ["CommunitySqlAlchemyKGHealthReader"]
