"""R01B REPLAN-IMP1 (AC2) — Community UnitOfWork parity (commit/rollback/close).

Proves the Community ``CommunityUnitOfWork`` + factory mirror the core
``SQLAlchemyUnitOfWork`` semantics against a REAL SQLite database:

  - commit persists; read-after-write inside the same transaction (autoflush);
  - rollback discards a flushed-but-uncommitted row (the teardown invariant);
  - explicit rollback discards; the session always closes (one teardown path);
  - the unit of work satisfies the core ports (PulseUnitOfWork + the three
    repository Protocols), and realm_id/actor are carried-not-enforced.
"""

from __future__ import annotations

import asyncio
import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board as BoardRow
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
    build_community_unit_of_work_factory,
)
from okto_pulse.core.domain.entities import Board


@pytest.fixture
def _temp_session_factory(tmp_path):
    """Temp SQLite DB built entirely by Community relational adapters."""
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'r01b_uow.db'}"
    )
    session_factory = build_community_session_factory(engine)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    try:
        yield session_factory
    finally:
        asyncio.run(engine.dispose())


def _board(board_id: str) -> Board:
    return Board(id=board_id, name="R01B", owner_id="r01b-user", settings={})


def test_ac2_commit_persists_and_read_after_write(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-commit"

    async def drive():
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            # read-after-write inside the same transaction (autoflush=True)
            seen = await uow.boards.get(board_id)
            await uow.commit()
        # a FRESH session confirms the row was committed
        async with sf() as s:
            row = (
                await s.execute(select(BoardRow).where(BoardRow.id == board_id))
            ).scalar_one_or_none()
        return seen, row

    seen, row = asyncio.run(drive())
    assert seen is not None and seen.id == board_id  # read-after-write
    assert row is not None and row.name == "R01B"  # committed


def test_ac2_rollback_on_error_discards_flushed_row(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-rollback"

    async def drive():
        # An exception inside the context triggers __aexit__(exc) -> rollback+close;
        # __aexit__ returns None so the exception is NOT suppressed.
        with pytest.raises(RuntimeError):
            async with factory() as uow:
                await uow.boards.add(_board(board_id))
                await uow.boards.get(board_id)  # autoflush -> INSERT in the txn
                raise RuntimeError("boom")
        async with sf() as s:
            row = (
                await s.execute(select(BoardRow).where(BoardRow.id == board_id))
            ).scalar_one_or_none()
        return row

    assert asyncio.run(drive()) is None  # rolled back, never committed


def test_ac2_explicit_rollback_discards(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-explicit-rb"

    async def drive():
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            await uow.rollback()
        async with sf() as s:
            row = (
                await s.execute(select(BoardRow).where(BoardRow.id == board_id))
            ).scalar_one_or_none()
        return row

    assert asyncio.run(drive()) is None


def test_ac2_unit_of_work_satisfies_ports(_temp_session_factory):
    from okto_pulse.core.repositories.interfaces.repositories import (
        BoardRepository,
        IdeationRepository,
        SpecRepository,
    )
    from okto_pulse.core.repositories.interfaces.unit_of_work import PulseUnitOfWork

    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)

    async def drive():
        async with factory() as uow:
            checks = (
                isinstance(uow, PulseUnitOfWork),
                isinstance(uow.boards, BoardRepository),
                isinstance(uow.ideations, IdeationRepository),
                isinstance(uow.specs, SpecRepository),
                not hasattr(uow, "session"),
                uow.services is not None,
                uow.realm_id,
                uow.actor,
            )
            await uow.rollback()
        return checks

    is_uow, is_b, is_i, is_s, no_session, has_services, realm, actor = asyncio.run(
        drive()
    )
    assert is_uow and is_b and is_i and is_s and no_session and has_services
    # Community composition binds the legacy data set to RealmScope.local().
    assert realm == "local" and actor is None


def test_ac2_factory_carries_realm_and_actor(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)

    async def drive():
        async with factory(realm_id="realm-1", actor="actor-x") as uow:
            carried = (uow.realm_id, uow.actor)
            await uow.rollback()
        return carried

    realm, actor = asyncio.run(drive())
    assert realm == "realm-1" and actor == "actor-x"


def test_f02_exactly_one_commit_on_success(_temp_session_factory):
    async def drive():
        async with _temp_session_factory() as session:
            uow = CommunityUnitOfWork(session)
            calls = {"commit": 0, "rollback": 0}
            original_commit = uow.commit
            original_rollback = uow.rollback

            async def counted_commit():
                calls["commit"] += 1
                await original_commit()

            async def counted_rollback():
                calls["rollback"] += 1
                await original_rollback()

            uow.commit = counted_commit
            uow.rollback = counted_rollback
            async with uow:
                await uow.boards.add(_board("f02-one-commit"))
                await uow.commit()
            return calls

    assert asyncio.run(drive()) == {"commit": 1, "rollback": 0}


def test_f02_exactly_one_rollback_on_failure(_temp_session_factory):
    async def drive():
        async with _temp_session_factory() as session:
            uow = CommunityUnitOfWork(session)
            calls = {"commit": 0, "rollback": 0}
            original_commit = uow.commit
            original_rollback = uow.rollback

            async def counted_commit():
                calls["commit"] += 1
                await original_commit()

            async def counted_rollback():
                calls["rollback"] += 1
                await original_rollback()

            uow.commit = counted_commit
            uow.rollback = counted_rollback
            with pytest.raises(RuntimeError, match="modeled failure"):
                async with uow:
                    await uow.boards.add(_board("f02-one-rollback"))
                    raise RuntimeError("modeled failure")
            return calls

    assert asyncio.run(drive()) == {"commit": 0, "rollback": 1}


@pytest.mark.asyncio(loop_scope="function")
async def test_mcp_uow_hard_cancel_returns_connection_to_pool(tmp_path) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'r01b-uow-cancel.db'}"
    )
    factory = build_community_unit_of_work_factory(
        build_community_session_factory(engine)
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    entered = asyncio.Event()

    async def victim() -> None:
        async with factory() as uow:
            assert await uow.boards.get("missing-board") is None
            entered.set()
            await asyncio.sleep(30)

    try:
        task = asyncio.create_task(victim(), name="test.cancelled-mcp-uow")
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert engine.sync_engine.pool.checkedout() == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if engine.sync_engine.pool.checkedout() == 0:
                break
            await asyncio.sleep(0.05)
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        await engine.dispose()
