"""Community SQLAlchemy global outbox store."""

from __future__ import annotations

import copy
from typing import Any, Sequence

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    GlobalUpdateOutbox,
    KuzuNodeRef,
)
from okto_pulse.core.ports.global_outbox import (
    GlobalOutboxEventRecord,
    GlobalOutboxNodeRefFact,
)


def _record(row: Any) -> GlobalOutboxEventRecord:
    return GlobalOutboxEventRecord(
        id=str(row.id),
        event_id=str(row.event_id),
        board_id=str(row.board_id),
        session_id=str(row.session_id) if row.session_id else None,
        payload=copy.deepcopy(row.payload or {}),
        retry_count=int(row.retry_count),
        last_error=row.last_error,
        processed_at=row.processed_at,
    )


class CommunitySqlAlchemyGlobalOutboxStore:
    async def materialize_claimed(
        self, context: Any, claimed: Sequence[Any]
    ) -> tuple[GlobalOutboxEventRecord, ...]:
        return tuple(
            item if isinstance(item, GlobalOutboxEventRecord) else _record(item)
            for item in claimed
        )

    async def list_dead_letters(
        self, context: Any, *, limit: int
    ) -> tuple[GlobalOutboxEventRecord, ...]:
        rows = (
            await context.execute(
                select(GlobalUpdateOutbox)
                .where(
                    GlobalUpdateOutbox.processed_at.is_(None),
                    GlobalUpdateOutbox.retry_count == -1,
                )
                .order_by(GlobalUpdateOutbox.created_at.asc())
                .limit(limit)
            )
        ).scalars().all()
        return tuple(_record(row) for row in rows)

    async def list_added_node_refs(
        self,
        context: Any,
        *,
        session_id: str,
        board_id: str,
        node_types: Sequence[str],
    ) -> tuple[GlobalOutboxNodeRefFact, ...]:
        rows = (
            await context.execute(
                select(KuzuNodeRef).where(
                    KuzuNodeRef.session_id == session_id,
                    KuzuNodeRef.board_id == board_id,
                    KuzuNodeRef.operation == "add",
                    KuzuNodeRef.kuzu_node_type.in_(tuple(node_types)),
                )
            )
        ).scalars().all()
        return tuple(
            GlobalOutboxNodeRefFact(
                graph_node_id=str(row.kuzu_node_id),
                graph_node_type=str(row.kuzu_node_type),
            )
            for row in rows
        )

    async def save_events(
        self, context: Any, events: Sequence[GlobalOutboxEventRecord]
    ) -> None:
        for event in events:
            row = await context.get(GlobalUpdateOutbox, event.id)
            if row is None:
                continue
            row.retry_count = event.retry_count
            row.last_error = event.last_error
            row.processed_at = event.processed_at
        await context.flush()

    async def commit(self, context: Any) -> None:
        await context.commit()


__all__ = ["CommunitySqlAlchemyGlobalOutboxStore"]
