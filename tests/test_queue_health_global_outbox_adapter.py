"""Community queue-health adapter coverage for terminal global outbox rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import GlobalUpdateOutbox
from okto_pulse.community.adapters.sqlalchemy_queue_health import (
    CommunitySqlAlchemyQueueHealthReader,
)


def _row(
    row_id: str,
    *,
    board_id: str,
    retry_count: int,
    processed: bool = False,
) -> GlobalUpdateOutbox:
    now = datetime.now(timezone.utc)
    return GlobalUpdateOutbox(
        id=row_id,
        event_id=f"event-{row_id}",
        board_id=board_id,
        session_id="session",
        event_type="node_upsert",
        payload={"node": row_id},
        created_at=now - timedelta(minutes=1),
        processed_at=now if processed else None,
        retry_count=retry_count,
        last_error="MemoryError: bad allocation",
    )


@pytest.mark.asyncio
async def test_adapter_counts_only_terminal_unprocessed_rows_per_board(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'queue-health.db'}"
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(GlobalUpdateOutbox.__table__.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add_all(
                [
                    _row("active", board_id="board-a", retry_count=0),
                    _row("max", board_id="board-a", retry_count=5),
                    _row("sentinel", board_id="board-a", retry_count=-1),
                    _row(
                        "processed",
                        board_id="board-a",
                        retry_count=5,
                        processed=True,
                    ),
                    _row("other", board_id="board-b", retry_count=-1),
                ]
            )
            await session.commit()

            snapshot = await CommunitySqlAlchemyQueueHealthReader().global_outbox_dead_letter_snapshot(
                session,
                board_id="board-a",
                limit=1,
                max_outbox_retries=5,
                dead_letter_retry_sentinel=-1,
            )

        assert snapshot.total_count == 2
        assert len(snapshot.rows) == 1
        assert snapshot.rows[0].board_id == "board-a"
        assert snapshot.rows[0].retry_count in {-1, 5}
        assert snapshot.oldest_created_at is not None
    finally:
        await engine.dispose()
