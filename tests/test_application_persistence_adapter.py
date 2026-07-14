from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    ApplicationQuery,
    ApplicationRecord,
)
from okto_pulse.core.domain.realm import RealmScope


@pytest.mark.asyncio
async def test_application_persistence_round_trip_includes_and_rollback(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'application.db'}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())
    card_id = str(uuid.uuid4())

    async with factory() as session:
        board = ApplicationRecord(
            entity="board",
            values={
                "id": board_id,
                "name": "Application boundary",
                "owner_id": "owner-1",
                "projection_only": "must not reach the ORM constructor",
            },
        )
        await adapter.add(session, board)
        await adapter.add(
            session,
            ApplicationRecord(
                entity="card",
                values={
                    "id": card_id,
                    "board_id": board_id,
                    "title": "Detached card",
                    "created_by": "owner-1",
                },
            ),
        )
        await adapter.commit(session)

    async with factory() as session:
        rows = await adapter.list(
            session,
            ApplicationQuery(
                entity="board",
                filters=(ApplicationFilter("id", "eq", board_id),),
                includes=("cards",),
            ),
        )
        assert len(rows) == 1
        loaded = rows[0]
        assert [card.id for card in loaded.cards] == [card_id]

        loaded.name = "Committed name"
        await adapter.commit(session)

    async with factory() as session:
        loaded = await adapter.get(session, entity="board", record_id=board_id)
        assert loaded is not None
        assert loaded.name == "Committed name"

        loaded.name = "Rolled back name"
        await adapter.rollback(session)

    async with factory() as session:
        loaded = await adapter.get(session, entity="board", record_id=board_id)
        assert loaded is not None
        assert loaded.name == "Committed name"

    await engine.dispose()


@pytest.mark.asyncio
async def test_application_persistence_synchronizes_legacy_direct_commit(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct-commit.db'}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())

    async with factory() as session:
        await adapter.add(
            session,
            ApplicationRecord(
                entity="board",
                values={
                    "id": board_id,
                    "name": "Before direct commit",
                    "owner_id": "owner-1",
                },
            ),
        )
        await adapter.commit(session)

    async with factory() as session:
        loaded = await adapter.get(session, entity="board", record_id=board_id)
        assert loaded is not None
        loaded.name = "After direct commit"
        await session.commit()

    async with factory() as session:
        reloaded = await adapter.get(session, entity="board", record_id=board_id)
        assert reloaded is not None
        assert reloaded.name == "After direct commit"

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_seen_item_is_board_scoped(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seen-scope.db'}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())
    seen_id = str(uuid.uuid4())
    async with factory() as session:
        await adapter.add(
            session,
            ApplicationRecord(
                entity="board",
                values={
                    "id": board_id,
                    "name": "Seen scope",
                    "owner_id": "owner-seen",
                },
            ),
        )
        await adapter.add(
            session,
            ApplicationRecord(
                entity="agent_seen_item",
                values={
                    "id": seen_id,
                    "board_id": board_id,
                    "agent_id": "agent-seen",
                    "item_type": "mention",
                    "item_id": "comment-seen",
                },
            ),
        )
        await adapter.commit(session)

    async with factory() as session:
        rows = await adapter.list(
            session,
            ApplicationQuery(
                entity="agent_seen_item",
                filters=(ApplicationFilter("agent_id", "eq", "agent-seen"),),
            ),
        )
        assert [row.id for row in rows] == [seen_id]
        assert rows[0].board_id == board_id

    await engine.dispose()
