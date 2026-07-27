from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.realm_migration import backfill_local_realm
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_models import Board as BoardRow
from okto_pulse.community.adapters.sqlalchemy_realm_access import (
    CommunitySqlAlchemyRealmAccess,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    build_community_unit_of_work_factory,
)
from okto_pulse.core.domain.entities import Board, Ideation, Spec
from okto_pulse.core.domain.realm import RealmIsolationViolation, RealmScope


async def _database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'realm.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


@pytest.mark.asyncio
async def test_f03_two_realms_hide_known_ids_and_reject_cross_realm_writes(tmp_path):
    engine, sessions = await _database(tmp_path)
    factory = build_community_unit_of_work_factory(sessions)
    realm_a = RealmScope.tenant("realm-a")
    realm_b = RealmScope.tenant("realm-b")
    board = Board(id="known-board-id", name="A", owner_id="owner-a")

    async with factory(realm_scope=realm_a) as uow:
        await uow.boards.add(board)
        await uow.commit()

    async with factory(realm_scope=realm_b) as uow:
        assert await uow.boards.get(board.id) is None
        with pytest.raises(RealmIsolationViolation, match="tenant_resource_not_found"):
            await uow.ideations.add(
                Ideation(
                    id="cross-idea",
                    board_id=board.id,
                    title="Cross realm",
                    created_by="owner-b",
                )
            )
        with pytest.raises(RealmIsolationViolation, match="tenant_resource_not_found"):
            await uow.specs.add(
                Spec(
                    id="cross-spec",
                    board_id=board.id,
                    title="Cross realm",
                    created_by="owner-b",
                )
            )
        await uow.rollback()

    guard = CommunitySqlAlchemyRealmAccess()
    async with sessions() as session:
        for operation in ("read", "write", "event", "outbox", "worker", "kg"):
            with pytest.raises(
                RealmIsolationViolation,
                match="tenant_resource_not_found",
            ):
                await guard.require_board_access(
                    session,
                    scope=realm_b,
                    board_id=board.id,
                    operation=operation,
                )

    await engine.dispose()


@pytest.mark.asyncio
async def test_f03_local_backfill_is_idempotent_and_preserves_legacy_boards(tmp_path):
    engine, sessions = await _database(tmp_path)
    async with sessions() as session:
        session.add(
            BoardRow(
                id="legacy-board",
                name="Legacy",
                owner_id="local-user",
                realm_id=None,
            )
        )
        await session.commit()

    first = await backfill_local_realm(engine)
    second = await backfill_local_realm(engine)
    assert first.rows_backfilled == 1
    assert second.rows_backfilled == 0
    assert first.indexes == second.indexes

    async with sessions() as session:
        realm_id = await session.scalar(
            select(BoardRow.realm_id).where(BoardRow.id == "legacy-board")
        )
        indexes = {
            row[1]
            for row in (
                await session.execute(text("PRAGMA index_list(boards)"))
            ).all()
        }
    assert realm_id == "local"
    assert {"ix_boards_realm_id", "uq_boards_realm_id_id"} <= indexes

    factory = build_community_unit_of_work_factory(sessions)
    async with factory() as uow:
        assert uow.realm_scope == RealmScope.local()
        assert await uow.boards.get("legacy-board") is not None
        await uow.rollback()

    await engine.dispose()
