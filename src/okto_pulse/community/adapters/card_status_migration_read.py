"""SQLite read of the migration origin used by the legacy return edge."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class CommunityCardStatusMigrationRead:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def return_to_planning_eligible(
        self, *, board_id: str, card_id: str
    ) -> bool:
        # Activity rows share the Card's SQLite transaction. rowid gives a
        # durable insertion order even when created_at has second precision.
        result = await self._session.execute(
            text(
                "SELECT EXISTS ("
                " SELECT 1 FROM activity_logs AS origin"
                " WHERE origin.board_id = :board_id AND origin.card_id = :card_id"
                " AND origin.action = 'card_status_normalized'"
                " AND json_extract(origin.details, '$.from_status') = 'started'"
                " AND json_extract(origin.details, '$.to_status') = 'in_progress'"
                " AND NOT EXISTS ("
                "  SELECT 1 FROM activity_logs AS moved"
                "  WHERE moved.board_id = origin.board_id"
                "  AND moved.card_id = origin.card_id"
                "  AND moved.rowid > origin.rowid"
                "  AND moved.action = 'card_moved'"
                "  AND json_extract(moved.details, '$.to_status') = 'in_progress'"
                " )"
                ")"
            ),
            {"board_id": board_id, "card_id": card_id},
        )
        return bool(result.scalar_one())
