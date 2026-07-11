"""Community SQLAlchemy Discovery catalog read adapter."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    BoardShare,
    DiscoveryIntent,
    DiscoverySavedSearch,
    DiscoverySearchHistory,
)
from okto_pulse.core.ports.discovery_catalog import (
    DiscoveryIntentRecord,
    DiscoverySavedSearchRecord,
    DiscoverySearchHistoryRecord,
)


def _intent(row: DiscoveryIntent) -> DiscoveryIntentRecord:
    return DiscoveryIntentRecord(
        id=row.id,
        name=row.name,
        label=row.label,
        description=row.description,
        category=row.category,
        tool_binding=row.tool_binding,
        params_schema=row.params_schema,
        renderer=row.renderer,
        min_permission=row.min_permission,
        active=bool(row.active),
        is_seed=bool(row.is_seed),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class CommunitySqlAlchemyDiscoveryCatalogReader:
    async def list_active_intents(self, context: Any):  # noqa: ANN201
        rows = (
            await context.execute(
                select(DiscoveryIntent)
                .where(DiscoveryIntent.active.is_(True))
                .order_by(DiscoveryIntent.category, DiscoveryIntent.label)
            )
        ).scalars().all()
        return tuple(_intent(row) for row in rows)

    async def list_saved_searches(
        self, context: Any, *, board_id: str
    ):  # noqa: ANN201
        rows = (
            await context.execute(
                select(DiscoverySavedSearch)
                .where(DiscoverySavedSearch.board_id == board_id)
                .order_by(DiscoverySavedSearch.created_at.desc())
            )
        ).scalars().all()
        return tuple(
            DiscoverySavedSearchRecord(
                id=row.id,
                board_id=row.board_id,
                name=row.name,
                query=row.query,
                intent_id=row.intent_id,
                filters_json=row.filters_json,
                created_by=row.created_by,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_search_history(
        self,
        context: Any,
        *,
        board_id: str,
        user_id: str,
        limit: int,
    ):  # noqa: ANN201
        rows = (
            await context.execute(
                select(DiscoverySearchHistory)
                .where(
                    DiscoverySearchHistory.board_id == board_id,
                    DiscoverySearchHistory.user_id == user_id,
                )
                .order_by(DiscoverySearchHistory.searched_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return tuple(
            DiscoverySearchHistoryRecord(
                id=row.id,
                board_id=row.board_id,
                user_id=row.user_id,
                query=row.query,
                intent_id=row.intent_id,
                result_count=int(row.result_count or 0),
                searched_at=row.searched_at,
            )
            for row in rows
        )

    async def get_intent(
        self, context: Any, *, intent_id: str
    ) -> DiscoveryIntentRecord | None:
        row = await context.get(DiscoveryIntent, intent_id)
        return _intent(row) if row is not None else None

    async def can_read_board(
        self, context: Any, *, board_id: str, user_id: str
    ) -> bool:
        board = await context.get(Board, board_id)
        if board is None:
            return False
        if board.owner_id == user_id:
            return True
        share = (
            await context.execute(
                select(BoardShare.id).where(
                    BoardShare.board_id == board_id,
                    BoardShare.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        return share is not None


__all__ = ["CommunitySqlAlchemyDiscoveryCatalogReader"]
