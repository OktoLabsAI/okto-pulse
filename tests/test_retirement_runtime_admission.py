"""Interrupted data preservation must not be served or normalized by startup."""

import asyncio
import json
import subprocess
import sys

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.core.ports import SchemaMigrationError
from okto_pulse.core.ports import relational_runtime as core_runtime
from okto_pulse.core.ports.schema_lifecycle import register_relational_schema_lifecycle_orchestrator
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle
from okto_pulse.community.adapters.retirement_runtime_admission import (
    require_retirement_not_started, require_retirement_runtime_admission,
)
from okto_pulse.community.adapters.sqlalchemy_models import RetirementDataCheckpoint
from okto_pulse.community.adapters import joint_recovery_snapshot as recovery
from okto_pulse.community.adapters.retirement_data_journal import prepare_retirement_data_run, resume_retirement_data_run
from test_context_disposition_retirement import prepare as prepare_context
from test_card_context_retirement import dump
import test_sprint_retirement_inventory as relational

database = relational.database


async def _empty(tmp_path):
    path = tmp_path / "admission.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("CREATE TABLE untouched(id INTEGER PRIMARY KEY, value TEXT)")
        await connection.exec_driver_sql("INSERT INTO untouched VALUES(1, 'original')")
    return engine, path


@pytest.mark.asyncio
@pytest.mark.parametrize("journal", [False, True])
async def test_absent_or_empty_journal_is_admitted_without_writing(tmp_path, journal):
    engine, path = await _empty(tmp_path)
    try:
        if journal:
            async with engine.begin() as connection:
                await connection.run_sync(RetirementDataCheckpoint.__table__.create)
        before = dump(path)
        await require_retirement_runtime_admission(engine)
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", ["view", "wrong_columns", "extra_column"])
async def test_invalid_journal_structure_cannot_appear_as_an_empty_ready_database(tmp_path, schema):
    engine, path = await _empty(tmp_path)
    try:
        async with engine.begin() as connection:
            if schema == "view":
                await connection.exec_driver_sql("CREATE VIEW retirement_data_checkpoints AS SELECT 1 WHERE 0")
            elif schema == "wrong_columns":
                await connection.exec_driver_sql("CREATE TABLE retirement_data_checkpoints(value TEXT)")
            else:
                await connection.run_sync(RetirementDataCheckpoint.__table__.create)
                await connection.exec_driver_sql("ALTER TABLE retirement_data_checkpoints ADD COLUMN runtime_ready INTEGER")
        before = dump(path)
        with pytest.raises(SchemaMigrationError, match="journal_schema_invalid"):
            await require_retirement_runtime_admission(engine)
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize("event_type", [
    "migration.context_dispositions_committed", "historical_context.bound",
    "migration.card_validation_preserved", "migration.work_superseded", "migration.work_retirement_completed",
])
async def test_retained_effect_receipts_block_when_the_data_journal_is_lost(tmp_path, journal, event_type):
    engine, path = await _empty(tmp_path)
    try:
        async with engine.begin() as connection:
            if journal:
                await connection.run_sync(RetirementDataCheckpoint.__table__.create)
            await connection.exec_driver_sql("CREATE TABLE domain_events(id TEXT, event_type TEXT, payload_json TEXT)")
            await connection.exec_driver_sql("INSERT INTO domain_events VALUES ('private', ?, 'unreadable historical payload')", (event_type,))
        before = dump(path)
        with pytest.raises(SchemaMigrationError, match="retirement_cutover_incomplete"):
            await require_retirement_runtime_admission(engine)
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_original_archive_and_unrelated_history_do_not_imply_a_started_cutover(tmp_path):
    engine, path = await _empty(tmp_path)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("CREATE TABLE domain_events(id TEXT, event_type TEXT, payload_json TEXT)")
            for value in ("historical_archive.created", "card.created", "sprint.closed"):
                await connection.exec_driver_sql("INSERT INTO domain_events VALUES (?, ?, 'opaque preserved history')", (value, value))
        before = dump(path)
        await require_retirement_runtime_admission(engine)
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinal", [0, 1, 2, 3])
async def test_all_retained_prefixes_block_every_lifecycle_path_before_plan_or_seeds(tmp_path, monkeypatch, ordinal):
    engine, path = await _empty(tmp_path)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(RetirementDataCheckpoint.__table__.create)
            for value in range(ordinal + 1):
                # Even a forged self-asserted terminal flag is not admission.
                await connection.exec_driver_sql("INSERT INTO retirement_data_checkpoints VALUES (?, ?, ?, ?)",
                    ("opaque-private-id", value, json.dumps({"runtime_ready": True, "state": "complete"}), "f" * 64))
        runtime = db.CommunityDatabaseRuntime(engine, async_sessionmaker(engine))
        monkeypatch.setattr(db, "get_engine", lambda: engine)
        monkeypatch.setattr(db, "resolve_community_database_runtime", lambda: runtime)
        orchestrator = register_community_relational_schema_lifecycle()
        def forbidden(*args, **kwargs):
            pytest.fail("admission must precede migration and bootstrap planning")
        monkeypatch.setattr(orchestrator._migrator, "plan", forbidden)
        monkeypatch.setattr(orchestrator._bootstrapper, "plan", forbidden)
        before = dump(path)
        for initialize in (orchestrator.initialize_schema, core_runtime.init_db, db.init_db):
            with pytest.raises(SchemaMigrationError) as failure:
                await initialize()
            assert failure.value.failure_reason == "retirement_cutover_incomplete"
            assert failure.value.phase == "pre_create_all"
            assert "opaque-private-id" not in str(failure.value)
            assert "Do not delete migration evidence" in failure.value.remediation
            assert dump(path) == before
        class AlternateOrchestrator:
            async def initialize_schema(self):
                pytest.fail("Community entrypoint must check even an alternate registered orchestrator")
        register_relational_schema_lifecycle_orchestrator(AlternateOrchestrator())
        with pytest.raises(SchemaMigrationError, match="retirement_cutover_incomplete"):
            await db.init_db()
        assert dump(path) == before
    finally:
        await engine.dispose()


def _startup(path):
    script = """
import asyncio, sys
from okto_pulse.core.ports import SchemaMigrationError
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle
async def main():
    db.configure_community_database('sqlite+aiosqlite:///' + sys.argv[1])
    register_community_relational_schema_lifecycle()
    try:
        await db.init_db()
        print('ready')
    except SchemaMigrationError as failure:
        print(failure.failure_reason)
    finally:
        await db.get_engine().dispose()
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)],
        capture_output=True, text=True, timeout=120, check=True)
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.asyncio
async def test_completed_data_preservation_blocks_runtime_and_joint_restore_recovers_original_source(database, tmp_path):
    engine, path = database
    uploads, kg, backups = (tmp_path / name for name in ("storage", "kg", "backups"))
    for directory in (uploads, kg, backups):
        directory.mkdir()
    # Seed all source content and install its archive/grants before backup;
    # only preparation of the coordinator and its effects follow the backup.
    storage, references, plan = await prepare_context(engine, tmp_path)
    original_rows = dump(path)
    builds = recovery.RecoveryBuildPair("a" * 40, "b" * 40, "c" * 64, "d" * 64)
    original = recovery.create_joint_recovery_snapshot(path, (), backups, snapshot_id="original",
        builds=builds, runtime_directories=(tmp_path, kg), kg_base_dir=kg, storage_root=uploads, max_seconds=120)
    run = await prepare_retirement_data_run(engine, storage, references, plan=plan)
    result = await resume_retirement_data_run(engine, storage, run, plan=plan)
    assert result["state"] == "data_preserved"
    before = dump(path)
    assert await asyncio.to_thread(_startup, path) == "retirement_cutover_incomplete"
    assert dump(path) == before
    async with engine.begin() as connection:
        # Privileged corruption of a disposable fixture cannot turn existing
        # transformation receipts into a clean installation.
        await connection.exec_driver_sql("DROP TRIGGER retirement_data_no_delete")
        await connection.exec_driver_sql("DELETE FROM retirement_data_checkpoints")
    damaged = dump(path)
    assert await asyncio.to_thread(_startup, path) == "retirement_cutover_incomplete"
    assert dump(path) == damaged
    restored = recovery.restore_joint_recovery_snapshot(original, tmp_path / "restored", builds=builds,
        current_storage_root=uploads, max_seconds=120)
    # This is a complete rollback to the pre-run set, not deletion of markers.
    assert dump(restored / "database.sqlite3") == original_rows
    # This fixture proves joint data restoration and admission as an installer
    # source. A real rollback boots the retained matching source build pair;
    # the new build must not serve its removed operational Sprint schema.
    restored_engine = create_async_engine(f"sqlite+aiosqlite:///{restored / 'database.sqlite3'}")
    try:
        await require_retirement_not_started(restored_engine)
    finally:
        await restored_engine.dispose()
    assert await asyncio.to_thread(_startup, restored / "database.sqlite3") == "retirement_legacy_schema_requires_offline_cutover"
    assert dump(restored / "database.sqlite3") == original_rows


def test_committed_journal_survives_abrupt_process_exit_and_refuses_startup(tmp_path):
    path = tmp_path / "abrupt.sqlite"
    script = """
import os, sqlite3, sys
from sqlalchemy import create_engine
from okto_pulse.community.adapters.sqlalchemy_models import RetirementDataCheckpoint
engine = create_engine('sqlite:///' + sys.argv[1])
RetirementDataCheckpoint.__table__.create(engine)
with engine.begin() as connection:
    connection.exec_driver_sql("INSERT INTO retirement_data_checkpoints VALUES ('m1', 0, '{}', 'broken')")
os._exit(73)
"""
    exited = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=60)
    assert exited.returncode == 73, exited.stderr
    before = dump(path)
    assert _startup(path) == "retirement_cutover_incomplete"
    assert dump(path) == before
