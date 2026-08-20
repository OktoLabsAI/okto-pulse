from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import SprintActivationBaseline
from okto_pulse.community.adapters.sqlalchemy_sprint_activation_baseline import (
    CommunitySqlAlchemySprintActivationBaselineStore,
)
from okto_pulse.core.ports.sprint_activation_baseline import (
    SprintActivationBaseline as Baseline,
    SprintActivationMember,
)


def _baseline() -> Baseline:
    return Baseline(
        board_id="board-1",
        sprint_id="sprint-1",
        spec_id="spec-1",
        sprint_version=2,
        activated_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        activated_by="user-1",
        members=(SprintActivationMember("card-1", "task", 3),),
    )


@pytest.mark.asyncio
async def test_store_flushes_without_committing_and_round_trips(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'activation-baseline.db'}"
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SprintActivationBaseline.__table__.create)
    store = CommunitySqlAlchemySprintActivationBaselineStore()
    baseline = _baseline()

    async with sessions() as session:
        assert await store.save_if_absent(session, baseline) == baseline
        assert (
            await store.get(session, board_id="board-1", sprint_id="sprint-1")
            == baseline
        )
        await session.rollback()

    async with sessions() as session:
        assert (
            await store.get(session, board_id="board-1", sprint_id="sprint-1") is None
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_commit_is_idempotent_and_preserves_first_baseline(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'activation-idempotent.db'}"
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SprintActivationBaseline.__table__.create)
    store = CommunitySqlAlchemySprintActivationBaselineStore()
    baseline = _baseline()

    async with sessions() as session:
        await store.save_if_absent(session, baseline)
        await session.commit()
    async with sessions() as session:
        assert await store.save_if_absent(session, baseline) == baseline
        await session.commit()
    async with sessions() as session:
        assert (
            await store.get(session, board_id="board-1", sprint_id="sprint-1")
            == baseline
        )
    await engine.dispose()
