"""Frozen v034 through an atomic bootstrap receipt and cold, effect-free replay."""

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import retirement_bootstrap as bootstrap
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from test_retirement_offline_run import SOURCE, MIGRATION
from test_retirement_v034_cards import dump
from test_retirement_v034_source import restore_source


async def prepare(tmp_path):
    path = restore_source(tmp_path)
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    runtime = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    for name in ('uploads', 'backups', 'kg'):
        (tmp_path / name).mkdir()
    storage = CommunityFileSystemStorage(str(tmp_path / 'uploads'))
    try:
        # Build IDs are synthetic test bindings; this is not published-pair E2E.
        run = await offline.prepare_offline_retirement_run(runtime, storage, (), tmp_path / 'backups', tmp_path / 'run',
            snapshot_id='original', plan=ContextDispositionPlan(migration_id='bootstrap-v034',
                decision_reference='fixture without substantive context', decisions=()),
            source_builds=SOURCE, migration_builds=MIGRATION,
            runtime_directories=(tmp_path, tmp_path / 'kg'), kg_base_dir=tmp_path / 'kg')
        return runtime, storage, run, path
    except BaseException:
        await runtime.close()
        raise


async def resume(runtime, storage, run):
    return await offline.resume_offline_retirement_bootstrap(runtime, storage, (), run, migration_builds=MIGRATION)


@pytest.mark.asyncio
async def test_lost_bootstrap_response_replays_without_repeating_writers(tmp_path, monkeypatch):
    runtime, storage, run, path = await prepare(tmp_path)
    original = offline.complete_retirement_bootstrap
    committed = []
    async def lose_response(*args, **kwargs):
        committed.append(await original(*args, **kwargs))
        raise RuntimeError('lost after bootstrap commit')
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(offline, 'complete_retirement_bootstrap', lose_response)
            with pytest.raises(RuntimeError, match='lost after bootstrap commit'):
                await resume(runtime, storage, run)
        assert len(committed) == 1
        before = dump(path)
    finally:
        await runtime.close()
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    cold = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    try:
        from okto_pulse.community.adapters.relational_schema_lifecycle import CommunityRelationalSchemaLifecycleOrchestrator
        async def forbidden(*args):
            pytest.fail('lifecycle repeated after committed checkpoint')
        monkeypatch.setattr(CommunityRelationalSchemaLifecycleOrchestrator, 'initialize_schema', forbidden)
        result = await resume(cold, storage, run)
        assert result['state'] == 'bootstrap_complete' and result['bootstrap'] == committed[0]
        assert dump(path) == before
        with pytest.raises(Exception, match='retirement_cutover_incomplete'):
            await offline.require_retirement_runtime_admission(cold.engine)
    finally:
        await cold.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['checkpoint', 'card-content', 'policy'])
async def test_late_failure_restores_schema_and_data_before_bootstrap(tmp_path, monkeypatch, failure):
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        await offline.resume_offline_retirement_schema(runtime, storage, (), run, migration_builds=MIGRATION)
        before = dump(path)
        with monkeypatch.context() as scoped:
            if failure == 'checkpoint':
                original = bootstrap.record_retirement_stage
                async def interrupted(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise RuntimeError('injected checkpoint')
                scoped.setattr(bootstrap, 'record_retirement_stage', interrupted)
            else:
                from okto_pulse.community.adapters import data_bootstrap_steps
                key = '_bootstrap_quality_assessment_legacy_import_v1'
                original = data_bootstrap_steps.DATA_BOOTSTRAP_STEP_CALLABLES[key]
                async def corrupted():
                    result = await original()
                    async with db.get_engine().begin() as connection:
                        sql = "UPDATE cards SET title='changed' WHERE id='card-a'" if failure == 'card-content' else \
                            "UPDATE specs SET validation_min_completeness=99 WHERE id='spec-a'"
                        await connection.exec_driver_sql(sql)
                    return result
                scoped.setitem(data_bootstrap_steps.DATA_BOOTSTRAP_STEP_CALLABLES, key, corrupted)
            with pytest.raises(Exception, match='injected checkpoint|card_content_changed|mismatch|parity'):
                await resume(runtime, storage, run)
        assert dump(path) == before
        assert (await resume(runtime, storage, run))['state'] == 'bootstrap_complete'
        with sqlite3.connect(path) as connection:
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_lost_checkpoint_never_reexecutes_bootstrap(tmp_path):
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        await resume(runtime, storage, run)
        async with runtime.engine.begin() as connection:
            trigger = (await connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE name='retirement_data_no_delete'")).scalar_one()
            await connection.exec_driver_sql('DROP TRIGGER retirement_data_no_delete')
            await connection.exec_driver_sql('DELETE FROM retirement_data_checkpoints WHERE ordinal=8')
            await connection.exec_driver_sql(trigger)
        before = dump(path)
        with pytest.raises(ValueError, match='retirement_schema_completion_mismatch'):
            await resume(runtime, storage, run)
        assert dump(path) == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['data', 'lifecycle'])
async def test_checkpoint_replay_rejects_changed_candidate_or_plan(tmp_path, monkeypatch, damage):
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        await resume(runtime, storage, run)
        if damage == 'data':
            async with runtime.engine.begin() as connection:
                await connection.exec_driver_sql("UPDATE specs SET title='changed after checkpoint' WHERE id='spec-a'")
        else:
            orchestrator, fingerprint = bootstrap._lifecycle()
            monkeypatch.setattr(bootstrap, '_lifecycle', lambda: (orchestrator, '0' * 64))
            assert fingerprint != '0' * 64
        before = dump(path)
        from okto_pulse.community.adapters.relational_schema_lifecycle import CommunityRelationalSchemaLifecycleOrchestrator
        async def forbidden(*args):
            pytest.fail('checkpoint drift must never repeat lifecycle writers')
        monkeypatch.setattr(CommunityRelationalSchemaLifecycleOrchestrator, 'initialize_schema', forbidden)
        with pytest.raises(ValueError, match='retirement_bootstrap_replay_mismatch'):
            await resume(runtime, storage, run)
        assert dump(path) == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_max7_schema_receipt_is_verified_before_journal_expansion(tmp_path, monkeypatch):
    from okto_pulse.community.adapters.sqlalchemy_models import RetirementDataCheckpoint
    constraint = next(item for item in RetirementDataCheckpoint.__table__.constraints
        if item.name == 'ck_retirement_checkpoint_ordinal')
    # Synthetic legacy journal DDL only; actual cut produces its own receipt.
    # Never rewrite the receipt/hash to accommodate the newer CHECK afterward.
    with monkeypatch.context() as legacy:
        legacy.setattr(constraint, 'sqltext', text('ordinal >= 0 AND ordinal <= 7'))
        runtime, storage, run, path = await prepare(tmp_path)
        try:
            old = await offline.resume_offline_retirement_schema(runtime, storage, (), run, migration_builds=MIGRATION)
        except BaseException:
            await runtime.close()
            raise
    try:
        before = dump(path)
        assert await offline.resume_offline_retirement_schema(runtime, storage, (), run, migration_builds=MIGRATION) == old
        assert dump(path) == before
        result = await resume(runtime, storage, run)
        assert result['state'] == 'bootstrap_complete' and result['schema'] == old['schema']
        with sqlite3.connect(path) as connection:
            ddl = connection.execute("SELECT sql FROM sqlite_schema WHERE name='retirement_data_checkpoints'").fetchone()[0]
            assert 'ordinal <= 8' in ddl
        stable = dump(path)
        assert await resume(runtime, storage, run) == result
        assert dump(path) == stable
    finally:
        await runtime.close()
