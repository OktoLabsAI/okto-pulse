from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_analytics_read import (
    CommunitySqlAlchemyAnalyticsReader,
)
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.api.analytics import _parse_date
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.analytics_read import AnalyticsFilter, AnalyticsQuery
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    ApplicationQuery,
    ApplicationRecord,
)


async def _runtime(tmp_path, name: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


def test_rest_adapter_uses_the_core_half_open_date_contract():
    assert _parse_date("2026-07-14") == datetime(
        2026, 7, 14, tzinfo=timezone.utc
    )
    assert _parse_date("2026-07-14", end_of_day=True) == datetime(
        2026, 7, 15, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_activity_keyset_is_exclusive_for_real_sqlite_server_defaults(tmp_path):
    engine, factory = await _runtime(tmp_path, "activity-cursor.db")
    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = "11111111-1111-1111-1111-111111111111"
    try:
        async with factory() as session:
            await adapter.add(
                session,
                ApplicationRecord(
                    entity="board",
                    values={
                        "id": board_id,
                        "name": "Server default activity",
                        "owner_id": "owner",
                    },
                ),
            )
            for index in range(1, 6):
                await adapter.add(
                    session,
                    ApplicationRecord(
                        entity="activity_log",
                        values={
                            "id": f"row-{index}",
                            "board_id": board_id,
                            "card_id": None,
                            "action": "card_moved",
                            "actor_type": "agent",
                            "actor_id": "agent",
                            "actor_name": "Agent",
                            "details": {},
                            # Deliberately omit created_at: exercise SQLite's
                            # real CURRENT_TIMESTAMP server_default.
                        },
                    ),
                )
            await adapter.commit(session)

        async with factory() as session:
            first = await adapter.list(
                session,
                ApplicationQuery(
                    entity="activity_log",
                    filters=(ApplicationFilter("board_id", "eq", board_id),),
                    order_by=(("created_at", True), ("id", True)),
                    limit=2,
                ),
            )
            assert len({row.created_at for row in first}) == 1
            anchor = first[-1]
            second = await adapter.list(
                session,
                ApplicationQuery(
                    entity="activity_log",
                    filters=(ApplicationFilter("board_id", "eq", board_id),),
                    any_groups=(
                        (ApplicationFilter("created_at", "lt", anchor.created_at),),
                        (
                            ApplicationFilter("created_at", "eq", anchor.created_at),
                            ApplicationFilter("id", "lt", anchor.id),
                        ),
                    ),
                    order_by=(("created_at", True), ("id", True)),
                    limit=10,
                ),
            )

        first_ids = {row.id for row in first}
        second_ids = {row.id for row in second}
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == {f"row-{index}" for index in range(1, 6)}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_analytics_half_open_day_uses_sqlite_temporal_normalization(tmp_path):
    engine, factory = await _runtime(tmp_path, "analytics-day.db")
    reader = CommunitySqlAlchemyAnalyticsReader()
    try:
        async with factory() as session:
            for row_id, occurred in (
                ("before", "2026-07-13 23:59:59"),
                ("start", "2026-07-14 00:00:00"),
                ("middle", "2026-07-14 12:34:56"),
                ("after", "2026-07-15 00:00:00"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO activity_logs "
                        "(id, board_id, action, actor_type, actor_id, actor_name, "
                        "details, created_at) VALUES "
                        "(:id, :board, 'card_moved', 'agent', 'agent', 'Agent', "
                        "'{}', :created_at)"
                    ),
                    {
                        "id": row_id,
                        "board": "board-day",
                        "created_at": occurred,
                    },
                )
            await session.commit()

        async with factory() as session:
            rows = await reader.list(
                session,
                AnalyticsQuery(
                    entity="activity_log",
                    filters=(
                        AnalyticsFilter(
                            "created_at",
                            "gte",
                            datetime(2026, 7, 14, tzinfo=timezone.utc),
                        ),
                        AnalyticsFilter(
                            "created_at",
                            "lt",
                            datetime(2026, 7, 15, tzinfo=timezone.utc),
                        ),
                    ),
                    order_by="created_at",
                ),
            )
        assert [row.id for row in rows] == ["start", "middle"]
    finally:
        await engine.dispose()
