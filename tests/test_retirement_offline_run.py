"""Real backup and SQL steps composed through retained operator anchors."""

from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import context_disposition_retirement as context
from okto_pulse.community.adapters import card_validation_retirement as cards
from okto_pulse.community.adapters import sprint_work_retirement as work
from okto_pulse.community.adapters import joint_recovery_snapshot as recovery
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from test_card_context_retirement import dump
from test_card_validation_retirement import raw_cards
from test_context_disposition_retirement import prepare as prepare_context
from test_joint_recovery_window import _start
from test_schema_lifecycle_reentry import probe
import test_sprint_retirement_inventory as relational

database = relational.database
SOURCE = recovery.RecoveryBuildPair("a" * 40, "b" * 40, "c" * 64, "d" * 64)
MIGRATION = recovery.RecoveryBuildPair("e" * 40, "f" * 40, "1" * 64, "2" * 64)


async def inputs(engine, tmp_path):
    storage, _, plan = await prepare_context(engine, tmp_path)
    kg, backups = tmp_path / "kg", tmp_path / "backups"
    kg.mkdir()
    backups.mkdir()
    runtime = db.CommunityDatabaseRuntime(engine, async_sessionmaker(engine))
    kwargs = dict(snapshot_id="original", plan=plan, source_builds=SOURCE, migration_builds=MIGRATION,
        runtime_directories=(tmp_path, kg), kg_base_dir=kg, max_seconds=120)
    return runtime, storage, backups, tmp_path / "private-run", kwargs


async def prepare(args):
    runtime, storage, backups, directory, kwargs = args
    return await offline.prepare_offline_retirement_run(runtime, storage, (), backups, directory, **kwargs)


async def resume(args, run):
    return await offline.resume_offline_retirement_data(args[0], args[1], run, migration_builds=MIGRATION)


@pytest.mark.asyncio
async def test_preparation_and_resume_hold_both_fences_and_keep_original_backup(database, tmp_path, monkeypatch):
    engine, path = database
    args = await inputs(engine, tmp_path)
    original_rows, original_cards = dump(path), await raw_cards(engine)
    captured = offline.capture_permission_retirement_checkpoint
    resumed = offline.resume_retirement_data_run
    observed = []
    def assert_fences(stage):
        assert probe(db._schema_process_lock_path(args[0])) == "blocked"
        assert [_start(root) for root in args[4]["runtime_directories"]] == ["blocked", "blocked"]
        observed.append(stage)
    async def checked_capture(*a, **kw):
        assert_fences("prepare")
        return await captured(*a, **kw)
    async def checked_resume(*a, **kw):
        assert_fences("resume")
        return await resumed(*a, **kw)
    monkeypatch.setattr(offline, "capture_permission_retirement_checkpoint", checked_capture)
    monkeypatch.setattr(offline, "resume_retirement_data_run", checked_resume)
    run = await prepare(args)
    assert await raw_cards(engine) == original_cards
    document, plan, permission, data, backup, _ = offline.read_offline_retirement_run(run)
    assert plan == args[4]["plan"] and permission.migration_id == data.migration_id
    assert dump(backup.directory / "relational" / "database.sqlite3") == original_rows
    assert "operator-prose" in (run.directory / "run.json").read_text(encoding="utf-8")
    assert "operator-prose" not in "\n".join(dump(path))
    before = dump(path)
    with pytest.raises(ValueError, match="private_destination"):
        await prepare(args)
    assert dump(path) == before
    result = await resume(args, run)
    assert result["state"] == "data_preserved" and result["cards"].card_count == 3
    assert result["permission_checkpoint"] == permission and result["backup"] == backup
    before = dump(path)
    assert await resume(args, run) == result
    assert dump(path) == before
    assert observed == ["prepare", "resume", "resume"]
    assert probe(db._schema_process_lock_path(args[0])) == "entered"
    assert [_start(root) for root in args[4]["runtime_directories"]] == ["started", "started"]
    with pytest.raises(Exception, match="retirement_cutover_incomplete"):
        await offline.require_retirement_runtime_admission(engine)
    assert document["source_builds"] != document["migration_builds"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["context", "cards", "work"])
async def test_reopen_after_committed_stage_uses_retained_receipts_without_recapture(database, tmp_path, monkeypatch, stage):
    engine, path = database
    args = await inputs(engine, tmp_path)
    run = await prepare(args)
    module, name = {"context": (context, "install_context_dispositions"), "cards": (cards, "materialize_archived_card_policies"),
        "work": (work, "supersede_archived_sprint_work")}[stage]
    original = getattr(module, name)
    async def interrupted(*a, **kw):
        await original(*a, **kw)
        raise RuntimeError("lost after committed stage")
    with monkeypatch.context() as scoped:
        scoped.setattr(module, name, interrupted)
        with pytest.raises(RuntimeError, match="lost after"):
            await resume(args, run)
    def forbidden(*a, **kw):
        raise AssertionError("must not recapture original authority or backup")
    monkeypatch.setattr(offline, "capture_permission_retirement_checkpoint", forbidden)
    monkeypatch.setattr(offline, "capture_sprint_retirement_archive", forbidden)
    monkeypatch.setattr(recovery, "_capture_joint_recovery_snapshot", forbidden)
    if stage in {"cards", "work"}:
        monkeypatch.setattr(context, "_require_original_archive", forbidden)
        monkeypatch.setattr(cards, "_capture", forbidden)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET title='Later edit' WHERE id='spec-a'")
    reopened = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        runtime = db.CommunityDatabaseRuntime(reopened, async_sessionmaker(reopened))
        result = await offline.resume_offline_retirement_data(runtime, args[1],
            offline.OfflineRetirementRun(run.directory, run.manifest_sha256), migration_builds=MIGRATION)
        assert result["state"] == "data_preserved" and result["context"].candidate_count == 4
        before = dump(path)
        assert await resume(args, run) == result
        assert dump(path) == before
    finally:
        await reopened.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["run", "backup", "permission", "data", "build", "storage", "database"])
async def test_broken_anchors_or_runtime_binding_fail_before_transform(database, tmp_path, damage):
    engine, path = database
    args = await inputs(engine, tmp_path)
    run = await prepare(args)
    _, _, _, _, backup, _ = offline.read_offline_retirement_run(run)
    runtime, storage, builds = args[0], args[1], MIGRATION
    extra = None
    if damage in {"run", "backup"}:
        artifact = run.directory / "run.json" if damage == "run" else backup.directory / "manifest.json"
        artifact.write_bytes(artifact.read_bytes() + b" ")
    elif damage in {"permission", "data"}:
        async with engine.begin() as connection:
            if damage == "permission":
                await connection.exec_driver_sql("DELETE FROM permission_introduction_audit WHERE phase='permission_retirement_capture'")
            else:
                await connection.exec_driver_sql("DROP TRIGGER retirement_data_no_delete")
                await connection.exec_driver_sql("DELETE FROM retirement_data_checkpoints")
    elif damage == "build":
        builds = replace(MIGRATION, core_wheel_sha256="3" * 64)
    elif damage == "storage":
        storage = CommunityFileSystemStorage(str(tmp_path / "other-storage"))
    else:
        extra = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'other.sqlite'}")
        runtime = db.CommunityDatabaseRuntime(extra, async_sessionmaker(extra))
    before = dump(path)
    try:
        with pytest.raises(ValueError):
            await offline.resume_offline_retirement_data(runtime, storage, run, migration_builds=builds)
        assert dump(path) == before
        assert (await raw_cards(engine))["c1"]["sprint_id"] == "sprint"
    finally:
        if extra is not None:
            await extra.dispose()


@pytest.mark.asyncio
async def test_invalid_plan_never_seals_or_prepares_data_journal(database, tmp_path):
    engine, _ = database
    args = await inputs(engine, tmp_path)
    args[4]["plan"] = args[4]["plan"].model_copy(update={"decisions": ()})
    with pytest.raises(ValueError, match="population_mismatch"):
        await prepare(args)
    assert not args[3].exists()
    async with engine.connect() as connection:
        assert (await connection.exec_driver_sql("SELECT count(*) FROM retirement_data_checkpoints")).scalar_one() == 0
    assert (await raw_cards(engine))["c1"]["sprint_id"] == "sprint"
    assert (args[2] / "original" / "manifest.json").is_file()


@pytest.mark.asyncio
async def test_failed_publication_retains_original_rollback_and_blocks_replacement_backup(database, tmp_path, monkeypatch):
    engine, path = database
    args = await inputs(engine, tmp_path)
    original_rows = dump(path)
    retained = []
    def fail_seal(directory, document):
        retained.append(recovery.JointRecoverySnapshot(args[2] / "original", document["backup"]["manifest_sha256"]))
        raise OSError("operator artifact unavailable")
    monkeypatch.setattr(offline, "_seal", fail_seal)
    with pytest.raises(OSError, match="artifact unavailable"):
        await prepare(args)
    assert not args[3].exists()
    assert (await raw_cards(engine))["c1"]["sprint_id"] == "sprint"
    args[4]["snapshot_id"] = "replacement"
    with pytest.raises(Exception, match="retirement_cutover_incomplete"):
        await prepare(args)
    assert not (args[2] / "replacement").exists()
    restored = recovery.restore_joint_recovery_snapshot(retained[0], tmp_path / "restored", builds=SOURCE,
        current_storage_root=args[1].base_dir, max_seconds=120)
    assert dump(restored / "database.sqlite3") == original_rows


@pytest.mark.asyncio
async def test_final_stage_cannot_invalidate_original_permission_receipt_silently(database, tmp_path):
    engine, _ = database
    args = await inputs(engine, tmp_path)
    run = await prepare(args)
    async with engine.begin() as connection:
        await connection.exec_driver_sql("CREATE TRIGGER destroy_permission AFTER INSERT ON retirement_data_checkpoints "
            "WHEN NEW.ordinal=3 BEGIN DELETE FROM permission_introduction_audit WHERE phase='permission_retirement_capture'; END")
    with pytest.raises(ValueError, match="permission_retirement"):
        await resume(args, run)
    with pytest.raises(Exception, match="retirement_cutover_incomplete"):
        await offline.require_retirement_runtime_admission(engine)
    with pytest.raises(ValueError, match="permission_retirement"):
        await resume(args, run)
