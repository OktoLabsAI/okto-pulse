"""SK-M SQLite fresh/upgrade/idempotency/drift migration contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters.relational_schema_migrator import (
    build_community_migration_ledger,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_spec_dependency_schema,
    _normalize_spec_started_edition_postgresql_check,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    configure_community_database,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
    Base,
    SpecDependency,
    SpecDependencyBoardLock,
    SpecDependencyOperation,
    spec_dependency_sqlite_trigger_manifest,
    spec_dependency_sqlite_trigger_predecessors,
)


async def _runtime(path: Path):
    return configure_community_database(f"sqlite+aiosqlite:///{path.as_posix()}")


@pytest.mark.asyncio
async def test_fresh_schema_is_exact_and_migration_is_idempotent(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-fresh.db")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    assert await _migrate_spec_dependency_schema() == "skipped"
    assert await _migrate_spec_dependency_schema() == "skipped"
    async with runtime.engine.connect() as connection:
        tables = {
            str(row[0])
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ).all()
        }
        triggers = {
            str(row[0])
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_spec_dependency_%'"
                )
            ).all()
        }
        active_index = (
            await connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='uq_spec_dependency_active_edge'"
            )
        ).scalar_one()
        board_boundary_trigger_sql = (
            await connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_spec_dependency_board_boundary_insert'"
            )
        ).scalar_one()
        dependency_delete_trigger_sql = (
            await connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_spec_dependency_immutable_delete'"
            )
        ).scalar_one()
        operation_delete_trigger_sql = (
            await connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_spec_dependency_operation_immutable_delete'"
            )
        ).scalar_one()
    assert {
        "spec_dependency_board_locks",
        "spec_dependencies",
        "spec_dependency_operations",
    }.issubset(tables)
    assert triggers == set(spec_dependency_sqlite_trigger_manifest())
    assert "WHERE active = true" in str(active_index)
    assert "NEW.dependent_spec_id" in str(board_boundary_trigger_sql)
    assert "NEW.prerequisite_spec_ref" in str(board_boundary_trigger_sql)
    assert "kg_board_erasure_permits" in str(dependency_delete_trigger_sql)
    assert "artifact_deletion_tombstones" in str(dependency_delete_trigger_sql)
    assert "OLD.dependent_spec_id" in str(dependency_delete_trigger_sql)
    assert "kg_board_erasure_permits" in str(operation_delete_trigger_sql)
    assert "artifact_deletion_tombstones" not in str(operation_delete_trigger_sql)
    await runtime.close()


@pytest.mark.asyncio
async def test_legacy_dependency_table_adds_sealed_snapshot_columns_before_audit(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-sealed-snapshot-upgrade.db")
    snapshot_columns = (
        "source_title_on_create",
        "source_edition_on_create",
        "source_title_on_remove",
        "source_edition_on_remove",
        "target_title_on_remove",
        "target_edition_on_remove",
    )
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "DROP TRIGGER trg_spec_dependency_tombstone_immutable_update"
        )
        for column_name in snapshot_columns:
            await connection.exec_driver_sql(
                f"ALTER TABLE spec_dependencies DROP COLUMN {column_name}"
            )

    assert await _migrate_spec_dependency_schema() is None
    assert await _migrate_spec_dependency_schema() == "skipped"
    async with runtime.engine.connect() as connection:
        columns = tuple(
            str(row[1])
            for row in (
                await connection.exec_driver_sql(
                    "PRAGMA table_info('spec_dependencies')"
                )
            ).all()
        )
        trigger_sql = str(
            (
                await connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='trg_spec_dependency_tombstone_immutable_update'"
                )
            ).scalar_one()
        )
    assert columns[-6:] == snapshot_columns
    assert "NEW.source_title_on_create IS OLD.source_title_on_create" in trigger_sql
    assert "NEW.source_edition_on_create IS OLD.source_edition_on_create" in trigger_sql
    await runtime.close()


@pytest.mark.asyncio
async def test_legacy_schema_adds_marker_backfills_started_editions_and_converges(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-upgrade.db")
    async with runtime.engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TABLE boards (id VARCHAR(36) PRIMARY KEY)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE kg_board_erasure_permits ("
            "board_id VARCHAR(36) PRIMARY KEY, permit_token VARCHAR(64) NOT NULL)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE artifact_deletion_tombstones ("
            "id VARCHAR(36) PRIMARY KEY, board_id VARCHAR(36) NOT NULL, "
            "artifact_type VARCHAR(50) NOT NULL, artifact_id VARCHAR(36) NOT NULL, "
            "generation INTEGER NOT NULL, delete_event_id VARCHAR(255) NOT NULL, "
            "FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE specs ("
            "id VARCHAR(36) PRIMARY KEY, board_id VARCHAR(36) NOT NULL, "
            "title VARCHAR(500) NOT NULL, status VARCHAR(50) NOT NULL, "
            "edition INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL, "
            "archived BOOLEAN NOT NULL DEFAULT false, ideation_id VARCHAR(36), "
            "FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE spec_history ("
            "id VARCHAR(36) PRIMARY KEY, spec_id VARCHAR(36) NOT NULL, "
            "action VARCHAR(100) NOT NULL, changes JSON, created_at DATETIME, "
            "FOREIGN KEY(spec_id) REFERENCES specs(id) ON DELETE CASCADE)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE cards ("
            "id VARCHAR(36) PRIMARY KEY, spec_id VARCHAR(36), "
            "status VARCHAR(50) NOT NULL, created_at DATETIME, "
            "updated_at DATETIME, "
            "FOREIGN KEY(spec_id) REFERENCES specs(id) ON DELETE SET NULL)"
        )
        await connection.exec_driver_sql("INSERT INTO boards(id) VALUES ('b')")
        await connection.exec_driver_sql(
            "INSERT INTO specs(id,board_id,title,status,edition,version,archived) "
            "VALUES ('draft','b','Draft','draft',4,1,false),"
            "('running','b','Running','in_progress',3,1,false),"
            "('done','b','Done','done',2,1,false),"
            "('cancelled','b','Cancelled','cancelled',5,1,false),"
            "('validated-started','b','Validated started','validated',3,1,false),"
            "('validated-unknown','b','Validated unknown','validated',3,1,false),"
            "('approved-history','b','Approved history','approved',1,1,false),"
            "('card-started','b','Card started','validated',1,1,false),"
            "('card-running','b','Card running','review',1,1,false),"
            "('card-done','b','Card done','approved',1,1,false),"
            "('draft-reopened','b','Draft reopened','draft',2,1,false),"
            "('review-reopened','b','Review reopened','review',2,1,false),"
            "('review-current-card','b','Review current card','review',2,1,false)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO spec_history(id,spec_id,action,changes,created_at) VALUES "
            "('h1','validated-started','status_changed',"
            '\'[{"field":"status","old":"done","new":"draft"},'
            '{"field":"edition","old":2,"new":3}]\','
            "'2026-01-01 00:00:00'),"
            "('h2','validated-started','status_changed',"
            '\'[{"field":"status","old":"validated",'
            '"new":"in_progress"}]\',\'2026-01-02 00:00:00\'),'
            "('h3','validated-started','status_changed',"
            '\'[{"field":"status","old":"in_progress",'
            '"new":"validated"}]\',\'2026-01-03 00:00:00\'),'
            "('h4','validated-unknown','status_changed',"
            '\'[{"field":"status","old":"approved",'
            '"new":"validated"}]\',\'2026-01-03 00:00:00\'),'
            "('h5','approved-history','status_changed',"
            '\'[{"field":"status","old":"in_progress",'
            '"new":"validated"}]\',\'2026-01-02 00:00:00\'),'
            "('h6','approved-history','status_changed',"
            '\'[{"field":"status","old":"validated",'
            '"new":"approved"}]\',\'2026-01-03 00:00:00\'),'
            "('h7','draft-reopened','status_changed',"
            '\'[{"field":"status","old":"done","new":"draft"},'
            '{"field":"edition","old":1,"new":2}]\','
            "'2026-01-04 00:00:00'),"
            "('h8','review-current-card','status_changed',"
            '\'[{"field":"status","old":"done","new":"draft"},'
            '{"field":"edition","old":1,"new":2}]\','
            "'2026-01-04 00:00:00'),"
            "('h9','review-reopened','status_changed',"
            '\'[{"field":"status","old":"done","new":"draft"},'
            '{"field":"edition","old":1,"new":2}]\','
            "'2026-01-04 00:00:00')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO cards(id,spec_id,status,created_at,updated_at) VALUES "
            "('c1','card-started','started','2026-01-02','2026-01-02'),"
            "('c2','card-running','in_progress','2026-01-02','2026-01-02'),"
            "('c3','card-done','done','2026-01-02','2026-01-03'),"
            "('c4','draft-reopened','done','2026-01-02','2026-01-03'),"
            "('c5','review-current-card','in_progress','2026-01-05','2026-01-05'),"
            "('c6','review-reopened','done','2026-01-02','2026-01-03')"
        )
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=(
                    SpecDependencyBoardLock.__table__,
                    SpecDependency.__table__,
                    SpecDependencyOperation.__table__,
                ),
            )
        )

    assert await _migrate_spec_dependency_schema() is None
    assert await _migrate_spec_dependency_schema() == "skipped"
    async with runtime.engine.connect() as connection:
        columns = {
            str(row[1])
            for row in (
                await connection.exec_driver_sql("PRAGMA table_info('specs')")
            ).all()
        }
        values = dict(
            (
                await connection.exec_driver_sql(
                    "SELECT id,last_started_edition FROM specs ORDER BY id"
                )
            ).all()
        )
    assert "last_started_edition" in columns
    assert values == {
        "approved-history": 1,
        "cancelled": 5,
        "card-done": 1,
        "card-running": 1,
        "card-started": 1,
        "done": 2,
        "draft": None,
        "draft-reopened": None,
        "review-current-card": 2,
        "review-reopened": None,
        "running": 3,
        "validated-started": 3,
        "validated-unknown": 3,
    }
    await runtime.close()


@pytest.mark.asyncio
async def test_owned_trigger_drift_is_rejected_fail_closed(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "skm-drift.db")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "DROP TRIGGER trg_spec_dependency_operation_immutable_update"
        )
        await connection.exec_driver_sql(
            "CREATE TRIGGER trg_spec_dependency_operation_immutable_update "
            "BEFORE UPDATE ON spec_dependency_operations BEGIN SELECT 1; END"
        )

    with pytest.raises(RuntimeError, match="owned trigger is corrupt"):
        await _migrate_spec_dependency_schema()
    await runtime.close()


@pytest.mark.asyncio
async def test_started_edition_trigger_predecessor_is_upgraded_exactly_once(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-started-trigger-upgrade.db")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for trigger_name, sources in (
            spec_dependency_sqlite_trigger_predecessors().items()
        ):
            await connection.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
            await connection.exec_driver_sql(sources[0])

    assert await _migrate_spec_dependency_schema() is None
    assert await _migrate_spec_dependency_schema() == "skipped"
    async with runtime.engine.connect() as connection:
        rows = (
            await connection.exec_driver_sql(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_spec_dependency_started_edition_%'"
            )
        ).all()
    observed = {str(name): str(sql) for name, sql in rows}
    manifest = spec_dependency_sqlite_trigger_manifest()
    started_trigger_names = {
        "trg_spec_dependency_started_edition_insert",
        "trg_spec_dependency_started_edition_update",
    }
    assert set(observed) == started_trigger_names
    for trigger_name, sql in observed.items():
        assert "".join(sql.split()).lower() == "".join(
            manifest[trigger_name][1].split()
        ).lower()
    await runtime.close()


@pytest.mark.asyncio
async def test_started_edition_database_guard_is_monotonic_across_reentry(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-started-monotonic.db")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "INSERT INTO boards(id,name,owner_id,realm_id) "
            "VALUES ('b-marker','Marker','owner','local')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO specs(id,board_id,title,status,edition,version,archived,"
            "created_by) VALUES "
            "('s-marker','b-marker','Marker','validated',2,1,false,'owner')"
        )

        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET last_started_edition=1 WHERE id='s-marker'"
            )
        await connection.exec_driver_sql(
            "UPDATE specs SET last_started_edition=edition WHERE id='s-marker'"
        )
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET last_started_edition=NULL WHERE id='s-marker'"
            )
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET edition=4 WHERE id='s-marker'"
            )

        # Return to Draft advances exactly one edition and preserves the old
        # execution memory. Starting again moves only the marker to edition 3.
        await connection.exec_driver_sql(
            "UPDATE specs SET status='draft', edition=3 WHERE id='s-marker'"
        )
        marker_after_reentry = (
            await connection.exec_driver_sql(
                "SELECT edition,last_started_edition FROM specs "
                "WHERE id='s-marker'"
            )
        ).one()
        assert tuple(marker_after_reentry) == (3, 2)
        await connection.exec_driver_sql(
            "UPDATE specs SET last_started_edition=edition WHERE id='s-marker'"
        )
        assert (
            await connection.exec_driver_sql(
                "SELECT last_started_edition FROM specs WHERE id='s-marker'"
            )
        ).scalar_one() == 3
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET last_started_edition=2 WHERE id='s-marker'"
            )
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET edition=2 WHERE id='s-marker'"
            )
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "INSERT INTO specs(id,board_id,title,status,edition,version,archived,"
                "created_by,last_started_edition) VALUES "
                "('s-invalid','b-marker','Invalid','draft',3,1,false,'owner',2)"
            )
    await runtime.close()


@pytest.mark.asyncio
async def test_owned_delete_trigger_event_drift_is_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-delete-trigger-drift.db")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "DROP TRIGGER trg_spec_dependency_immutable_delete"
        )
        await connection.exec_driver_sql(
            "CREATE TRIGGER trg_spec_dependency_immutable_delete "
            "BEFORE UPDATE ON spec_dependencies BEGIN SELECT 1; END"
        )

    with pytest.raises(RuntimeError, match="owned trigger is corrupt"):
        await _migrate_spec_dependency_schema()
    await runtime.close()


@pytest.mark.asyncio
async def test_legacy_cross_board_edge_is_rejected_fail_closed(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "skm-cross-board-drift.db")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "INSERT INTO boards(id,name,owner_id,realm_id) VALUES "
            "('b-source','Source','owner','local'),"
            "('b-target','Target','owner','local')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO specs(id,board_id,title,status,edition,version,archived,"
            "created_by) VALUES "
            "('s-source','b-source','Source','draft',1,1,false,'owner'),"
            "('s-target','b-target','Target','done',1,1,false,'owner')"
        )
        await connection.exec_driver_sql(
            "DROP TRIGGER trg_spec_dependency_board_boundary_insert"
        )
        await connection.exec_driver_sql(
            "INSERT INTO spec_dependencies("
            "id,board_id,dependent_spec_id,prerequisite_spec_id,"
            "prerequisite_spec_ref,active,resolved_on_create,retrospective,"
            "introduced_at_spec_version,source_version_on_create,"
            "source_status_on_create,target_status_on_create,"
            "target_version_on_create,target_title_on_create,"
            "target_edition_on_create,add_idempotency_key,add_request_digest,"
            "created_at,created_by_id,created_by_type,created_by_name) VALUES ("
            "'dep-cross','b-source','s-source','s-target','s-target',true,true,"
            "false,2,2,'draft','done',1,'Target',1,'add-cross','" + "a" * 64 + "',"
            "'2026-01-01 00:00:00','owner','user','Owner')"
        )

    with pytest.raises(RuntimeError, match="board-boundary data is corrupt"):
        await _migrate_spec_dependency_schema()
    await runtime.close()


@pytest.mark.asyncio
async def test_started_edition_legacy_data_drift_is_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-started-edition-data-drift.db")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "DROP TRIGGER trg_spec_dependency_started_edition_insert"
        )
        await connection.exec_driver_sql(
            "DROP TRIGGER trg_spec_dependency_started_edition_update"
        )
        await connection.exec_driver_sql(
            "INSERT INTO boards(id,name,owner_id,realm_id) "
            "VALUES ('b-corrupt','Corrupt','owner','local')"
        )
        # Model an inherited database whose constraint was previously absent;
        # SQLite's test-only pragma lets us seed that legacy drift without
        # weakening the canonical table definition used by the migration.
        await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        await connection.exec_driver_sql(
            "INSERT INTO specs(id,board_id,title,status,edition,version,archived,"
            "created_by,last_started_edition) VALUES "
            "('s-corrupt','b-corrupt','Corrupt','validated',2,1,false,"
            "'owner',3)"
        )
        await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    with pytest.raises(RuntimeError, match="started-edition data is corrupt"):
        await _migrate_spec_dependency_schema()
    await runtime.close()


def test_migration_and_global_revision_censuses_include_dependency_authority_once() -> (
    None
):
    step_ids = [step.step_id for step in build_community_migration_ledger()]
    assert step_ids.count("_migrate_spec_dependency_schema") == 1
    assert step_ids.index("_migrate_spec_dependency_schema") < step_ids.index(
        "_migrate_agent_permissions"
    )
    assert GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES.count("spec_dependencies") == 1
    assert GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION == (
        "gdsr-trigger-manifest-v7"
    )


def test_postgresql_started_edition_constraint_normalization_rejects_weakening() -> (
    None
):
    canonical = (
        "CHECK (((last_started_edition IS NULL) OR "
        "((last_started_edition >= 1) AND "
        "(last_started_edition <= edition))))"
    )
    weakened = canonical[:-1] + " OR TRUE)"

    assert _normalize_spec_started_edition_postgresql_check(canonical) == (
        _normalize_spec_started_edition_postgresql_check(
            "CHECK (last_started_edition IS NULL OR "
            "(last_started_edition >= 1 AND "
            "last_started_edition <= edition))"
        )
    )
    assert _normalize_spec_started_edition_postgresql_check(weakened) != (
        _normalize_spec_started_edition_postgresql_check(canonical)
    )
