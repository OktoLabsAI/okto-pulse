from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventDeliveryStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
)
from okto_pulse.core.application.domain_event_delivery import (
    DomainEventDeliveryProcessor,
)
from okto_pulse.core.events.types import CardCreated


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


class RecordingHandler:
    async def handle(self, event: CardCreated, session) -> None:  # noqa: ANN001
        board = await session.get(Board, event.board_id)
        board.description = f"handled:{event.card_id}"


class FailingHandler:
    async def handle(self, event: CardCreated, session) -> None:  # noqa: ANN001
        board = await session.get(Board, event.board_id)
        board.description = f"must-rollback:{event.card_id}"
        raise RuntimeError("temporary adapter failure")


async def _runtime(path: Path):  # noqa: ANN202
    engine = build_community_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, build_community_session_factory(engine)


async def _seed(session_factory, *, execution_id: str, handler_name: str) -> None:  # noqa: ANN001
    async with session_factory() as session:
        if await session.get(Board, "board-f05") is None:
            session.add(
                Board(
                    id="board-f05",
                    name="F05",
                    owner_id="owner-f05",
                )
            )
        session.add(
            DomainEventRow(
                id=f"event-{execution_id}",
                event_type=CardCreated.event_type,
                board_id="board-f05",
                actor_id="agent-f05",
                actor_type="agent",
                payload_json={"card_id": "card-f05", "spec_id": "spec-f05"},
                occurred_at=NOW,
            )
        )
        await session.flush()
        session.add(
            DomainEventHandlerExecution(
                id=execution_id,
                event_id=f"event-{execution_id}",
                handler_name=handler_name,
                status="pending",
                attempts=0,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_adapter_commits_handler_effect_and_completion_atomically(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _runtime(tmp_path / "event-success.db")
    try:
        await _seed(
            session_factory,
            execution_id="exec-success",
            handler_name=RecordingHandler.__name__,
        )
        processor = DomainEventDeliveryProcessor(
            CommunitySqlAlchemyDomainEventDeliveryStore(session_factory),
            handler_resolver=lambda _name, _event: RecordingHandler,
            clock=lambda: NOW,
        )

        assert await processor.process_batch() == 1

        async with session_factory() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                "exec-success",
            )
            board = await session.get(Board, "board-f05")
            assert execution.status == "done"
            assert execution.attempts == 1
            assert execution.processed_at.replace(tzinfo=timezone.utc) == NOW
            assert board.description == "handled:card-f05"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_adapter_rolls_back_handler_effect_before_retry_state(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _runtime(tmp_path / "event-retry.db")
    try:
        await _seed(
            session_factory,
            execution_id="exec-retry",
            handler_name=FailingHandler.__name__,
        )
        processor = DomainEventDeliveryProcessor(
            CommunitySqlAlchemyDomainEventDeliveryStore(session_factory),
            handler_resolver=lambda _name, _event: FailingHandler,
            clock=lambda: NOW,
        )

        assert await processor.process_batch() == 1

        async with session_factory() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                "exec-retry",
            )
            board = await session.get(Board, "board-f05")
            assert execution.status == "pending"
            assert execution.attempts == 1
            assert execution.last_error == "temporary adapter failure"
            assert execution.next_attempt_at is not None
            assert board.description is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_adapter_recovers_processing_rows_idempotently(tmp_path: Path) -> None:
    engine, session_factory = await _runtime(tmp_path / "event-recovery.db")
    try:
        await _seed(
            session_factory,
            execution_id="exec-orphan",
            handler_name=RecordingHandler.__name__,
        )
        async with session_factory() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                "exec-orphan",
            )
            execution.status = "processing"
            await session.commit()

        store = CommunitySqlAlchemyDomainEventDeliveryStore(session_factory)
        assert await store.recover_orphans() == 1
        assert await store.recover_orphans() == 0

        async with session_factory() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                "exec-orphan",
            )
            assert execution.status == "pending"
    finally:
        await engine.dispose()
