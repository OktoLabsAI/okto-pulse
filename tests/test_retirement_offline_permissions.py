"""Permission cutover and its durable checkpoint form one SQL transaction."""

from dataclasses import asdict

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import sqlalchemy_database as db
from test_card_context_retirement import dump
from test_graph_binding_publication_window import child as binding_probe
from test_joint_recovery_window import _start as startup_probe
from test_permission_retirement_review_installation import seed
from test_retirement_data_journal import records
from test_retirement_offline_materialization import prepare, resume, setup
from test_retirement_offline_run import MIGRATION
from test_schema_lifecycle_reentry import probe as schema_probe
import test_sprint_retirement_inventory as relational

database = relational.database


async def permissions(args, graphs, run, runtime=None):
    return await offline.resume_offline_retirement_permissions(runtime or args[0], args[1], tuple(graphs), run,
        migration_builds=MIGRATION)


@pytest.mark.asyncio
@pytest.mark.parametrize("agents", [False, True])
async def test_permission_checkpoint_and_cold_replay_keep_startup_closed(database, tmp_path, monkeypatch, agents):
    engine, path = database
    if agents:
        await seed(engine)
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        real = offline.retire_permission_documents
        seen = []
        async def checked(*a, **kw):
            assert binding_probe(args[4]["kg_base_dir"]) == "blocked"
            assert schema_probe(db._schema_process_lock_path(args[0])) == "blocked"
            assert all(startup_probe(root) == "blocked" for root in args[4]["runtime_directories"])
            seen.append(True)
            return await real(*a, **kw)
        with monkeypatch.context() as scoped:
            scoped.setattr(offline, "retire_permission_documents", checked)
            result = await permissions(args, graphs, run)
        assert seen == [True]
        assert result["state"] == "permissions_retired"
        _, _, checkpoint, data, *_ = offline.read_offline_retirement_run(run)
        retained = await records(engine, data)
        assert len(retained) == 7 and retained[-1]["stage"] == "permissions"
        assert retained[-1]["payload"] == asdict(result["permission_cleanup"])
        assert result["permission_cleanup"].migration_id == checkpoint.migration_id
        before = dump(path)
        cold = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            runtime = db.CommunityDatabaseRuntime(cold, async_sessionmaker(cold))
            assert await permissions(args, graphs, run, runtime) == result
            assert dump(path) == before
            with pytest.raises(Exception, match="retirement_cutover_incomplete"):
                await offline.require_retirement_runtime_admission(cold)
        finally:
            await cold.dispose()
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["checkpoint", "policy", "outbox"])
async def test_late_failure_rolls_back_permission_cleanup_and_checkpoint(database, tmp_path, failure):
    engine, path = database
    await seed(engine)
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        await resume(args, graphs, run)
        timing, body, error = {
            "checkpoint": ("BEFORE", "SELECT RAISE(ABORT,'permission checkpoint failed')", "permission checkpoint failed"),
            "policy": ("AFTER", "UPDATE agents SET permission_flags='{}' WHERE id='ambiguous'", "cleanup_write_mismatch"),
            "outbox": ("AFTER", "UPDATE global_update_outbox SET retry_count=99 WHERE id='mixed'", "outbox_mismatch"),
        }[failure]
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE TRIGGER fail_permission_checkpoint {timing} INSERT ON "
                f"retirement_data_checkpoints WHEN NEW.ordinal=6 BEGIN {body}; END")
        before = dump(path)
        with pytest.raises(Exception, match=error):
            await permissions(args, graphs, run)
        assert dump(path) == before
        _, _, _, data, *_ = offline.read_offline_retirement_run(run)
        assert len(await records(engine, data)) == 6
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER fail_permission_checkpoint")
        assert (await permissions(args, graphs, run))["state"] == "permissions_retired"
        assert len(await records(engine, data)) == 7
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
async def test_lost_response_replays_original_permission_commit(database, tmp_path, monkeypatch):
    engine, path = database
    await seed(engine)
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        original = offline.retire_permission_documents
        committed = []
        async def lost(*a, **kw):
            committed.append(await original(*a, **kw))
            raise RuntimeError("lost after permission commit")
        with monkeypatch.context() as scoped:
            scoped.setattr(offline, "retire_permission_documents", lost)
            with pytest.raises(RuntimeError, match="lost after permission commit"):
                await permissions(args, graphs, run)
        before = dump(path)
        assert (await permissions(args, graphs, run))["permission_cleanup"] == committed[0]
        assert dump(path) == before
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["checkpoint", "cleanup"])
async def test_missing_coordinated_evidence_cannot_be_reconstructed(database, tmp_path, missing):
    engine, path = database
    await seed(engine)
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        await permissions(args, graphs, run)
        # Deliberate corruption of a disposable database, bypassing immutability.
        async with engine.begin() as connection:
            if missing == "checkpoint":
                guard = (await connection.exec_driver_sql("SELECT sql FROM sqlite_schema "
                    "WHERE name='retirement_data_no_delete'")).scalar_one()
                await connection.exec_driver_sql("DROP TRIGGER retirement_data_no_delete")
                await connection.exec_driver_sql("DELETE FROM retirement_data_checkpoints WHERE ordinal=6")
                await connection.exec_driver_sql(guard)
            else:
                await connection.exec_driver_sql("DELETE FROM permission_introduction_audit "
                    "WHERE phase='permission_retirement_cleanup'")
        before = dump(path)
        with pytest.raises(ValueError, match="checkpoint_replay_mismatch"):
            await permissions(args, graphs, run)
        assert dump(path) == before
    finally:
        for graph in graphs:
            graph.database.close()
