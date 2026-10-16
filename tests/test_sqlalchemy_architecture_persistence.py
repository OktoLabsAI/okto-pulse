"""Focused transaction cleanup coverage for Architecture persistence."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory

from okto_pulse.community.adapters.sqlalchemy_models import ArchitectureDesign, Base, Board, Ideation

from okto_pulse.community.adapters.sqlalchemy_architecture_persistence import (
    CommunitySqlAlchemyArchitecturePersistence,
)
from okto_pulse.core.ports.architecture_persistence import ArchitectureRecord


class _RollbackContext:
    def __init__(self) -> None:
        self.rollback_calls = 0

    async def rollback(self) -> None:
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_rollback_clears_tracked_records() -> None:
    adapter = CommunitySqlAlchemyArchitecturePersistence()
    context = _RollbackContext()
    adapter._tracked[context] = [
        ArchitectureRecord(
            entity="architecture_design",
            values={"id": "design-1"},
        )
    ]

    await adapter.rollback(context)

    assert context.rollback_calls == 1
    assert context not in adapter._tracked


@pytest_asyncio.fixture
async def architecture_sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'architecture-fence.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = build_community_session_factory(engine)
        async with factory() as db:
            db.add(Board(id="board", name="Fence", owner_id="author", realm_id="local"))
            await db.flush()
            db.add(Ideation(id="idea", board_id="board", title="Idea", created_by="author"))
            await db.flush()
            db.add(ArchitectureDesign(
                id="design", board_id="board", parent_type="ideation", ideation_id="idea",
                title="Original", global_description="Concurrency fixture", created_by="author",
                version=1, entities=[], interfaces=[], diagrams=[],
            ))
            await db.commit()
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_version_fence_serializes_writers_and_rejects_stale_base(architecture_sessions):
    adapter = CommunitySqlAlchemyArchitecturePersistence()
    async with architecture_sessions() as first, architecture_sessions() as second:
        stale = await adapter.get(second, entity="architecture_design", record_id="design")
        assert stale.version == 1
        assert await adapter.fence_version(first, design_id="design", expected_version=1)
        await first.execute(update(ArchitectureDesign).where(ArchitectureDesign.id == "design").values(
            title="Winner", version=2,
        ))
        attempting = asyncio.Event()

        async def compete():
            attempting.set()
            return await adapter.fence_version(second, design_id="design", expected_version=stale.version)

        contender = asyncio.create_task(compete())
        try:
            await asyncio.wait_for(attempting.wait(), timeout=5)
            await first.commit()
            assert await asyncio.wait_for(contender, timeout=10) is False
        finally:
            if not contender.done():
                contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
            await adapter.rollback(second)
    async with architecture_sessions() as reader:
        row = await reader.get(ArchitectureDesign, "design")
        assert (row.title, row.version) == ("Winner", 2)


@pytest.mark.asyncio
async def test_fence_itself_preserves_version_timestamp_and_rollback(architecture_sessions):
    adapter = CommunitySqlAlchemyArchitecturePersistence()
    async with architecture_sessions() as db:
        before = (await db.execute(select(ArchitectureDesign.version, ArchitectureDesign.updated_at))).one()
        assert await adapter.fence_version(db, design_id="missing", expected_version=1) is False
        assert await adapter.fence_version(db, design_id="design", expected_version=2) is False
        assert await adapter.fence_version(db, design_id="design", expected_version=1)
        await db.commit()
        assert (await db.execute(select(ArchitectureDesign.version, ArchitectureDesign.updated_at))).one() == before
        assert await adapter.fence_version(db, design_id="design", expected_version=1)
        await db.execute(update(ArchitectureDesign).where(ArchitectureDesign.id == "design").values(
            title="Rolled back", version=2,
        ))
        await adapter.rollback(db)
    async with architecture_sessions() as reader:
        row = await reader.get(ArchitectureDesign, "design")
        assert (row.title, row.version, row.updated_at) == ("Original", 1, before.updated_at)
