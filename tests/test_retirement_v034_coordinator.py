"""Actual predecessor schema through offline cut and cold replay, not runtime readiness."""

import json

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.core.ports.permission_retirement import retired_feature_permission_flags
from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.joint_recovery_snapshot import RecoveryBuildPair
from okto_pulse.community.adapters.retirement_schema_storage import RETIRED_TABLES
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters.relational_schema_transaction import schema_transaction_runtime
from okto_pulse.community.adapters.relational_schema_lifecycle import CommunityRelationalSchemaLifecycleOrchestrator
from okto_pulse.community.adapters.relational_schema_migrator import make_community_relational_schema_migrator
from okto_pulse.community.adapters.data_bootstrapper import make_community_data_bootstrapper
from okto_pulse.community.adapters.permission_retirement_checkpoint import read_permission_retirement_checkpoint
from okto_pulse.community.adapters.permission_retirement_cleanup import _load_layers
from okto_pulse.community.adapters.permission_retirement_review_installation import _require_loaded_parity
from test_permission_retirement_review_installation import seed
from test_retirement_bootstrap_convergence import lifecycle
from test_retirement_v034_cards import dump
from test_retirement_v034_source import FIXTURES, restore_source


@pytest.mark.asyncio
@pytest.mark.parametrize("with_authority", [False, True])
@pytest.mark.parametrize("transactional", [False, True])
async def test_real_predecessor_reaches_schema_retired_with_original_backup_and_cold_replay(tmp_path, monkeypatch, with_authority, transactional):
    path = restore_source(tmp_path)
    metadata = json.loads((FIXTURES / "f2_v034_source.json").read_text(encoding="utf-8"))["source_builds"]
    source = RecoveryBuildPair(metadata["core"]["commit"], metadata["community"]["commit"],
        metadata["core"]["wheel_sha256"], metadata["community"]["wheel_sha256"])
    # This is an adapter integration test; synthetic target identifiers do not
    # claim an installed/published paired-runtime E2E or an operational rollback.
    target = RecoveryBuildPair("e" * 40, "f" * 40, "1" * 64, "2" * 64)
    uploads, backups, kg = (tmp_path / name for name in ("uploads", "backups", "kg"))
    for directory in (uploads, backups, kg):
        directory.mkdir()
    storage = CommunityFileSystemStorage(str(uploads))
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    runtime = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    try:
        if with_authority:
            # Extend only this copy with explicit denials, malformed old flags,
            # inactive identities and preset lineage; the frozen DB is intact.
            await seed(engine)
        original = dump(path)
        run = await offline.prepare_offline_retirement_run(runtime, storage, (), backups, tmp_path / "run",
            snapshot_id="original", plan=ContextDispositionPlan(migration_id="original-v034",
                decision_reference="fixture has no substantive context candidates", decisions=()),
            source_builds=source, migration_builds=target,
            runtime_directories=(tmp_path, kg), kg_base_dir=kg)
        _, _, permission, _, backup, _ = offline.read_offline_retirement_run(run)
        assert dump(backup.directory / "relational" / "database.sqlite3") == original
        result = await offline.resume_offline_retirement_schema(runtime, storage, (), run, migration_builds=target)
        assert result["state"] == "schema_retired"
        async with engine.connect() as connection:
            tables = set((await connection.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table'")).scalars())
            assert not tables & set(RETIRED_TABLES)
            assert "sprint_id" not in [row[1] for row in await connection.exec_driver_sql("PRAGMA table_info(cards)")]
            assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
    finally:
        await runtime.close()
    stable = dump(path)
    reopened = create_async_engine(f"sqlite+aiosqlite:///{path}")
    cold = db.CommunityDatabaseRuntime(reopened, db.build_community_session_factory(reopened))
    try:
        replay = await offline.resume_offline_retirement_schema(cold, storage, (), run, migration_builds=target)
        assert replay == result
        assert dump(path) == stable
        assert dump(backup.directory / "relational" / "database.sqlite3") == original
        with pytest.raises(Exception, match="retirement_cutover_incomplete"):
            await offline.require_retirement_runtime_admission(reopened)
        # Investigation of the real lifecycle writers, not runtime admission.
        # A terminal receipt must not approve a bootstrap that changes any
        # surviving decision or clears captured owner-review classification.
        if transactional:
            before_bootstrap = dump(path)
            from okto_pulse.community.adapters import data_bootstrap_steps
            original_import = data_bootstrap_steps.DATA_BOOTSTRAP_STEP_CALLABLES['_bootstrap_quality_assessment_legacy_import_v1']
            async def fail_after_import():
                await original_import()
                raise RuntimeError("injected after real legacy import")
            with monkeypatch.context() as scoped:
                scoped.setitem(data_bootstrap_steps.DATA_BOOTSTRAP_STEP_CALLABLES, "_bootstrap_quality_assessment_legacy_import_v1", fail_after_import)
                with pytest.raises(Exception, match="injected after real legacy import"):
                    async with reopened.connect() as connection:
                        async with schema_transaction_runtime(connection):
                            orchestrator = CommunityRelationalSchemaLifecycleOrchestrator(
                                migrator=make_community_relational_schema_migrator(),
                                bootstrapper=make_community_data_bootstrapper())
                            await orchestrator.initialize_schema()
            assert dump(path) == before_bootstrap
            # Ordinary writers commit internally. Neither schema nor seeds can
            # become visible to another reader until the outer owner commits.
            for commit in (False, True):
                async with reopened.connect() as connection:
                    async with schema_transaction_runtime(connection):
                        orchestrator = CommunityRelationalSchemaLifecycleOrchestrator(
                            migrator=make_community_relational_schema_migrator(),
                            bootstrapper=make_community_data_bootstrapper())
                        await orchestrator.initialize_schema()
                        assert dump(path) == before_bootstrap
                    if commit:
                        await connection.commit()
                    else:
                        await connection.rollback()
                if not commit:
                    assert dump(path) == before_bootstrap
        else:
            await lifecycle(cold)
        async with reopened.connect() as connection:
            await connection.exec_driver_sql("BEGIN")
            contexts = await read_permission_retirement_checkpoint(connection, permission)
            if with_authority:
                assert contexts
            _require_loaded_parity(await _load_layers(connection), contexts,
                retired_flags=retired_feature_permission_flags())
        with pytest.raises(Exception, match="retirement_cutover_incomplete"):
            await offline.require_retirement_runtime_admission(reopened)
    finally:
        await cold.close()
