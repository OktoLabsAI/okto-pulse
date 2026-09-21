"""Physical cutover, restoration of connection enforcement and cold replay."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.retirement_schema_storage import RETIRED_TABLES
from test_card_context_retirement import dump
from test_retirement_data_journal import records
from test_retirement_offline_materialization import prepare, setup
from test_retirement_offline_permissions import permissions
from test_retirement_offline_run import MIGRATION
import test_sprint_retirement_inventory as relational

database = relational.database


async def schema(args, graphs, run, runtime=None):
    return await offline.resume_offline_retirement_schema(runtime or args[0], args[1], tuple(graphs), run,
        migration_builds=MIGRATION)


async def settings(engine):
    async with engine.connect() as connection:
        return tuple([(await connection.exec_driver_sql(f"PRAGMA {name}")).scalar_one()
            for name in ("foreign_keys", "legacy_alter_table")])


@pytest.mark.asyncio
async def test_physical_cut_and_lost_response_resume_keep_startup_closed(database, tmp_path, monkeypatch):
    engine, path = database
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        async with engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        before_settings = await settings(engine)
        assert before_settings == (1, 0)
        original = offline.retire_schema
        committed = []
        async def lost(*a, **kw):
            committed.append(await original(*a, **kw))
            raise RuntimeError("lost after schema commit")
        with monkeypatch.context() as scoped:
            scoped.setattr(offline, "retire_schema", lost)
            with pytest.raises(RuntimeError, match="lost after schema commit"):
                await schema(args, graphs, run)
        assert await settings(engine) == before_settings
        async with engine.connect() as connection:
            tables = set((await connection.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table'")).scalars())
            assert not tables & set(RETIRED_TABLES)
            assert "sprint_id" not in [row[1] for row in await connection.exec_driver_sql("PRAGMA table_info(cards)")]
            assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
        _, _, _, data, *_ = offline.read_offline_retirement_run(run)
        assert len(await records(engine, data)) == 8
        before = dump(path)
        reopened = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            runtime = db.CommunityDatabaseRuntime(reopened, async_sessionmaker(reopened))
            result = await schema(args, graphs, run, runtime)
            assert result["state"] == "schema_retired" and result["schema"] == committed[0]
            assert dump(path) == before
            with pytest.raises(Exception, match="retirement_cutover_incomplete"):
                await offline.require_retirement_runtime_admission(reopened)
            # Losing the terminal step cannot turn a physically changed schema
            # into a fresh migration or authorize reconstruction of its receipt.
            async with reopened.begin() as connection:
                guard = (await connection.exec_driver_sql("SELECT sql FROM sqlite_schema "
                    "WHERE name='retirement_data_no_delete'")).scalar_one()
                await connection.exec_driver_sql("DROP TRIGGER retirement_data_no_delete")
                await connection.exec_driver_sql("DELETE FROM retirement_data_checkpoints WHERE ordinal=7")
                await connection.exec_driver_sql(guard)
            damaged = dump(path)
            with pytest.raises(ValueError, match="retirement_schema_source_mismatch"):
                await schema(args, graphs, run, runtime)
            assert dump(path) == damaged
        finally:
            await reopened.dispose()
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["abort", "surviving_data"])
async def test_checkpoint_failure_restores_schema_data_and_connection_settings(database, tmp_path, damage):
    engine, path = database
    args, graphs = await setup(engine, tmp_path, global_present=False, other_board_graph=False)
    try:
        run = await prepare(args, graphs)
        await permissions(args, graphs, run)
        timing, action, error = {
            "abort": ("BEFORE", "SELECT RAISE(ABORT,'schema checkpoint failed')", "schema checkpoint failed"),
            "surviving_data": ("AFTER", "UPDATE specs SET title='unapproved' WHERE id='spec-a'", "completion_mismatch"),
        }[damage]
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE TRIGGER fail_schema_checkpoint {timing} INSERT ON "
                f"retirement_data_checkpoints WHEN NEW.ordinal=7 BEGIN {action}; END")
        async with engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        before_settings, before = await settings(engine), dump(path)
        with pytest.raises(Exception, match=error):
            await schema(args, graphs, run)
        assert dump(path) == before and await settings(engine) == before_settings
        _, _, _, data, *_ = offline.read_offline_retirement_run(run)
        assert len(await records(engine, data)) == 7
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER fail_schema_checkpoint")
        assert (await schema(args, graphs, run))["state"] == "schema_retired"
    finally:
        for graph in graphs:
            graph.database.close()
