"""Frozen storage values survive removal of Sprint enums from live Core."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.legacy_sprint_values import (
    HistoricalSprintLaneType, HistoricalSprintStatus,
)
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base, Board, HistoricalSprintLaneTypeType, HistoricalSprintStatusType, Spec, Sprint,
)


STATUS_VALUES = {"DRAFT": "draft", "ACTIVE": "active", "REVIEW": "review", "CLOSED": "closed", "CANCELLED": "cancelled"}
LANE_VALUES = {"NORMAL": "normal", "HOTFIX": "hotfix"}


@pytest.mark.parametrize("enum_type,expected", [(HistoricalSprintStatus, STATUS_VALUES), (HistoricalSprintLaneType, LANE_VALUES)])
def test_frozen_decoder_values(enum_type, expected):
    assert {member.name: member.value for member in enum_type} == expected
    assert enum_type.__module__ == "okto_pulse.community.adapters.legacy_sprint_values"


@pytest.mark.parametrize("codec", [HistoricalSprintStatusType(), HistoricalSprintLaneTypeType()])
def test_null_and_unknown_values_keep_previous_strict_read_contract(codec):
    assert codec.process_bind_param(None, None) is None
    assert codec.process_result_value(None, None) is None
    for invalid in ("", "unknown", "ACTIVE", "normal "):
        with pytest.raises(ValueError):
            codec.process_result_value(invalid, None)


@pytest.mark.asyncio
async def test_all_ten_historical_combinations_round_trip_without_changing_stored_strings(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    sessions = build_community_session_factory(engine)
    expected = [(f"old-{index}", status, lane) for index, (status, lane) in enumerate(
        (status, lane) for status in STATUS_VALUES.values() for lane in LANE_VALUES.values()
    )]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Board(id="board", name="History", owner_id="owner"))
            session.add(Spec(id="spec", board_id="board", title="Spec", created_by="owner"))
            session.add_all(Sprint(id=identity, board_id="board", spec_id="spec", title="Archived origin",
                                   status=status, lane_type=lane, created_by="owner") for identity, status, lane in expected)
            await session.commit()
        async with sessions() as session:
            rows = (await session.execute(select(Sprint).order_by(Sprint.id))).scalars().all()
            assert [(row.id, row.status.value, row.lane_type.value) for row in rows] == expected
            assert all(isinstance(row.status, HistoricalSprintStatus) and isinstance(row.lane_type, HistoricalSprintLaneType) for row in rows)
            assert list((await session.execute(text("SELECT id, status, lane_type FROM sprints ORDER BY id"))).all()) == expected
            await session.commit()
        async with engine.connect() as connection:
            assert list((await connection.execute(text("SELECT id, status, lane_type FROM sprints ORDER BY id"))).all()) == expected
    finally:
        await engine.dispose()
