"""Opt-in PostgreSQL conformance proof for SK-M schema lifecycle.

The Community product runtime stays SQLite-only.  This test deliberately uses
an externally managed PostgreSQL instance only as a portable-schema proof and
never starts a container or an Okto Pulse process.
"""

from __future__ import annotations

import os
from importlib import import_module
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import (
    CommunityDatabaseRuntime,
)
from okto_pulse.community.adapters.relational_schema_migrator import (
    CommunityRelationalSchemaMigrator,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_spec_dependency_schema,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    SpecDependency,
    SpecDependencyBoardLock,
    SpecDependencyOperation,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
)
from okto_pulse.core.ports.relational_runtime import (
    configure_database_runtime,
    reset_database_runtime_for_tests,
)
from okto_pulse.core.ports.relational_schema_migrator import MigrationStep

pytestmark = pytest.mark.e2e

_POSTGRES_DSN_ENV = "OKTO_PULSE_TEST_POSTGRES_DSN"
_POSTGRES_DSN = os.environ.get(_POSTGRES_DSN_ENV)
asyncpg = import_module("asyncpg") if _POSTGRES_DSN else None


def _schema_qualified_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _create_minimal_parent_schema(connection: object) -> None:
    await connection.exec_driver_sql(
        'CREATE TABLE "boards" ("id" VARCHAR(36) PRIMARY KEY)'
    )
    await connection.exec_driver_sql(
        'CREATE TABLE "specs" ('
        '"id" VARCHAR(36) PRIMARY KEY, '
        '"board_id" VARCHAR(36) NOT NULL REFERENCES "boards"("id") '
        "ON DELETE CASCADE, "
        '"title" VARCHAR(500) NOT NULL, '
        '"status" VARCHAR(50) NOT NULL, '
        '"edition" INTEGER NOT NULL DEFAULT 1, '
        '"last_started_edition" INTEGER, '
        '"version" INTEGER NOT NULL, '
        '"archived" BOOLEAN NOT NULL DEFAULT false, '
        'CONSTRAINT "ck_spec_last_started_edition" CHECK '
        '("last_started_edition" IS NULL OR '
        '("last_started_edition" >= 1 AND '
        '"last_started_edition" <= "edition")))'
    )
    await connection.exec_driver_sql(
        'CREATE TABLE "kg_board_erasure_permits" ('
        '"board_id" VARCHAR(36) PRIMARY KEY, '
        '"permit_token" VARCHAR(64) NOT NULL UNIQUE)'
    )
    await connection.exec_driver_sql(
        'CREATE TABLE "artifact_deletion_tombstones" ('
        '"id" VARCHAR(36) PRIMARY KEY, '
        '"board_id" VARCHAR(36) NOT NULL REFERENCES "boards"("id") '
        "ON DELETE CASCADE, "
        '"artifact_type" VARCHAR(50) NOT NULL, '
        '"artifact_id" VARCHAR(36) NOT NULL, '
        '"generation" INTEGER NOT NULL, '
        '"delete_event_id" VARCHAR(255) NOT NULL)'
    )


async def _create_dependency_authority(connection: object) -> None:
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


def _drift_probe_migrator() -> CommunityRelationalSchemaMigrator:
    steps = (
        MigrationStep(
            step_id="create_all_boundary",
            order=1,
            phase="create_all_boundary",
            description="The test schema already crossed create_all.",
            idempotent=True,
            destructive=False,
            owner="community",
        ),
        MigrationStep(
            step_id="_migrate_spec_dependency_schema",
            order=2,
            phase="post_create_all",
            description="SK-M portable schema proof.",
            idempotent=True,
            destructive=False,
            owner="community",
        ),
    )
    return CommunityRelationalSchemaMigrator(
        steps=steps,
        callables={
            "create_all_boundary": lambda: "skipped",
            "_migrate_spec_dependency_schema": _migrate_spec_dependency_schema,
        },
        target="community-postgresql-conformance",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _POSTGRES_DSN,
    reason=f"set {_POSTGRES_DSN_ENV} to run the SK-M PostgreSQL proof",
)
async def test_skm_postgresql_fresh_upgrade_rerun_and_drift() -> None:
    """Prove fresh/upgrade/rerun plus named-step failure on unknown drift."""

    assert asyncpg is not None
    assert _POSTGRES_DSN is not None
    schema_name = f"skm_spec_dependency_{uuid4().hex}"
    admin_dsn = _POSTGRES_DSN.replace("postgresql+asyncpg://", "postgresql://", 1)
    admin_dsn = admin_dsn.replace("postgres://", "postgresql://", 1)
    admin = await asyncpg.connect(admin_dsn)
    engine = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema_name}"')
        engine = create_async_engine(
            _schema_qualified_dsn(_POSTGRES_DSN),
            connect_args={"server_settings": {"search_path": schema_name}},
        )
        runtime = CommunityDatabaseRuntime(
            engine=engine,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
        )
        configure_database_runtime(runtime=runtime)

        # Fresh create_all boundary followed by first install and a no-op rerun.
        async with engine.begin() as connection:
            await _create_minimal_parent_schema(connection)
            await _create_dependency_authority(connection)

        assert await _migrate_spec_dependency_schema() is None
        assert await _migrate_spec_dependency_schema() == "skipped"

        # PostgreSQL owns the same monotonic lifecycle marker contract as
        # SQLite. A Draft re-entry advances exactly one edition while retaining
        # the previous marker; only the next real start may move it forward.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'INSERT INTO "boards" ("id") VALUES (\'board-marker\')'
            )
            await connection.exec_driver_sql(
                'INSERT INTO "specs" '
                '("id","board_id","title","status","edition",'
                '"version","archived") VALUES '
                "('spec-marker','board-marker','Marker','validated',2,1,false)"
            )
            with pytest.raises(
                Exception, match="spec_dependency_started_edition_invalid"
            ):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'UPDATE "specs" SET "last_started_edition"=1 '
                        "WHERE id='spec-marker'"
                    )
            await connection.exec_driver_sql(
                'UPDATE "specs" SET "last_started_edition"="edition" '
                "WHERE id='spec-marker'"
            )
            with pytest.raises(
                Exception, match="spec_dependency_started_edition_invalid"
            ):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'UPDATE "specs" SET "last_started_edition"=NULL '
                        "WHERE id='spec-marker'"
                    )
            with pytest.raises(
                Exception, match="spec_dependency_started_edition_invalid"
            ):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'UPDATE "specs" SET "edition"=4 '
                        "WHERE id='spec-marker'"
                    )
            await connection.exec_driver_sql(
                'UPDATE "specs" SET "status"=\'draft\', "edition"=3 '
                "WHERE id='spec-marker'"
            )
            assert tuple(
                (
                    await connection.exec_driver_sql(
                        'SELECT "edition","last_started_edition" FROM "specs" '
                        "WHERE id='spec-marker'"
                    )
                ).one()
            ) == (3, 2)
            await connection.exec_driver_sql(
                'UPDATE "specs" SET "last_started_edition"="edition" '
                "WHERE id='spec-marker'"
            )
            with pytest.raises(
                Exception, match="spec_dependency_started_edition_invalid"
            ):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'UPDATE "specs" SET "edition"=2 '
                        "WHERE id='spec-marker'"
                    )
            with pytest.raises(
                Exception, match="spec_dependency_started_edition_invalid"
            ):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'INSERT INTO "specs" '
                        '("id","board_id","title","status","edition",'
                        '"last_started_edition","version","archived") VALUES '
                        "('spec-invalid-marker','board-marker','Invalid','draft',"
                        "3,2,1,false)"
                    )

        # Supported upgrade: parent SDLC tables exist while the SK-M authority
        # and last_started_edition do not yet exist.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'DROP TABLE "spec_dependency_operations", '
                '"spec_dependencies", "spec_dependency_board_locks" CASCADE'
            )
            await connection.exec_driver_sql(
                'ALTER TABLE "specs" DROP CONSTRAINT "ck_spec_last_started_edition"'
            )
            await connection.exec_driver_sql(
                'ALTER TABLE "specs" DROP COLUMN "last_started_edition"'
            )
            await _create_dependency_authority(connection)

        assert await _migrate_spec_dependency_schema() is None
        assert await _migrate_spec_dependency_schema() == "skipped"

        # The database authority itself rejects a cross-board active edge;
        # this must not depend solely on the official application service.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'INSERT INTO "boards" ("id") VALUES '
                "('board-source'), ('board-target')"
            )
            await connection.exec_driver_sql(
                'INSERT INTO "specs" '
                '("id","board_id","title","status","edition",'
                '"version","archived") VALUES '
                "('spec-source','board-source','Source','draft',1,1,false),"
                "('spec-target','board-target','Target','done',1,1,false)"
            )
            with pytest.raises(
                Exception, match="spec_dependency_board_boundary_invalid"
            ):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'INSERT INTO "spec_dependencies" ('
                        '"id","board_id","dependent_spec_id",'
                        '"prerequisite_spec_id","prerequisite_spec_ref",'
                        '"active","resolved_on_create","retrospective",'
                        '"introduced_at_spec_version",'
                        '"source_version_on_create","source_status_on_create",'
                        '"target_status_on_create","target_version_on_create",'
                        '"target_title_on_create","target_edition_on_create",'
                        '"add_idempotency_key","add_request_digest",'
                        '"created_at","created_by_id","created_by_type",'
                        '"created_by_name") VALUES ('
                        "'dep-cross','board-source','spec-source','spec-target',"
                        "'spec-target',true,true,false,2,2,'draft','done',1,"
                        "'Target',1,'add-cross','" + "a" * 64 + "',"
                        "CURRENT_TIMESTAMP,'owner','user','Owner')"
                    )

        # DELETE is immutable at the database boundary.  The same guards must
        # still permit the FK action caused by a tombstoned dependent Spec and
        # the explicit Board purge while its transaction-scoped permit exists.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'INSERT INTO "specs" '
                '("id","board_id","title","status","edition",'
                '"version","archived") VALUES '
                "('spec-prerequisite','board-source','Prerequisite','done',"
                "1,1,false)"
            )
            await connection.exec_driver_sql(
                'INSERT INTO "spec_dependencies" ('
                '"id","board_id","dependent_spec_id",'
                '"prerequisite_spec_id","prerequisite_spec_ref",'
                '"active","resolved_on_create","retrospective",'
                '"introduced_at_spec_version",'
                '"source_version_on_create","source_status_on_create",'
                '"target_status_on_create","target_version_on_create",'
                '"target_title_on_create","target_edition_on_create",'
                '"add_idempotency_key","add_request_digest",'
                '"created_at","created_by_id","created_by_type",'
                '"created_by_name") VALUES ('
                "'dep-delete-guard','board-source','spec-source',"
                "'spec-prerequisite','spec-prerequisite',true,true,false,"
                "2,2,'draft','done',1,'Prerequisite',1,'add-delete','"
                + "d" * 64
                + "',CURRENT_TIMESTAMP,'owner','user','Owner')"
            )
            await connection.exec_driver_sql(
                'INSERT INTO "spec_dependency_operations" ('
                '"id","board_id","dependent_spec_ref","dependency_ref",'
                '"operation","idempotency_key","request_digest",'
                '"actor_id","actor_type","actor_name",'
                '"expected_spec_version","resulting_spec_version",'
                '"result_payload","created_at") VALUES ('
                "'operation-delete-guard','board-source','spec-source',"
                "'dep-delete-guard','add','operation-delete','"
                + "e" * 64
                + "','owner','user','Owner',1,2,'{}'::json,"
                "CURRENT_TIMESTAMP)"
            )
            with pytest.raises(Exception, match="spec_dependency_delete_forbidden"):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'DELETE FROM "spec_dependencies" '
                        "WHERE id = 'dep-delete-guard'"
                    )
            with pytest.raises(
                Exception, match="spec_dependency_operation_immutable"
            ):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'DELETE FROM "spec_dependency_operations" '
                        "WHERE id = 'operation-delete-guard'"
                    )
            with pytest.raises(Exception, match="spec_dependency_delete_forbidden"):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        'DELETE FROM "specs" WHERE id = \'spec-source\''
                    )
            await connection.exec_driver_sql(
                'INSERT INTO "artifact_deletion_tombstones" ('
                '"id","board_id","artifact_type","artifact_id",'
                '"generation","delete_event_id") VALUES ('
                "'tombstone-delete-guard','board-source','spec',"
                "'spec-source',1,'delete-spec-source')"
            )
            await connection.exec_driver_sql(
                'DELETE FROM "specs" WHERE id = \'spec-source\''
            )
            assert (
                await connection.exec_driver_sql(
                    'SELECT COUNT(*) FROM "spec_dependencies" '
                    "WHERE id = 'dep-delete-guard'"
                )
            ).scalar_one() == 0
            assert (
                await connection.exec_driver_sql(
                    'SELECT COUNT(*) FROM "spec_dependency_operations" '
                    "WHERE id = 'operation-delete-guard'"
                )
            ).scalar_one() == 1

            await connection.exec_driver_sql(
                'INSERT INTO "specs" '
                '("id","board_id","title","status","edition",'
                '"version","archived") VALUES '
                "('spec-board-cascade','board-source','Board cascade',"
                "'draft',1,1,false)"
            )
            await connection.exec_driver_sql(
                'INSERT INTO "spec_dependencies" ('
                '"id","board_id","dependent_spec_id",'
                '"prerequisite_spec_id","prerequisite_spec_ref",'
                '"active","resolved_on_create","retrospective",'
                '"introduced_at_spec_version",'
                '"source_version_on_create","source_status_on_create",'
                '"target_status_on_create","target_version_on_create",'
                '"target_title_on_create","target_edition_on_create",'
                '"add_idempotency_key","add_request_digest",'
                '"created_at","created_by_id","created_by_type",'
                '"created_by_name") VALUES ('
                "'dep-board-cascade','board-source','spec-board-cascade',"
                "'spec-prerequisite','spec-prerequisite',true,true,false,"
                "2,2,'draft','done',1,'Prerequisite',1,'add-board-cascade','"
                + "f" * 64
                + "',CURRENT_TIMESTAMP,'owner','user','Owner')"
            )
            await connection.exec_driver_sql(
                'INSERT INTO "kg_board_erasure_permits" '
                '("board_id","permit_token") VALUES '
                "('board-source','board-source-permit')"
            )
            await connection.exec_driver_sql(
                'DELETE FROM "spec_dependencies" '
                "WHERE board_id = 'board-source'"
            )
            await connection.exec_driver_sql(
                'DELETE FROM "spec_dependency_operations" '
                "WHERE board_id = 'board-source'"
            )
            assert (
                await connection.exec_driver_sql(
                    'SELECT COUNT(*) FROM "spec_dependencies" '
                    "WHERE board_id = 'board-source'"
                )
            ).scalar_one() == 0
            assert (
                await connection.exec_driver_sql(
                    'SELECT COUNT(*) FROM "spec_dependency_operations" '
                    "WHERE board_id = 'board-source'"
                )
            ).scalar_one() == 0
            await connection.exec_driver_sql(
                'DELETE FROM "kg_board_erasure_permits" '
                "WHERE board_id = 'board-source'"
            )
            await connection.exec_driver_sql(
                'DELETE FROM "boards" WHERE id = \'board-source\''
            )

        # Exact catalog drift is rejected: keeping the owned trigger name and
        # function while weakening DELETE to UPDATE must not pass convergence.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'DROP TRIGGER "trg_spec_dependency_immutable_delete" '
                'ON "spec_dependencies"'
            )
            await connection.exec_driver_sql(
                'CREATE TRIGGER "trg_spec_dependency_immutable_delete" '
                'BEFORE UPDATE ON "spec_dependencies" FOR EACH ROW '
                "EXECUTE FUNCTION pulse_spec_dependency_delete_guard()"
            )

        trigger_drift_migrator = _drift_probe_migrator()
        trigger_drift_result = await trigger_drift_migrator.aexecute(
            trigger_drift_migrator.plan(target="community-postgresql-conformance")
        )
        assert trigger_drift_result.is_success is False
        assert trigger_drift_result.failed_step is not None
        assert (
            trigger_drift_result.failed_step.step_id
            == "_migrate_spec_dependency_schema"
        )
        assert "owned trigger is corrupt" in str(trigger_drift_result.failure_reason)

        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'DROP TRIGGER "trg_spec_dependency_immutable_delete" '
                'ON "spec_dependencies"'
            )
        assert await _migrate_spec_dependency_schema() is None
        assert await _migrate_spec_dependency_schema() == "skipped"

        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'DROP INDEX "uq_spec_dependency_active_edge"'
            )
            await connection.exec_driver_sql(
                'CREATE UNIQUE INDEX "uq_spec_dependency_active_edge" '
                'ON "spec_dependencies" '
                '("board_id", "dependent_spec_id", "prerequisite_spec_ref")'
            )

        migrator = _drift_probe_migrator()
        result = await migrator.aexecute(
            migrator.plan(target="community-postgresql-conformance")
        )
        assert result.is_success is False
        assert result.failed_step is not None
        assert result.failed_step.step_id == "_migrate_spec_dependency_schema"
        assert "active-edge index predicate is corrupt" in str(result.failure_reason)

        # Restore the owned partial index, then prove that a named CHECK with
        # the expected operands but weakened by ``OR TRUE`` is still rejected.
        # This guards against substring-based catalog validation silently
        # accepting a lifecycle-edition fence that no longer fences anything.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'DROP INDEX "uq_spec_dependency_active_edge"'
            )
            await connection.exec_driver_sql(
                'CREATE UNIQUE INDEX "uq_spec_dependency_active_edge" '
                'ON "spec_dependencies" '
                '("board_id", "dependent_spec_id", "prerequisite_spec_ref") '
                'WHERE "active" = true'
            )
            await connection.exec_driver_sql(
                'ALTER TABLE "specs" DROP CONSTRAINT "ck_spec_last_started_edition"'
            )
            await connection.exec_driver_sql(
                'ALTER TABLE "specs" ADD CONSTRAINT '
                '"ck_spec_last_started_edition" CHECK '
                '("last_started_edition" IS NULL OR '
                '("last_started_edition" >= 1 AND '
                '"last_started_edition" <= "edition") OR true)'
            )

        weakened_check_migrator = _drift_probe_migrator()
        weakened_check_result = await weakened_check_migrator.aexecute(
            weakened_check_migrator.plan(target="community-postgresql-conformance")
        )
        assert weakened_check_result.is_success is False
        assert weakened_check_result.failed_step is not None
        assert (
            weakened_check_result.failed_step.step_id
            == "_migrate_spec_dependency_schema"
        )
        assert "started-edition constraint is corrupt" in str(
            weakened_check_result.failure_reason
        )
    finally:
        reset_database_runtime_for_tests()
        if engine is not None:
            await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _POSTGRES_DSN,
    reason=f"set {_POSTGRES_DSN_ENV} to run the SK-M PostgreSQL proof",
)
async def test_skm_postgresql_uow_starts_repeatable_read_before_query() -> None:
    """The SaaS-target adapter configures isolation before its first SELECT."""

    assert _POSTGRES_DSN is not None
    engine = create_async_engine(_schema_qualified_dsn(_POSTGRES_DSN))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            uow = CommunityUnitOfWork(session)
            await uow.begin_consistent_read()
            isolation = await session.scalar(text("SHOW transaction_isolation"))
            assert str(isolation).replace("_", " ").upper() == "REPEATABLE READ"
            await session.rollback()
    finally:
        await engine.dispose()
