"""Community SQLAlchemy reader for skip-override audit facts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import ActivityLog
from okto_pulse.core.ports.skip_overrides import AmbiguitySkipAuditFact


class CommunitySqlAlchemySkipOverrideReader:
    async def latest_enabled_ambiguity_skip(
        self,
        context: Any,
        *,
        board_id: str,
        ideation_id: str,
        action: str,
    ) -> AmbiguitySkipAuditFact | None:
        rows = (
            await context.execute(
                select(ActivityLog)
                .where(
                    ActivityLog.board_id == board_id,
                    ActivityLog.action == action,
                )
                .order_by(ActivityLog.created_at.desc())
            )
        ).scalars().all()
        for row in rows:
            details = row.details or {}
            if str(details.get("ideation_id") or "") != ideation_id:
                continue
            if details.get("new_value") is True:
                return AmbiguitySkipAuditFact(
                    actor_id=row.actor_id,
                    created_at=row.created_at,
                    source=(str(details["source"]) if details.get("source") else None),
                )
        return None


__all__ = ["CommunitySqlAlchemySkipOverrideReader"]
