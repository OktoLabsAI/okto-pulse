"""SK-B/B08 board mutex shared by policy snapshots and subject writes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.current_relational_schema import (
    initialize_current_schema, current_schema_contract,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
)
from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec


@pytest.mark.asyncio
async def test_subject_write_waits_for_policy_snapshot_board_mutex(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "b08-policy-mutex.db"
    database_module.create_database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await initialize_current_schema(get_engine(), current_schema_contract())

    async with get_session_factory()() as seed:
        seed.add(Board(realm_id="local", id="board-b08-mutex", name="B08", owner_id="owner"))
        seed.add(
            Spec(architecture_adoption=ArchitectureAdoptionScope(board_id="board-b08-mutex", spec_id="spec-b08-mutex", adopted_in_edition=1, actor_id="owner", inherited_resource_ids=()).model_dump(mode="json"),
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
            writer_entered.set()
            spec = await writer.get(Spec, "spec-b08-mutex")
            assert spec is not None
            spec.title = "After"
            await writer.commit()

    async with get_session_factory()() as snapshot_session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(snapshot_session)
        await adapter._lock_board(board_id="board-b08-mutex")  # noqa: SLF001

        writer_task = asyncio.create_task(mutate_subject())
        try:
            await asyncio.wait_for(writer_entered.wait(), timeout=2)
            await asyncio.sleep(0.05)
            if writer_task.done():
                await writer_task  # Surface writer failure instead of hiding it behind an event.
            assert not writer_task.done()

            await snapshot_session.commit()
            await asyncio.wait_for(writer_task, timeout=2)
        finally:
            if not writer_task.done():
                writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)

    async with get_session_factory()() as verification:
        persisted = await verification.get(Spec, "spec-b08-mutex")
        assert persisted is not None
        assert persisted.title == "After"
        assert persisted.version == 2
