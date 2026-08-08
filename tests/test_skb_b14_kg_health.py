"""B14 board-scoped policy projection delivery health."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_kg_health import (
    CommunitySqlAlchemyKGHealthReader,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
)


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def _event(
    *,
    event_id: str,
    board_id: str,
    occurred_at: datetime,
) -> DomainEventRow:
    return DomainEventRow(
        id=event_id,
        event_type="board.policy_binding_materialized.v1",
        board_id=board_id,
        actor_id="agent-b14",
        actor_type="agent",
        payload_json={},
        occurred_at=occurred_at,
    )


def _execution(
    *,
    event_id: str,
    suffix: str,
    status: str,
    attempts: int,
    handler_name: str = "PolicyConstraintProjectionHandler",
    next_attempt_at: datetime | None = None,
) -> DomainEventHandlerExecution:
    return DomainEventHandlerExecution(
        id=f"execution-{suffix}",
        event_id=event_id,
        handler_name=handler_name,
        status=status,
        attempts=attempts,
        next_attempt_at=next_attempt_at,
    )


@pytest.mark.asyncio
async def test_b14_health_isolates_board_handler_and_delivery_buckets(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b14-health.sqlite3")
    now = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
    board_id = "board-b14-health"
    other_board_id = "board-b14-health-other"
    facts = (
        ("pending", "pending", 1, None),
        ("retry", "pending", 2, now + timedelta(minutes=30)),
        ("processing", "processing", 3, None),
        ("dlq", "dlq", 4, None),
        ("done", "done", 90, None),
        ("failed", "failed", 80, None),
    )
    async with get_session_factory()() as session:
        session.add_all(
            [
                Board(id=board_id, name="B14 health", owner_id="owner"),
                Board(
                    id=other_board_id,
                    name="B14 other",
                    owner_id="owner",
                ),
            ]
        )
        await session.flush()
        for index, (suffix, status, attempts, due_at) in enumerate(facts):
            event_id = f"event-{suffix}"
            event_row = _event(
                event_id=event_id,
                board_id=board_id,
                occurred_at=now + timedelta(minutes=index),
            )
            session.add(event_row)
            await session.flush((event_row,))
            session.add(
                _execution(
                    event_id=event_id,
                    suffix=suffix,
                    status=status,
                    attempts=attempts,
                    next_attempt_at=due_at,
                )
            )
        other_board_event = _event(
            event_id="event-other-board",
            board_id=other_board_id,
            occurred_at=now - timedelta(days=1),
        )
        session.add(other_board_event)
        await session.flush((other_board_event,))
        session.add(
            _execution(
                event_id="event-other-board",
                suffix="other-board",
                status="dlq",
                attempts=99,
            )
        )
        other_handler_event = _event(
            event_id="event-other-handler",
            board_id=board_id,
            occurred_at=now - timedelta(days=2),
        )
        session.add(other_handler_event)
        await session.flush((other_handler_event,))
        session.add(
            _execution(
                event_id="event-other-handler",
                suffix="other-handler",
                status="dlq",
                attempts=98,
                handler_name="UnrelatedHandler",
            )
        )
        await session.commit()

    async with get_session_factory()() as session:
        snapshot = await CommunitySqlAlchemyKGHealthReader().queue_snapshot(
            session,
            board_id=board_id,
        )

    assert snapshot.policy_constraint_projection_pending_count == 1
    assert snapshot.policy_constraint_projection_processing_count == 1
    assert snapshot.policy_constraint_projection_retry_scheduled_count == 1
    assert snapshot.policy_constraint_projection_dlq_count == 1
    assert snapshot.policy_constraint_projection_max_attempt_count == 4
    assert snapshot.policy_constraint_projection_oldest_pending_at == now
    assert snapshot.policy_constraint_projection_oldest_retry_scheduled_at == (
        now + timedelta(minutes=1)
    )
    assert snapshot.policy_constraint_projection_oldest_retry_due_at == (
        now + timedelta(minutes=30)
    )
    assert snapshot.policy_constraint_projection_oldest_processing_at == (
        now + timedelta(minutes=2)
    )
    assert snapshot.policy_constraint_projection_oldest_dlq_at == (
        now + timedelta(minutes=3)
    )
