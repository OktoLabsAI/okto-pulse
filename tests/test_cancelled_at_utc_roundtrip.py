from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, insert, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    Ideation,
    Refinement,
    Spec,
    Sprint,
    UTCDateTime,
)


def test_all_cancellable_entities_use_timezone_preserving_type():
    for model in (Ideation, Refinement, Spec, Sprint, Card):
        assert isinstance(model.__table__.c.cancelled_at.type, UTCDateTime)


def test_utc_datetime_roundtrip_normalizes_aware_and_legacy_naive_values():
    metadata = MetaData()
    timestamps = Table(
        "timestamps",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("cancelled_at", UTCDateTime(), nullable=False),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)

    local = datetime(
        2026,
        7,
        25,
        10,
        30,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    legacy_naive = datetime(2026, 7, 25, 14, 0)

    with engine.begin() as connection:
        connection.execute(
            insert(timestamps),
            [
                {"id": 1, "cancelled_at": local},
                {"id": 2, "cancelled_at": legacy_naive},
            ],
        )
        rows = connection.execute(
            select(timestamps).order_by(timestamps.c.id)
        ).mappings().all()

    assert rows[0]["cancelled_at"] == datetime(
        2026,
        7,
        25,
        13,
        30,
        tzinfo=timezone.utc,
    )
    assert rows[1]["cancelled_at"] == legacy_naive.replace(tzinfo=timezone.utc)
