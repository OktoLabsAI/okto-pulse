"""Local First conformance for the KG live-event reader port."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.kg_events import (
    CommunityKGEventsReader,
    cancel_safe_community_session_scope,
    register_community_kg_events_reader,
)
from okto_pulse.community.adapters.sqlalchemy_repositories import (
    ConsolidationQueue,
    GlobalUpdateOutbox,
)
from okto_pulse.core.ports.kg_events import (
    KGEventsReaderPort,
    get_kg_events_reader_port,
    reset_kg_events_reader_port_for_tests,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base


@pytest.mark.asyncio
async def test_community_reader_returns_outbox_events_and_queue_snapshot() -> None:
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        created_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        async with factory() as session:
            session.add(
                GlobalUpdateOutbox(
                    event_id="evt-community-reader",
                    board_id="board-community-reader",
                    session_id="session-community-reader",
                    event_type="kg.session.committed",
                    payload={"nodes": 3},
                    created_at=created_at,
                )
            )
            await session.commit()

        reader = CommunityKGEventsReader(factory)
        result = await reader.poll(
            board_id="board-community-reader",
            after=created_at - timedelta(minutes=1),
            limit=50,
        )

        assert isinstance(reader, KGEventsReaderPort)
        assert [event.event_id for event in result.events] == ["evt-community-reader"]
        assert result.events[0].created_at == created_at
        assert result.events[0].created_at is not None
        assert result.events[0].created_at.utcoffset() == timedelta(0)
        assert result.events[0].payload == {"nodes": 3}
        assert result.progress == {
            "pending": 0,
            "claimed": 0,
            "done": 0,
            "failed": 0,
            "paused": 0,
            "total": 0,
            "processed": 0,
        }
        replay = await reader.replay(
            board_id="board-community-reader",
            after=created_at - timedelta(minutes=1),
            limit=50,
        )
        assert [event.event_id for event in replay] == ["evt-community-reader"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_community_reader_pages_equal_timestamps_by_event_id() -> None:
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created_at = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all(
                [
                    GlobalUpdateOutbox(
                        event_id=event_id,
                        board_id="board-tied-events",
                        session_id=f"session-{event_id}",
                        event_type="kg.session.committed",
                        payload={},
                        created_at=created_at,
                    )
                    for event_id in ("event-c", "event-a", "event-b")
                ]
            )
            await session.commit()

        reader = CommunityKGEventsReader(factory)
        first = await reader.poll(
            board_id="board-tied-events",
            after=created_at - timedelta(seconds=1),
            limit=2,
        )
        second = await reader.poll(
            board_id="board-tied-events",
            after=created_at,
            after_event_id=first.events[-1].event_id,
            limit=2,
        )

        assert [event.event_id for event in first.events] == ["event-a", "event-b"]
        assert [event.event_id for event in second.events] == ["event-c"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_queue_snapshot_excludes_maintenance_coordinators() -> None:
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all(
                [
                    ConsolidationQueue(
                        id="queue-consolidate",
                        board_id="board-progress",
                        artifact_type="spec",
                        artifact_id="spec-1",
                        source="historical_backfill",
                        status="pending",
                        work_kind="consolidate",
                        generation=0,
                        payload={},
                    ),
                    ConsolidationQueue(
                        id="queue-maintenance",
                        board_id="board-progress",
                        artifact_type="board",
                        artifact_id="board-progress",
                        source="kg_tick",
                        status="pending",
                        work_kind="stale_sweep",
                        generation=0,
                        payload={"cursor": "", "budget": 50, "attempt": 0},
                    ),
                ]
            )
            await session.commit()
            queue_rows = (
                await session.execute(
                    select(ConsolidationQueue.id, ConsolidationQueue.work_kind)
                    .where(ConsolidationQueue.board_id == "board-progress")
                    .order_by(ConsolidationQueue.id)
                )
            ).all()
            assert queue_rows == [
                ("queue-consolidate", "consolidate"),
                ("queue-maintenance", "stale_sweep"),
            ]

        result = await CommunityKGEventsReader(factory).poll(
            board_id="board-progress",
            after=datetime.now(timezone.utc),
            limit=50,
        )

        assert result.progress == {
            "pending": 1,
            "claimed": 0,
            "done": 0,
            "failed": 0,
            "paused": 0,
            "total": 1,
            "processed": 0,
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_safe_community_scope_finishes_close_after_cancellation() -> None:
    exit_started = asyncio.Event()
    allow_exit = asyncio.Event()
    exit_finished = asyncio.Event()

    class _Scope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args) -> None:
            exit_started.set()
            await allow_exit.wait()
            exit_finished.set()

    async def _use_scope() -> None:
        async with cancel_safe_community_session_scope(lambda: _Scope()):
            return None

    task = asyncio.create_task(_use_scope())
    await asyncio.wait_for(exit_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    allow_exit.set()
    await asyncio.wait_for(exit_finished.wait(), timeout=2)


def test_registration_composes_the_public_core_port() -> None:
    reset_kg_events_reader_port_for_tests()
    reader = register_community_kg_events_reader(lambda: object())
    try:
        assert get_kg_events_reader_port() is reader
    finally:
        reset_kg_events_reader_port_for_tests()


def test_local_reader_does_not_reach_into_core_database_infrastructure() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "okto_pulse"
        / "community"
        / "adapters"
        / "kg_events.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "okto_pulse.core.infra.database" not in imported_modules
    assert "okto_pulse.core.models.db" not in imported_modules
