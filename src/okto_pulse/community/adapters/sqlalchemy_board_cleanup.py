"""Community SQLAlchemy board runtime cleanup adapter."""

from __future__ import annotations

from sqlalchemy import delete

from okto_pulse.community.adapters.sqlalchemy_models import (
    ConsolidationAudit,
    ConsolidationQueue,
    GlobalUpdateOutbox,
)
from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory


class CommunitySqlAlchemyBoardRelationalCleanup:
    async def wipe_runtime_rows(self, *, board_id: str) -> dict[str, int]:
        removed: dict[str, int] = {}
        async with get_session_factory()() as session:
            for model, label in (
                (GlobalUpdateOutbox, "outbox"),
                (ConsolidationAudit, "audit"),
                (ConsolidationQueue, "queue"),
            ):
                result = await session.execute(
                    delete(model).where(model.board_id == board_id)
                )
                removed[label] = int(result.rowcount or 0)
            await session.commit()
        return removed


__all__ = ["CommunitySqlAlchemyBoardRelationalCleanup"]
