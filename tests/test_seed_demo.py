"""Default seed idempotence and reveal-once durability."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_SRC = Path(__file__).parent.parent / "src"
WORKSPACE_ROOT = Path(__file__).parent.parent.parent
CORE_SRC = WORKSPACE_ROOT / "okto-pulse-core" / "src"

source_paths = [p for p in (REPO_SRC, CORE_SRC) if p.exists()]
for p in reversed(source_paths):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


async def _seed_factory(tmp_path, name: str):
    from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    await initialize_current_schema(engine, current_schema_contract())
    return engine, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("old_toggle", [None, "0", "1"])
async def test_seed_only_my_board_and_agent_is_idempotent(tmp_path, monkeypatch, old_toggle):
    from okto_pulse.community.seed import seed_community_defaults

    if old_toggle is None:
        monkeypatch.delenv("OKTO_PULSE_SKIP_DEMO_SEED", raising=False)
    else:
        monkeypatch.setenv("OKTO_PULSE_SKIP_DEMO_SEED", old_toggle)
    engine, factory = await _seed_factory(tmp_path, "fresh.db")
    try:
        async with factory() as db:
            first = await seed_community_defaults(db)
            assert first[0].name == "My Board"
            assert first[1].name == "Local Agent"
            assert await seed_community_defaults(db) is None
            assert (await db.execute(sa_text("SELECT name FROM boards"))).scalars().all() == ["My Board"]
            assert (await db.execute(sa_text("SELECT name FROM agents"))).scalars().all() == ["Local Agent"]
            for table in ("specs", "cards", "consolidation_audit"):
                assert (await db.execute(sa_text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0
            assert (await db.execute(sa_text("SELECT count(*) FROM agent_boards"))).scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_primary_commit_delivers_key_despite_cancellation(
    tmp_path, monkeypatch, caplog
):
    """Cancellation during commit drains through reveal before propagating."""
    from okto_pulse.community import seed as seed_mod

    engine, factory = await _seed_factory(tmp_path, "cancelled-seed.db")
    delivered = []
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()


    try:
        async with factory() as db:
            original_commit = db.commit

            async def delayed_commit():
                commit_started.set()
                await release_commit.wait()
                await original_commit()

            monkeypatch.setattr(db, "commit", delayed_commit)
            seed_task = asyncio.create_task(
                seed_mod.seed_community_defaults(
                    db,
                    on_primary_committed=lambda board, agent, api_key: delivered.append(
                        (board.id, agent.id, api_key)
                    ),
                )
            )
            await asyncio.wait_for(commit_started.wait(), timeout=10)
            seed_task.cancel()
            release_commit.set()
            with pytest.raises(asyncio.CancelledError):
                await seed_task

        async with factory() as db:
            board_count = (
                await db.execute(sa_text("SELECT COUNT(*) FROM boards"))
            ).scalar_one()
            agent_row = (
                await db.execute(
                    sa_text("SELECT api_key, api_key_hash FROM agents LIMIT 1")
                )
            ).mappings().one()

        assert board_count == 1
        assert len(delivered) == 1
        assert delivered[0][2].startswith("dash_")
        assert agent_row["api_key"].startswith("sha256:")
        assert agent_row["api_key_hash"]
        assert delivered[0][2] not in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_primary_commit_failure_does_not_deliver_key(
    tmp_path, monkeypatch
):
    """A non-durable primary seed must never expose its unusable credential."""
    from okto_pulse.community import seed as seed_mod

    engine, factory = await _seed_factory(tmp_path, "failed-primary.db")
    delivered = []


    try:
        async with factory() as db:
            async def failing_commit():
                raise RuntimeError("primary commit failed")

            monkeypatch.setattr(db, "commit", failing_commit)
            with pytest.raises(RuntimeError, match="primary commit failed"):
                await seed_mod.seed_community_defaults(
                    db,
                    on_primary_committed=lambda board, agent, api_key: delivered.append(
                        (board.id, agent.id, api_key)
                    ),
                )

        assert delivered == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_primary_commit_sink_failure_propagates(
    tmp_path, monkeypatch
):
    """A failed sink is fail-closed after durability."""
    from okto_pulse.community import seed as seed_mod

    engine, factory = await _seed_factory(tmp_path, "sink-failure.db")

    def failing_sink(_board, _agent, _api_key):
        raise RuntimeError("credential sink failed")


    try:
        async with factory() as db:
            with pytest.raises(RuntimeError, match="credential sink failed"):
                await seed_mod.seed_community_defaults(
                    db,
                    on_primary_committed=failing_sink,
                )

        async with factory() as db:
            counts = (
                await db.execute(
                    sa_text(
                        "SELECT "
                        "(SELECT COUNT(*) FROM boards) AS boards, "
                        "(SELECT COUNT(*) FROM agents) AS agents"
                    )
                )
            ).mappings().one()

        assert dict(counts) == {"boards": 1, "agents": 1}
    finally:
        await engine.dispose()
