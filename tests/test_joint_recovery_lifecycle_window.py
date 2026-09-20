"""The same SQL lifecycle mutex spans a real paired recovery set and work."""

from contextlib import nullcontext
import asyncio
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import joint_recovery_snapshot as joint
from okto_pulse.community.adapters import sqlalchemy_database as db
import test_joint_recovery_snapshot as recovery
from test_joint_recovery_window import _start
from test_schema_lifecycle_reentry import probe

sources = recovery.sources
stored_sources = recovery.stored_sources


@pytest.mark.parametrize("fail", [False, True])
def test_lifecycle_and_startup_remain_excluded_from_capture_through_work(stored_sources, monkeypatch, fail):
    # The existing synchronous storage fixture uses asyncio.run. Drive our
    # coroutine after fixture setup rather than asking pytest-asyncio to reuse
    # the loop that fixture setup closed on Python 3.13.
    asyncio.run(_exercise_window(stored_sources, monkeypatch, fail))


async def _exercise_window(stored_sources, monkeypatch, fail):
    source, uploads, _, _ = stored_sources
    sql, graphs, backups, data, _ = source
    engine = create_async_engine(f"sqlite+aiosqlite:///{sql}")
    runtime = db.CommunityDatabaseRuntime(engine, async_sessionmaker(engine))
    schema_lock = db._schema_process_lock_path(runtime)
    capture = joint._capture_joint_recovery_snapshot
    observed = []
    def checked_capture(*args, **kwargs):
        assert probe(schema_lock) == "blocked"
        observed.append("capture")
        return capture(*args, **kwargs)
    monkeypatch.setattr(joint, "_capture_joint_recovery_snapshot", checked_capture)
    try:
        with pytest.raises(RuntimeError, match="body interrupted") if fail else nullcontext():
            async with joint.joint_recovery_lifecycle_window(runtime, graphs, backups, snapshot_id="capture",
                    builds=recovery.BUILDS, runtime_directories=(data, data / "kg"),
                    kg_base_dir=data / "kg", storage_root=uploads, max_seconds=120) as snapshot:
                assert observed == ["capture"]
                assert probe(schema_lock) == "blocked"
                assert [_start(root) for root in (data, data / "kg")] == ["blocked", "blocked"]
                assert joint.verify_joint_recovery_snapshot(snapshot)["format"] == "joint-recovery-snapshot/v4"
                with sqlite3.connect(sql, timeout=0.05) as connection:
                    connection.execute("UPDATE history SET payload=X'1122'")
                if fail:
                    raise RuntimeError("body interrupted")
        assert probe(schema_lock) == "entered"
        assert [_start(root) for root in (data, data / "kg")] == ["started", "started"]
        with sqlite3.connect(snapshot.directory / "relational" / "database.sqlite3") as original:
            assert original.execute("SELECT payload FROM history").fetchone() == (b"\x00\x0a\xff",)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_in_memory_runtime_cannot_claim_an_offline_file_backup(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    runtime = db.CommunityDatabaseRuntime(engine, async_sessionmaker(engine))
    try:
        with pytest.raises(ValueError, match="local_database_required"):
            async with joint.joint_recovery_lifecycle_window(runtime, (), tmp_path, snapshot_id="capture",
                    builds=recovery.BUILDS, runtime_directories=(tmp_path,),
                    kg_base_dir=tmp_path, storage_root=tmp_path):
                pytest.fail("memory database admitted as a durable source")
        assert list(tmp_path.iterdir()) == []
    finally:
        await engine.dispose()
