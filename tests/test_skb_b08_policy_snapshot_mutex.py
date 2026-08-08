"""SK-B/B08 board mutex shared by policy snapshots and subject writes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Spec


@pytest.mark.asyncio
async def test_subject_write_waits_for_policy_snapshot_board_mutex(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "b08-policy-mutex.db"
    database_module.create_database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with get_session_factory()() as seed:
        seed.add(Board(id="board-b08-mutex", name="B08", owner_id="owner"))
        seed.add(
            Spec(
                id="spec-b08-mutex",
                board_id="board-b08-mutex",
                title="Before",
                description="Policy subject.",
                created_by="owner",
            )
        )
        await seed.commit()

    writer_entered = asyncio.Event()

    async def mutate_subject() -> None:
        async with get_session_factory()() as writer:
            spec = await writer.get(Spec, "spec-b08-mutex")
            assert spec is not None
            spec.title = "After"
            writer_entered.set()
            await writer.commit()

    async with get_session_factory()() as snapshot_session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(snapshot_session)
        await adapter._lock_board(board_id="board-b08-mutex")  # noqa: SLF001

        writer_task = asyncio.create_task(mutate_subject())
        await writer_entered.wait()
        await asyncio.sleep(0.05)
        assert not writer_task.done()

        await snapshot_session.commit()
        await asyncio.wait_for(writer_task, timeout=2)

    async with get_session_factory()() as verification:
        persisted = await verification.get(Spec, "spec-b08-mutex")
        assert persisted is not None
        assert persisted.title == "After"
        assert persisted.version == 2
