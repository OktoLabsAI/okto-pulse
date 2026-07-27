from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventFactReader,
    CommunitySqlAlchemyDomainEventDeliveryStore,
)
from okto_pulse.community.adapters.board_source_reader import (
    CommunityBoardSourceReader,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    Card,
    DomainEventHandlerExecution,
    DomainEventRow,
    Spec,
)
from okto_pulse.core.application.domain_event_delivery import (
    DomainEventDeliveryProcessor,
)
from okto_pulse.core.events.types import CardCreated, KGFullRebuildTick


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


class FlakyFullRebuildHandler:
    calls = 0

    async def handle(self, event: KGFullRebuildTick, session) -> None:  # noqa: ANN001
        type(self).calls += 1
        if type(self).calls == 1:
            raise RuntimeError("temporary full rebuild failure")
        board = await session.get(Board, event.board_id)
        board.description = f"rebuilt:{event.tick_id}"


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
async def test_full_rebuild_retry_success_ack_clears_stale_backoff(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _runtime(tmp_path / "full-rebuild-retry.db")
    event = KGFullRebuildTick(
        board_id="board-f05",
        tick_id="full-rebuild-retry",
        scheduled_at=NOW.isoformat(),
        force_full_rebuild=True,
    )
    async with session_factory() as session:
        session.add(Board(id="board-f05", name="F05", owner_id="owner-f05"))
        session.add(
            DomainEventRow(
                id=event.event_id,
                event_type=event.event_type,
                board_id=event.board_id,
                actor_id=event.actor_id,
                actor_type=event.actor_type,
                payload_json=event.payload_for_storage(),
                occurred_at=event.occurred_at,
            )
        )
        await session.flush()
        session.add(
            DomainEventHandlerExecution(
                id="exec-full-rebuild-retry",
                event_id=event.event_id,
                handler_name=FlakyFullRebuildHandler.__name__,
                status="pending",
                attempts=0,
            )
        )
        await session.commit()

    current_time = NOW

    def clock() -> datetime:
        return current_time

    FlakyFullRebuildHandler.calls = 0
    processor = DomainEventDeliveryProcessor(
        CommunitySqlAlchemyDomainEventDeliveryStore(session_factory),
        handler_resolver=lambda _name, _event: FlakyFullRebuildHandler,
        clock=clock,
    )
    try:
        assert await processor.process_batch() == 1
        async with session_factory() as session:
            pending = await session.get(
                DomainEventHandlerExecution,
                "exec-full-rebuild-retry",
            )
            assert pending.status == "pending"
            assert pending.last_error == "temporary full rebuild failure"
            assert pending.next_attempt_at is not None
            retry_at = pending.next_attempt_at.replace(tzinfo=timezone.utc)

        current_time = retry_at + timedelta(microseconds=1)
        assert await processor.process_batch() == 1

        async with session_factory() as session:
            acknowledged = await session.get(
                DomainEventHandlerExecution,
                "exec-full-rebuild-retry",
            )
            board = await session.get(Board, "board-f05")
            assert acknowledged.status == "done"
            assert acknowledged.attempts == 2
            assert acknowledged.last_error is None
            assert acknowledged.next_attempt_at is None
            assert acknowledged.processed_at.replace(tzinfo=timezone.utc) == current_time
            assert board.description == "rebuilt:full-rebuild-retry"
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


@pytest.mark.asyncio
async def test_cognitive_facts_match_rebuild_source_hashes(tmp_path: Path) -> None:
    db_path = tmp_path / "event-cognitive-hashes.db"
    engine, session_factory = await _runtime(db_path)
    try:
        async with session_factory() as session:
            session.add(Board(id="board-hash", name="Hash", owner_id="owner-hash"))
            session.add(
                Spec(
                    id="spec-hash",
                    board_id="board-hash",
                    title="Hash parity spec",
                    context="Stable cognitive context",
                    created_by="agent-hash",
                )
            )
            session.add(
                Card(
                    id="card-hash",
                    board_id="board-hash",
                    spec_id="spec-hash",
                    title="Hash parity bug",
                    card_type="bug",
                    action_plan="A sufficiently detailed action plan for closeout replay.",
                    created_by="agent-hash",
                )
            )
            await session.commit()

        source_rows = CommunityBoardSourceReader(db_path).fetch("board-hash")
        spec_source = next(
            row for row in source_rows if row["source_ref"] == "spec:spec-hash"
        )
        bug_source = next(
            row for row in source_rows if row["source_ref"] == "bug:card-hash"
        )

        reader = CommunitySqlAlchemyDomainEventFactReader()
        async with session_factory() as session:
            spec_facts = await reader.load_cognitive_spec_facts(
                session, spec_id="spec-hash"
            )
            card_facts = await reader.load_cognitive_card_facts(
                session, card_id="card-hash"
            )

        assert spec_facts is not None
        assert card_facts is not None
        assert spec_facts.content_hash == spec_source["content_hash"]
        assert card_facts.content_hash == bug_source["content_hash"]
    finally:
        await engine.dispose()
