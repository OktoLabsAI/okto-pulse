"""Schema ownership follows a live task, never its copied context alone."""

import asyncio
import subprocess
import sys

from filelock import FileLock
import pytest

from okto_pulse.core.ports import DataBootstrapResult, MigrationResult
from okto_pulse.core.ports import relational_runtime as core_runtime
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle
from test_sqlalchemy_database_lifecycle_lock import _Runtime


def probe(path):
    script = """
import sys
from filelock import FileLock, Timeout
try:
    with FileLock(sys.argv[1], timeout=0.05):
        print('entered')
except Timeout:
    print('blocked')
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True,
        text=True, timeout=15, check=True)
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_same_task_nests_without_releasing_the_outer_process_mutex(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path / "source.sqlite")
    lock_path = db._schema_process_lock_path(runtime)
    monkeypatch.setattr(db, "_SCHEMA_PROCESS_LOCK_TIMEOUT_S", 0.05)
    async with db._serialized_schema_lifecycle(runtime):
        async with db._serialized_schema_lifecycle(runtime):
            assert probe(lock_path) == "blocked"
        assert probe(lock_path) == "blocked"
    assert probe(lock_path) == "entered"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_release", [False, True])
async def test_child_task_cannot_inherit_live_or_expired_lock_ownership(tmp_path, monkeypatch, after_release):
    runtime = _Runtime(tmp_path / "source.sqlite")
    monkeypatch.setattr(db, "_SCHEMA_PROCESS_LOCK_TIMEOUT_S", 0.05)
    start = asyncio.Event()
    async def child():
        await start.wait()
        with pytest.raises(db.CommunitySchemaLifecycleLockTimeout):
            async with db._serialized_schema_lifecycle(runtime):
                pytest.fail("a copied ContextVar is not ownership")
    async with db._serialized_schema_lifecycle(runtime):
        task = asyncio.create_task(child())
        if not after_release:
            start.set()
            await asyncio.wait_for(task, 2)
    if after_release:
        with FileLock(str(db._schema_process_lock_path(runtime)), timeout=0):
            start.set()
            await asyncio.wait_for(task, 2)
    assert probe(db._schema_process_lock_path(runtime)) == "entered"


@pytest.mark.asyncio
async def test_other_database_does_not_borrow_current_database_ownership(tmp_path, monkeypatch):
    first, second = (_Runtime(tmp_path / name) for name in ("first.sqlite", "second.sqlite"))
    monkeypatch.setattr(db, "_SCHEMA_PROCESS_LOCK_TIMEOUT_S", 0.05)
    async with db._serialized_schema_lifecycle(first):
        with FileLock(str(db._schema_process_lock_path(second)), timeout=0):
            with pytest.raises(db.CommunitySchemaLifecycleLockTimeout):
                async with db._serialized_schema_lifecycle(second):
                    pytest.fail("different database admitted without its lock")
        assert probe(db._schema_process_lock_path(first)) == "blocked"


@pytest.mark.asyncio
async def test_cancellation_releases_the_mutex_and_invalidates_task_ownership(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path / "source.sqlite")
    monkeypatch.setattr(db, "_SCHEMA_PROCESS_LOCK_TIMEOUT_S", 0.05)
    entered = asyncio.Event()
    async def owner():
        async with db._serialized_schema_lifecycle(runtime):
            async with db._serialized_schema_lifecycle(runtime):
                entered.set()
                await asyncio.Event().wait()
    task = asyncio.create_task(owner())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with db._serialized_schema_lifecycle(runtime):
        assert probe(db._schema_process_lock_path(runtime)) == "blocked"
    assert probe(db._schema_process_lock_path(runtime)) == "entered"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["concrete", "core", "community"])
async def test_all_production_lifecycle_paths_keep_the_mutex_through_schema_and_seeds(tmp_path, monkeypatch, path):
    runtime = db.configure_community_database(f"sqlite+aiosqlite:///{tmp_path / 'source.sqlite'}")
    monkeypatch.setattr(db, "_SCHEMA_PROCESS_LOCK_TIMEOUT_S", 0.05)
    orchestrator = register_community_relational_schema_lifecycle()
    observed = []
    async def schema(_plan):
        assert probe(db._schema_process_lock_path(runtime)) == "blocked"
        observed.append("schema")
        return MigrationResult(status="success")
    async def seeds(_plan):
        assert probe(db._schema_process_lock_path(runtime)) == "blocked"
        observed.append("seeds")
        return DataBootstrapResult(status="success")
    monkeypatch.setattr(orchestrator._migrator, "aexecute", schema)
    monkeypatch.setattr(orchestrator._bootstrapper, "aexecute", seeds)
    try:
        await {"concrete": orchestrator.initialize_schema, "core": core_runtime.init_db, "community": db.init_db}[path]()
        assert observed == ["schema", "seeds"]
        assert probe(db._schema_process_lock_path(runtime)) == "entered"
    finally:
        await runtime.engine.dispose()
