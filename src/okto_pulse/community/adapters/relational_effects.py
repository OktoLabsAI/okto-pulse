"""Community SQLAlchemy implementation of relational side-effect ports."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from okto_pulse.core.models.db import Board, ConsolidationQueue, KGTickRun
from okto_pulse.core.ports.relational_effects import (
    ConsolidationQueueUpsert,
    KGTickRunUpsert,
    RelationalEffectsPort,
    register_relational_effects_port,
)


class CommunitySqlAlchemyRelationalEffects(RelationalEffectsPort):
    """Community-owned SQLAlchemy implementation for core runtime effects."""

    async def count_active_consolidation_queue(
        self,
        session: Any,
        *,
        board_id: str,
    ) -> int:
        depth = await session.scalar(
            select(func.count()).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.status.in_(["pending", "claimed"]),
            )
        )
        return int(depth or 0)

    async def upsert_consolidation_queue(
        self,
        session: Any,
        upsert: ConsolidationQueueUpsert,
    ) -> None:
        insert = _upsert_insert_for_session(session)
        stmt = (
            insert(ConsolidationQueue)
            .values(
                board_id=upsert.board_id,
                artifact_type=upsert.artifact_type,
                artifact_id=upsert.artifact_id,
                priority=upsert.priority,
                source=upsert.source,
                triggered_by_event=upsert.triggered_by_event,
                status="pending",
            )
            .on_conflict_do_update(
                index_elements=["board_id", "artifact_type", "artifact_id"],
                set_={
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                    "priority": upsert.priority,
                    "source": upsert.source,
                    "triggered_by_event": upsert.triggered_by_event,
                    "claimed_by_session_id": None,
                    "claimed_at": None,
                    "worker_id": None,
                    "claim_timeout_at": None,
                    "next_retry_at": None,
                },
                where=ConsolidationQueue.status.notin_(("pending", "claimed")),
            )
        )
        await session.execute(stmt)

    async def list_board_ids(self, session: Any) -> list[str]:
        result = await session.execute(select(Board.id))
        return list(result.scalars().all())

    async def read_latest_kg_tick_completed_at(
        self,
        session: Any,
    ) -> datetime | None:
        return (
            await session.execute(
                select(KGTickRun.completed_at)
                .where(KGTickRun.completed_at.is_not(None))
                .order_by(KGTickRun.completed_at.desc())
                .limit(1)
            )
        ).scalars().first()

    async def upsert_kg_tick_run(
        self,
        session: Any,
        upsert: KGTickRunUpsert,
    ) -> None:
        insert = _upsert_insert_for_session(session)
        stmt = (
            insert(KGTickRun)
            .values(
                tick_id=upsert.tick_id,
                started_at=upsert.started_at,
                completed_at=upsert.completed_at,
                nodes_recomputed=upsert.nodes_recomputed,
                duration_ms=upsert.duration_ms,
                boards_processed=upsert.boards_processed,
                boards_failed=upsert.boards_failed,
                error=upsert.error,
            )
            .on_conflict_do_update(
                index_elements=["tick_id"],
                set_={
                    "completed_at": upsert.completed_at,
                    "nodes_recomputed": upsert.nodes_recomputed,
                    "duration_ms": upsert.duration_ms,
                    "boards_processed": upsert.boards_processed,
                    "boards_failed": upsert.boards_failed,
                    "error": upsert.error,
                },
            )
        )
        await session.execute(stmt)


def _upsert_insert_for_session(session: Any):
    dialect_name = session.bind.dialect.name if session.bind else None
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert

    return insert


_relational_effects = CommunitySqlAlchemyRelationalEffects()


def register_community_relational_effects() -> CommunitySqlAlchemyRelationalEffects:
    """Register Community relational side-effect implementation in core."""

    register_relational_effects_port(_relational_effects)
    return _relational_effects


__all__ = [
    "CommunitySqlAlchemyRelationalEffects",
    "register_community_relational_effects",
]
