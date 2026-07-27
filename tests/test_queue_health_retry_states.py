"""Integration coverage for Community-owned queue-state projections."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    ConsolidationQueue,
)
from okto_pulse.community.adapters.sqlalchemy_queue_health import (
    CommunitySqlAlchemyQueueHealthReader,
)


@pytest.mark.asyncio
async def test_active_snapshot_separates_ready_scheduled_and_overdue_claims(
    tmp_path: Path,
) -> None:
    """SQLite mechanics stay in Community while Core receives typed facts."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'queue-health.db'}"
    )
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    now = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)
    board_id = "board-queue-health"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with factory() as session:
            session.add(Board(id=board_id, name="Queue", owner_id="owner"))
            session.add_all(
                [
                    ConsolidationQueue(
                        id="ready",
                        board_id=board_id,
                        artifact_type="card",
                        artifact_id="card-ready",
                        work_kind="consolidate",
                        status="pending",
                        attempts=1,
                        triggered_at=now - timedelta(minutes=20),
                    ),
                    ConsolidationQueue(
                        id="scheduled",
                        board_id=board_id,
                        artifact_type="card",
                        artifact_id="card-scheduled",
                        work_kind="stale_reconcile",
                        generation=1,
                        status="pending",
                        attempts=2,
                        triggered_at=now - timedelta(minutes=19),
                        next_retry_at=now + timedelta(minutes=5),
                    ),
                    ConsolidationQueue(
                        id="claimed-current",
                        board_id=board_id,
                        artifact_type="spec",
                        artifact_id="spec-current",
                        work_kind="consolidate",
                        status="claimed",
                        attempts=3,
                        triggered_at=now - timedelta(minutes=18),
                        claimed_at=now - timedelta(seconds=30),
                        claim_timeout_at=now + timedelta(minutes=2),
                    ),
                    ConsolidationQueue(
                        id="claimed-overdue",
                        board_id=board_id,
                        artifact_type="board",
                        artifact_id=board_id,
                        work_kind="stale_sweep",
                        status="claimed",
                        attempts=4,
                        triggered_at=now - timedelta(minutes=17),
                        claimed_at=now - timedelta(minutes=10),
                        claim_timeout_at=now - timedelta(minutes=5),
                    ),
                ]
            )
            await session.commit()

        async with factory() as session:
            snapshot = await CommunitySqlAlchemyQueueHealthReader().active_snapshot(
                session,
                board_id=board_id,
                active_statuses=("pending", "claimed"),
                max_outbox_retries=5,
                dead_letter_retry_sentinel=-1,
                now=now,
                stuck_before=now - timedelta(minutes=5),
                item_limit=10,
            )

        assert snapshot.consolidation_by_status == {"pending": 2, "claimed": 2}
        assert snapshot.consolidation_ready_count == 1
        assert snapshot.consolidation_scheduled_retry_count == 1
        assert snapshot.consolidation_claimed_count == 2
        assert snapshot.consolidation_overdue_claimed_count == 1
        next_retry = snapshot.consolidation_next_retry_at
        assert next_retry is not None
        if next_retry.tzinfo is None:
            next_retry = next_retry.replace(tzinfo=timezone.utc)
        assert next_retry == now + timedelta(minutes=5)
        assert snapshot.consolidation_by_work_kind == {
            "consolidate": 2,
            "stale_reconcile": 1,
            "stale_sweep": 1,
        }
        assert snapshot.consolidation_max_attempts == 4
        assert [item.queue_id for item in snapshot.consolidation_items] == [
            "ready",
            "scheduled",
            "claimed-current",
            "claimed-overdue",
        ]
    finally:
        await engine.dispose()
