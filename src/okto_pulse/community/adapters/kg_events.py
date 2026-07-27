"""Local First event-store adapter for the Core KG SSE coordination port."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, asc, func, or_, select

from okto_pulse.core.ports.kg_events import (
    HISTORICAL_PROGRESS_SETTINGS_KEY,
    KGEventsPoll,
    KGOutboxEvent,
    get_kg_events_reader_port,
    register_kg_events_reader_port,
)
from okto_pulse.community.adapters.sqlalchemy_repositories import (
    Board,
    ConsolidationQueue,
    GlobalUpdateOutbox,
)

logger = logging.getLogger("okto_pulse.community.adapters.kg_events")

SessionScopeFactory = Callable[[], Any]
_pending_session_closes: set[asyncio.Task[None]] = set()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _cancel_safe_close(awaitable: Awaitable[None]) -> None:
    """Finish a Local First session close even when an SSE consumer cancels."""

    close_task = asyncio.get_running_loop().create_task(awaitable)
    _pending_session_closes.add(close_task)
    close_task.add_done_callback(_pending_session_closes.discard)
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("community.kg_events.session_close_failed")


@asynccontextmanager
async def cancel_safe_community_session_scope(
    session_factory: SessionScopeFactory,
) -> AsyncGenerator[Any, None]:
    """Own the cancellation-safe lifecycle of the Community SQL session."""

    scope = session_factory()
    enter = getattr(scope, "__aenter__", None)
    exit_ = getattr(scope, "__aexit__", None)
    if callable(enter) and callable(exit_):
        session = await enter()
    else:
        session = scope
        exit_ = None
    exc_info: tuple[type[BaseException] | None, BaseException | None, Any] = (
        None,
        None,
        None,
    )
    try:
        yield session
    except BaseException as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
        raise
    finally:
        if exit_ is not None:
            await _cancel_safe_close(exit_(*exc_info))
        else:
            await _cancel_safe_close(session.close())


class CommunityKGEventsReader:
    """SQLite implementation of the Core KG event reader port."""

    def __init__(self, session_factory: SessionScopeFactory) -> None:
        self._session_factory = session_factory

    async def poll(
        self,
        *,
        board_id: str,
        after: datetime,
        after_event_id: str | None = None,
        limit: int,
    ) -> KGEventsPoll:
        async with cancel_safe_community_session_scope(self._session_factory) as session:
            events = await self._query_outbox_rows(
                session,
                board_id=board_id,
                after=after,
                after_event_id=after_event_id,
                limit=limit,
            )
            progress = await self._query_queue_snapshot(session, board_id=board_id)
        return KGEventsPoll(events=events, progress=progress)

    async def replay(
        self,
        *,
        board_id: str,
        after: datetime,
        after_event_id: str | None = None,
        limit: int,
    ) -> Sequence[KGOutboxEvent]:
        async with cancel_safe_community_session_scope(self._session_factory) as session:
            return await self._query_outbox_rows(
                session,
                board_id=board_id,
                after=after,
                after_event_id=after_event_id,
                limit=limit,
            )

    async def _query_outbox_rows(
        self,
        session: Any,
        *,
        board_id: str,
        after: datetime,
        after_event_id: str | None,
        limit: int,
    ) -> list[KGOutboxEvent]:
        cursor_predicate = GlobalUpdateOutbox.created_at > after
        if after_event_id is not None:
            cursor_predicate = or_(
                GlobalUpdateOutbox.created_at > after,
                and_(
                    GlobalUpdateOutbox.created_at == after,
                    GlobalUpdateOutbox.event_id > after_event_id,
                ),
            )
        rows = (
            await session.execute(
                select(GlobalUpdateOutbox)
                .where(
                    and_(
                        GlobalUpdateOutbox.board_id == board_id,
                        cursor_predicate,
                    )
                )
                .order_by(
                    asc(GlobalUpdateOutbox.created_at),
                    asc(GlobalUpdateOutbox.event_id),
                )
                .limit(limit)
            )
        ).scalars().all()
        return [
            KGOutboxEvent(
                event_id=row.event_id,
                session_id=row.session_id,
                event_type=row.event_type,
                created_at=_as_utc(row.created_at),
                payload=(
                    dict(row.payload)
                    if isinstance(row.payload, Mapping)
                    else {}
                ),
            )
            for row in rows
        ]

    async def _query_queue_snapshot(
        self,
        session: Any,
        *,
        board_id: str,
    ) -> dict[str, int]:
        rows = (
            await session.execute(
                select(ConsolidationQueue.status, ConsolidationQueue.source, func.count())
                .where(ConsolidationQueue.board_id == board_id)
                .group_by(ConsolidationQueue.status, ConsolidationQueue.source)
            )
        ).all()
        snapshot = {"pending": 0, "claimed": 0, "done": 0, "failed": 0, "paused": 0}
        historical = {"pending": 0, "claimed": 0, "done": 0, "failed": 0, "paused": 0}
        for status, source, count in rows:
            if status in snapshot:
                snapshot[status] += int(count)
                if source == "historical_backfill":
                    historical[status] += int(count)

        live_total = sum(snapshot.values())
        historical_active = (
            historical["pending"] + historical["claimed"] + historical["paused"]
        )
        historical_total = 0
        if historical_active > 0:
            board = await session.get(Board, board_id)
            if board is not None and isinstance(board.settings, Mapping):
                state = board.settings.get(HISTORICAL_PROGRESS_SETTINGS_KEY)
                if isinstance(state, Mapping):
                    try:
                        historical_total = int(state.get("total") or 0)
                    except (TypeError, ValueError):
                        historical_total = 0

        non_historical_total = live_total - sum(historical.values())
        if historical_active > 0 and historical_total > 0:
            snapshot["total"] = max(live_total, historical_total + non_historical_total)
            snapshot["processed"] = max(
                0,
                snapshot["total"]
                - (snapshot["pending"] + snapshot["claimed"] + snapshot["paused"]),
            )
        else:
            snapshot["total"] = live_total
            snapshot["processed"] = snapshot["done"]
        return snapshot


def register_community_kg_events_reader(
    session_factory: SessionScopeFactory,
) -> CommunityKGEventsReader:
    """Register the Community Local First reader in the Core port registry."""

    reader = CommunityKGEventsReader(session_factory)
    register_kg_events_reader_port(reader)
    return reader


async def poll_community_kg_events(
    *,
    board_id: str,
    after: datetime,
    after_event_id: str | None = None,
    limit: int,
) -> KGEventsPoll:
    """Perform one finite read through the reader selected by composition."""

    return await get_kg_events_reader_port().poll(
        board_id=board_id,
        after=after,
        after_event_id=after_event_id,
        limit=limit,
    )


__all__ = [
    "CommunityKGEventsReader",
    "cancel_safe_community_session_scope",
    "poll_community_kg_events",
    "register_community_kg_events_reader",
]
