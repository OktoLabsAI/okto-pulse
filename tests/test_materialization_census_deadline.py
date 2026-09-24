"""Native SQL work must stop with the observation, not outlive its coroutine."""

import asyncio
import sqlite3
import time

import pytest
from sqlalchemy import event, text

from okto_pulse.community.adapters.materialization_health import CommunitySqlAlchemyMaterializationCensus
from okto_pulse.core.ports.materialization_health import CensusStatus, HealthProbeDeadline
from test_materialization_health_adapters import _database


@pytest.mark.asyncio
async def test_native_census_query_obeys_deadline_and_releases_connection(tmp_path):
    engine, factory = await _database(tmp_path, "native-census-deadline.db")
    injected = False

    @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
    def expensive_read(_connection, _cursor, statement, parameters, _context, _many):
        nonlocal injected
        if not injected and statement.lstrip().upper().startswith("SELECT"):
            injected = True
            return (
                "WITH RECURSIVE work(n) AS (SELECT 1 UNION ALL "
                "SELECT n+1 FROM work WHERE n < 20000000) SELECT sum(n) FROM work",
                (),
            )
        return statement, parameters

    try:
        started = time.monotonic()
        result = await CommunitySqlAlchemyMaterializationCensus(factory).snapshot(
            "board", generation="generation", deadline=HealthProbeDeadline(started + 0.08)
        )
        elapsed = time.monotonic() - started
        assert injected
        assert result.status == CensusStatus.UNAVAILABLE
        assert result.reason_code == "board_census_timeout"
        assert result.source_count is None
        assert elapsed < 0.7, f"native query outlived deadline: {elapsed:.3f}s"
        assert engine.sync_engine.pool.checkedout() == 0
        # A deadline must not poison a connection returned to the shared pool.
        async with factory() as session:
            assert (await session.execute(text("SELECT 42"))).scalar_one() == 42
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_census_lock_wait_is_bounded_without_changing_database(tmp_path):
    engine, factory = await _database(tmp_path, "census-lock.db")
    lock = sqlite3.connect(tmp_path / "census-lock.db")
    try:
        lock.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        result = await CommunitySqlAlchemyMaterializationCensus(factory).snapshot(
            "board", generation="generation", deadline=HealthProbeDeadline(started + 0.08)
        )
        assert time.monotonic() - started < 0.7
        assert result.status == CensusStatus.UNAVAILABLE
        assert result.source_count is None
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        lock.rollback()
        lock.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_successful_census_restores_native_settings_for_next_borrower(tmp_path):
    engine, factory = await _database(tmp_path, "census-pool-settings.db", pool_size=1, max_overflow=0)
    try:
        async with factory() as session:
            await session.execute(text("PRAGMA busy_timeout=1234"))
        # This checks pool restoration after a successful observation, not a
        # latency SLA. Keep the short deadline in the separate interruption test.
        deadline = HealthProbeDeadline(time.monotonic() + 5)
        result = await CommunitySqlAlchemyMaterializationCensus(factory).snapshot(
            "empty", generation="generation", deadline=deadline
        )
        assert result.status == CensusStatus.AVAILABLE, result.reason_code
        assert result.source_count == 0
        await asyncio.sleep(max(0, deadline.remaining_seconds(now=time.monotonic())) + 0.02)
        async with factory() as session:
            assert (await session.execute(text("PRAGMA busy_timeout"))).scalar_one() == 1234
            assert (await session.execute(text(
                "WITH RECURSIVE work(n) AS (SELECT 1 UNION ALL "
                "SELECT n+1 FROM work WHERE n < 5000) SELECT max(n) FROM work"
            ))).scalar_one() == 5000
    finally:
        await engine.dispose()
