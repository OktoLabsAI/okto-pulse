"""Existing engine/session commits are savepoints; only the outer owner publishes."""

import asyncio
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.ports.relational_runtime import (
    configure_database_runtime, is_database_runtime_configured,
    reset_database_runtime_for_tests, resolve_database_runtime,
)
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.relational_schema_transaction import schema_transaction_runtime


@pytest.fixture(autouse=True)
def restore_runtime_binding():
    original = resolve_database_runtime() if is_database_runtime_configured() else None
    try:
        yield
    finally:
        if original is None:
            reset_database_runtime_for_tests()
        else:
            configure_database_runtime(runtime=original)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [False, True])
async def test_engine_and_session_writers_share_one_reservation_and_restore_binding(tmp_path, commit):
    path = tmp_path / "transaction.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    original = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    configure_database_runtime(runtime=original)
    try:
        async with engine.connect() as owner:
            async with schema_transaction_runtime(owner) as scoped:
                assert resolve_database_runtime() is scoped
                async with db.get_engine().begin() as writer:
                    await writer.exec_driver_sql("BEGIN IMMEDIATE")
                    await writer.exec_driver_sql("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
                    await writer.execute(text("INSERT INTO sample VALUES (1,'first')"))
                async with db.get_engine().connect() as writer:
                    await writer.execute(text("INSERT INTO sample VALUES (2,'discard')"))
                    await writer.rollback()
                    await writer.begin()
                    await writer.execute(text("INSERT INTO sample VALUES (2,'second')"))
                    await writer.commit()
                async with db.get_session_factory()() as session:
                    await session.execute(text("BEGIN IMMEDIATE"))
                    await session.execute(text("INSERT INTO sample VALUES (3,'third')"))
                    await session.commit()
                assert (await owner.exec_driver_sql("SELECT count(*) FROM sample")).scalar_one() == 3
                with sqlite3.connect(path, timeout=0) as competing:
                    assert not competing.execute("SELECT 1 FROM sqlite_schema WHERE name='sample'").fetchall()
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        competing.execute("BEGIN IMMEDIATE")
            assert resolve_database_runtime() is original
            await (owner.commit() if commit else owner.rollback())
        with sqlite3.connect(path) as reader:
            if commit:
                assert reader.execute("SELECT * FROM sample ORDER BY id").fetchall() == [(1, "first"), (2, "second"), (3, "third")]
            else:
                assert not reader.execute("SELECT 1 FROM sqlite_schema WHERE name='sample'").fetchall()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", ["COMMIT", "-- annotation\nCOMMIT;", "END TRANSACTION /* comment */;", "ROLLBACK", "sync-commit", "sync-rollback"])
async def test_writer_cannot_escape_outer_boundary(tmp_path, escape):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'escape.db'}")
    original = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    configure_database_runtime(runtime=original)
    try:
        async with engine.connect() as owner:
            with pytest.raises(RuntimeError, match="outer_boundary_forbidden"):
                async with schema_transaction_runtime(owner):
                    async with db.get_engine().begin() as writer:
                        await writer.exec_driver_sql("CREATE TABLE uncommitted (id TEXT)")
                        if escape == "sync-commit":
                            await writer.run_sync(lambda sync: sync.commit())
                        elif escape == "sync-rollback":
                            await writer.run_sync(lambda sync: sync.rollback())
                        else:
                            await writer.execute(text(escape))
            assert resolve_database_runtime() is original
            await owner.rollback()
            assert not (await owner.exec_driver_sql("SELECT 1 FROM sqlite_schema WHERE name='uncommitted'")).all()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_implicit_read_transaction_is_not_a_write_reservation(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'read.db'}")
    try:
        async with engine.connect() as owner:
            await owner.exec_driver_sql("SELECT 1")
            with pytest.raises(ValueError, match="fresh_sqlite_connection_required"):
                async with schema_transaction_runtime(owner):
                    pytest.fail("unreserved transaction accepted")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_swallowing_boundary_error_cannot_publish_or_reuse_native_transaction(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'swallowed.db'}")
    try:
        async with engine.connect() as owner:
            with pytest.raises(RuntimeError, match="outer_boundary_forbidden"):
                async with schema_transaction_runtime(owner):
                    await owner.exec_driver_sql("CREATE TABLE forbidden (id TEXT)")
                    try:
                        await owner.exec_driver_sql("COMMIT")
                    except RuntimeError:
                        pass
            assert owner.invalidated
            await owner.rollback()
        async with engine.connect() as reused:
            assert not (await reused.exec_driver_sql("SELECT 1 FROM sqlite_schema WHERE name='forbidden'")).all()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_exception_after_internal_commits_rolls_back_schema_and_rows(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'failure.db'}")
    try:
        with pytest.raises(RuntimeError, match="injected"):
            async with engine.connect() as owner:
                async with schema_transaction_runtime(owner):
                    async with db.get_engine().begin() as writer:
                        await writer.exec_driver_sql("CREATE TABLE incomplete (id TEXT)")
                    async with db.get_session_factory()() as session:
                        await session.execute(text("INSERT INTO incomplete VALUES ('retained')"))
                        await session.commit()
                    raise RuntimeError("injected")
        async with engine.connect() as reopened:
            assert not (await reopened.exec_driver_sql("SELECT 1 FROM sqlite_schema WHERE name='incomplete'")).all()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_setup_failure_does_not_leave_boundary_listeners(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import relational_schema_transaction as adapter
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup.db'}")
    def fail(*args):
        raise RuntimeError("injected setup")
    monkeypatch.setattr(adapter, "build_community_session_factory", fail)
    try:
        async with engine.connect() as owner:
            with pytest.raises(RuntimeError, match="injected setup"):
                async with schema_transaction_runtime(owner):
                    pytest.fail("failed setup yielded a runtime")
            await owner.rollback()
            await owner.exec_driver_sql("CREATE TABLE after_failure (id TEXT)")
            await owner.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_rolls_back_internal_commits_and_releases_writer(tmp_path):
    path = tmp_path / 'cancel.db'
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    entered = asyncio.Event()
    never = asyncio.Event()
    async def work():
        async with engine.connect() as owner:
            async with schema_transaction_runtime(owner):
                async with db.get_engine().begin() as writer:
                    await writer.exec_driver_sql("CREATE TABLE cancelled (id TEXT)")
                entered.set()
                await never.wait()
    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with sqlite3.connect(path, timeout=0) as writer:
            writer.execute("BEGIN IMMEDIATE")
            assert not writer.execute("SELECT 1 FROM sqlite_schema WHERE name='cancelled'").fetchall()
            writer.rollback()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_filesystem_cleanup_cannot_escape_the_relational_transaction(tmp_path):
    from okto_pulse.community.adapters.relational_schema_steps import _remove_known_fixture_graph_if_present
    data = tmp_path / 'home' / 'data'
    data.mkdir(parents=True)
    fixture = data.parent / 'boards' / 'sprint-crud-board-001'
    fixture.mkdir(parents=True)
    retained = fixture / 'retained.txt'
    retained.write_bytes(b'original graph artifact')
    engine = create_async_engine(f"sqlite+aiosqlite:///{data / 'pulse.db'}")
    try:
        async with engine.connect() as owner:
            with pytest.raises(RuntimeError, match="external_effect_requires_coordinator"):
                async with schema_transaction_runtime(owner):
                    async with db.get_engine().begin() as writer:
                        await writer.exec_driver_sql("CREATE TABLE unpublished (id TEXT)")
                    _remove_known_fixture_graph_if_present(db.get_engine())
            await owner.rollback()
            assert not (await owner.exec_driver_sql("SELECT 1 FROM sqlite_schema WHERE name='unpublished'")).all()
        assert retained.read_bytes() == b'original graph artifact'
    finally:
        await engine.dispose()
