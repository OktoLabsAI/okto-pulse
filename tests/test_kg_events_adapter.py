"""Local First conformance for the KG live-event reader port."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.kg_events import (
    CommunityKGEventsReader,
    cancel_safe_community_session_scope,
    register_community_kg_events_reader,
)
from okto_pulse.community.adapters.sqlalchemy_repositories import GlobalUpdateOutbox
from okto_pulse.core.ports.kg_events import (
    KGEventsReaderPort,
    get_kg_events_reader_port,
    reset_kg_events_reader_port_for_tests,
)
from okto_pulse.core.ports.relational_runtime import Base


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
