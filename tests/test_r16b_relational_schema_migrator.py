"""R16-B — Community RelationalSchemaMigrator adapter (IMP2, card ad8fbb03).

Covers the 6 test scenarios 1:1:

  ts_7aacc71a — ledger covers ALL current Community-owned _migrate_* steps.
  ts_5283c465 — golden replay: adapter plan vs baseline init_db schema.
  ts_7d52dffc — idempotent replay: re-run -> skipped, no drift.
  ts_7c1fc064 — fail-closed: failing step / invalid plan / absent migrator.
  ts_35ad79e3 — layer gate: core/ports pure, core !-> community, DB facade pure.
  ts_83050921 — conformance: isinstance + canonical DTOs + no parallel DTOs.

Tests are synchronous; async migrations are driven via ``asyncio.run`` inside a
single loop per test to avoid cross-loop aiosqlite engine issues.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from zipfile import ZipFile

import pytest

# Importing the core app module registers every ORM model on Base.metadata
# (the production-faithful way), so create_all builds the full schema and the
# raw-SQL _migrate_* find their columns. It does NOT create an engine
# (create_database is only called inside create_app()).
import okto_pulse.community.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
import okto_pulse.core.ports.relational_schema_migrator as _port_mod
import okto_pulse.community.adapters.relational_schema_steps as _steps_mod
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.relational_schema_migrator import (
    CREATE_ALL_BOUNDARY_STEP_ID,
    CommunityRelationalSchemaMigrator,
    build_community_migration_ledger,
    make_community_relational_schema_migrator,
)
from okto_pulse.core.ports import (
    MigrationPlan,
    MigrationResult,
    MigrationStep,
    RelationalSchemaMigrator,
    SchemaMigrationError,
    require_migrator,
)

CORE_DATABASE_PY = Path(_db_mod.__file__)
STEPS_PY = Path(_steps_mod.__file__)
PORT_PY = Path(_port_mod.__file__)
CORE_PACKAGE_DIR = CORE_DATABASE_PY.parents[1]  # .../okto_pulse/core
V030_SCHEMA_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "schema"
    / "okto-pulse-community-v0.3.0.sqlite3.zip"
)
V030_SCHEMA_FIXTURE_SHA256 = (
    "83fbb57d93cac37d4c7063fdabb2f6cf64951aeb0dd2f53df4295be94992e5dc"
)

_DATA_BOOTSTRAP_FUNCS = {
    "_seed_builtin_presets",
    "_reconcile_builtin_presets",
    "_reconcile_agent_permission_flags",
    "_bootstrap_default_discovery_intents",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _async_migrate_names_from_database() -> set[str]:
    """AST scan: every Community-owned ``async def _migrate_*`` step."""
    tree = ast.parse(STEPS_PY.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("_migrate_")
    }


def _step_callable_order() -> list[str]:
    """Concrete Community callable registration order, excluding create_all."""
    return list(_steps_mod.SCHEMA_STEP_CALLABLES)


async def _collect_schema(engine) -> dict[str, dict[str, list]]:
    from sqlalchemy import inspect as sa_inspect

    def _inspect(sync_conn):
        insp = sa_inspect(sync_conn)
        return {
            t: {
                "columns": sorted(c["name"] for c in insp.get_columns(t)),
                "foreign_keys": sorted(
                    (
                        tuple(fk.get("constrained_columns") or ()),
                        fk.get("referred_table"),
                        tuple(fk.get("referred_columns") or ()),
                        str((fk.get("options") or {}).get("ondelete") or "").upper(),
                    )
                    for fk in insp.get_foreign_keys(t)
                ),
            }
            for t in sorted(insp.get_table_names())
        }

    async with engine.connect() as conn:
        return await conn.run_sync(_inspect)


@pytest.fixture
def _isolate_engine():
    """Keep the explicit fixture name used by the DB-driving tests."""
    yield


def _det_migrator(callables, steps=None):
    """Build a deterministic migrator over small synthetic steps + sync callables."""
    if steps is None:
        steps = (
            MigrationStep("pre_a", 1, "pre_create_all", "d", True, False, "community"),
            MigrationStep(
                CREATE_ALL_BOUNDARY_STEP_ID,
                2,
                "create_all_boundary",
                "d",
                True,
                False,
                "community",
            ),
            MigrationStep(
                "post_b", 3, "post_create_all", "d", True, False, "community"
            ),
        )
    return CommunityRelationalSchemaMigrator(steps=steps, callables=callables)


# ===========================================================================
# ts_7aacc71a — ledger gate (mechanical: count + names + order + exclusions).
# ===========================================================================
def test_ts_7aacc71a_ledger_covers_all_migrate_functions():
    migrate_names = _async_migrate_names_from_database()
    ledger = build_community_migration_ledger()
    ledger_migrate_ids = {s.step_id for s in ledger if s.phase != "create_all_boundary"}

    # 1:1 coverage — no migration without a step, no step without a migration.
    assert ledger_migrate_ids == migrate_names, (
        "ledger drift: "
        f"missing_steps={sorted(migrate_names - ledger_migrate_ids)} "
        f"orphan_steps={sorted(ledger_migrate_ids - migrate_names)}"
    )
    # 71 = the historical ledger plus the Code Traceability schema/guard step,
    # the contextual Evidence persistence/classification authority,
    # the SK-A Refinement ambiguity-skip
    # column, SK-A/C7 quality-assessment persistence schema, the curated Spec
    # checklist mode, the human-facing Spec edition counter, and SK-B's
    # immutable guideline-policy authority, its B04 lifecycle substrate, and
    # B07 immutable compliance evidence/currentness fences, B08's ordered
    # impact substrate + sealed evidence guards, and B09 governed append-only
    # waiver lifecycle persistence, and SK-B3 semantic guideline authority,
    # plus the SK-B3 closure backfill of the 5-column unique authority index
    # on guideline_board_bindings (structural prerequisite of the
    # binding-configuration composite FK on migrated databases), and the
    # evidence-based legacy Task Validation -> Rejected convergence, and the
    # per-Spec Code Evidence Matrix coverage skip, and the audited restoration
    # of Spec validation pointers lost by historical Code Traceability effects,
    # and nullable Project structure storage without a legacy content backfill.
    assert len(migrate_names) == 71, (
        f"expected 71 _migrate_*, found {len(migrate_names)}"
    )
    assert len(ledger_migrate_ids) == 71
    ordered_ids = [step.step_id for step in ledger]
    assert ordered_ids.index(
        "_migrate_guideline_policy_lifecycle_substrate"
    ) < ordered_ids.index("_migrate_guideline_impact_substrate")
    assert ordered_ids.index("_migrate_guideline_impact_substrate") < ordered_ids.index(
        "_migrate_guideline_policy_v1_schema"
    )
    assert ordered_ids.index("_migrate_guideline_policy_v1_schema") < ordered_ids.index(
        "_migrate_guideline_impact_v1_schema"
    )
    assert ordered_ids.index("_migrate_guideline_impact_v1_schema") < ordered_ids.index(
        "_migrate_policy_compliance_v1_schema"
    )
    assert ordered_ids.index(
        "_migrate_policy_compliance_v1_schema"
    ) < ordered_ids.index("_migrate_policy_waiver_v1_schema")

    # Exactly ONE create_all_boundary step.
    boundary = [s for s in ledger if s.phase == "create_all_boundary"]
    assert len(boundary) == 1
    assert boundary[0].step_id == CREATE_ALL_BOUNDARY_STEP_ID

    # Data bootstrap is excluded — a schema plan never absorbs seeding.
    for excluded in _DATA_BOOTSTRAP_FUNCS:
        assert excluded not in ledger_migrate_ids


def test_ts_7aacc71a_ledger_order_matches_community_step_registry():
    ledger = build_community_migration_ledger()
    ledger_migrate_order = [
        s.step_id
        for s in sorted(ledger, key=lambda s: s.order)
        if s.step_id != CREATE_ALL_BOUNDARY_STEP_ID
    ]
    assert _step_callable_order() == ledger_migrate_order


def test_postgresql_policy_materialization_trigger_matches_json_column_type():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow

    source = STEPS_PY.read_text(encoding="utf-8")
    table_ddl = str(
        CreateTable(DomainEventRow.__table__).compile(dialect=postgresql.dialect())
    )

    # The mapped column is JSON (not JSONB), so every function in this trigger
    # block must use PostgreSQL's JSON family unless the SQL casts explicitly.
    assert "payload_json JSON NOT NULL" in table_ddl
    assert "jsonb_object_length(" not in source
    assert (
        "SELECT COUNT(*)\n"
        "                          FROM json_object_keys(event.payload_json)"
    ) in source
    assert "json_typeof(\n                          event.payload_json->" in source


def test_legacy_default_template_table_gains_nullable_checklist_mode(
    tmp_path,
    _isolate_engine,
):
    async def drive():
        from sqlalchemy import text

        _db_mod.create_database(
            f"sqlite+aiosqlite:///{tmp_path / 'legacy-default-template.db'}"
        )
        engine = _db_mod.get_engine()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE default_board_configurations ("
                    "id VARCHAR(36) PRIMARY KEY, "
                    "version INTEGER NOT NULL, "
                    "status VARCHAR(20) NOT NULL, "
                    "is_active BOOLEAN NOT NULL, "
                    "scope VARCHAR(50) NOT NULL, "
                    "settings_payload JSON NOT NULL, "
                    "created_by VARCHAR(255) NOT NULL"
                    ")"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO default_board_configurations "
                    "(id, version, status, is_active, scope, settings_payload, created_by) "
                    "VALUES ('legacy', 1, 'active', 1, 'global', '{}', 'admin')"
                )
            )

        await _steps_mod._migrate_add_default_config_spec_checklist_mode()
        await _steps_mod._migrate_add_default_config_spec_checklist_mode()

        async with engine.connect() as connection:
            columns = {
                row[1]
                for row in (
                    await connection.execute(
                        text("PRAGMA table_info(default_board_configurations)")
                    )
                ).all()
            }
            mode = await connection.scalar(
                text(
                    "SELECT spec_checklist_mode "
                    "FROM default_board_configurations WHERE id = 'legacy'"
                )
            )
        await engine.dispose()
        return columns, mode

    columns, mode = asyncio.run(drive())
    assert "spec_checklist_mode" in columns
    assert mode is None


def test_code_traceability_migration_upgrades_only_legacy_policy_values(
    tmp_path,
    monkeypatch,
):
    async def drive():
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import create_async_engine

        from okto_pulse.community.adapters.sqlalchemy_models import (
            Base,
            Board,
            DefaultBoardConfiguration,
        )

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'traceability-policy-mode.db'}"
        )
        monkeypatch.setattr(_steps_mod, "get_engine", lambda: engine)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(
                    Board.__table__.insert(),
                    [
                        {
                            "id": "legacy-off",
                            "name": "Legacy off",
                            "owner_id": "owner",
                            "settings": {
                                "max_scenarios_per_card": 7,
                                "code_traceability": {
                                    "mode": "off",
                                    "minimum_trust": "corroborated",
                                },
                            },
                        },
                        {
                            "id": "blocking",
                            "name": "Blocking",
                            "owner_id": "owner",
                            "settings": {"code_traceability": {"mode": "blocking"}},
                        },
                    ],
                )
                await connection.execute(
                    DefaultBoardConfiguration.__table__.insert().values(
                        id="legacy-null-template",
                        version=1,
                        status="active",
                        is_active=True,
                        scope="global",
                        settings_payload={"code_traceability": None},
                        created_by="owner",
                    )
                )

            first = await _steps_mod._migrate_code_traceability_schema()
            second = await _steps_mod._migrate_code_traceability_schema()

            async with engine.connect() as connection:
                boards = {
                    row.id: row.settings
                    for row in (
                        await connection.execute(
                            select(Board.id, Board.settings).where(
                                Board.id.in_(("legacy-off", "blocking"))
                            )
                        )
                    ).all()
                }
                template_payload = await connection.scalar(
                    select(DefaultBoardConfiguration.settings_payload).where(
                        DefaultBoardConfiguration.id == "legacy-null-template"
                    )
                )
            return first, second, boards, template_payload
        finally:
            await engine.dispose()

    first, second, boards, template_payload = asyncio.run(drive())
    assert first is None
    assert second == "skipped"
    assert boards["legacy-off"] == {
        "max_scenarios_per_card": 7,
        "code_traceability": {
            "mode": "advisory",
            "minimum_trust": "corroborated",
        },
    }
    assert boards["blocking"]["code_traceability"]["mode"] == "blocking"
    assert template_payload == {"code_traceability": {"mode": "advisory"}}


def test_legacy_specs_gain_backfilled_non_null_edition(
    tmp_path,
    _isolate_engine,
):
    async def drive():
        from sqlalchemy import text

        _db_mod.create_database(
            f"sqlite+aiosqlite:///{tmp_path / 'legacy-spec-edition.db'}"
        )
        engine = _db_mod.get_engine()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE specs ("
                    "id VARCHAR(36) PRIMARY KEY, "
                    "title VARCHAR(500) NOT NULL, "
                    "version INTEGER NOT NULL"
                    ")"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO specs (id, title, version) "
                    "VALUES ('legacy', 'Legacy spec', 321)"
                )
            )

        await _steps_mod._migrate_add_spec_edition()
        await _steps_mod._migrate_add_spec_edition()

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO specs (id, title, version) "
                    "VALUES ('new-default', 'Defaulted edition', 1)"
                )
            )

        async with engine.connect() as connection:
            columns = {
                row[1]: row
                for row in (
                    await connection.execute(text("PRAGMA table_info(specs)"))
                ).all()
            }
            row = (
                await connection.execute(
                    text("SELECT edition, version FROM specs WHERE id = 'legacy'")
                )
            ).one()
            defaulted_edition = await connection.scalar(
                text("SELECT edition FROM specs WHERE id = 'new-default'")
            )
        await engine.dispose()
        return columns, row, defaulted_edition

    columns, row, defaulted_edition = asyncio.run(drive())
    assert columns["edition"][3] == 1  # PRAGMA notnull
    assert row.edition == 1
    assert row.version == 321
    assert defaulted_edition == 1


def test_lifecycle_edition_migration_converges_nullable_sqlite_schema(
    tmp_path,
    monkeypatch,
) -> None:
    """A partially deployed schema converges without fabricating evidence."""

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def drive():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'lifecycle-editions.db'}"
        )
        monkeypatch.setattr(_steps_mod, "get_engine", lambda: engine)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE ideations ("
                    "id TEXT PRIMARY KEY, title TEXT NOT NULL, edition INTEGER)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE refinements ("
                    "id TEXT PRIMARY KEY, title TEXT NOT NULL, edition INTEGER, "
                    "skip_ambiguity_gate_edition INTEGER)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE specs ("
                    "id TEXT PRIMARY KEY, title TEXT NOT NULL, edition INTEGER)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE quality_assessment_receipts ("
                    "id TEXT PRIMARY KEY, subject_edition INTEGER)"
                )
            )
            await connection.execute(
                text("CREATE INDEX ix_ideations_title ON ideations(title)")
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER trg_ideations_title_guard "
                    "BEFORE UPDATE OF title ON ideations "
                    "WHEN NEW.title = '' BEGIN "
                    "SELECT RAISE(ABORT, 'title_required'); END"
                )
            )
            await connection.execute(
                text("INSERT INTO ideations VALUES ('i-1', 'Idea', NULL)")
            )
            await connection.execute(
                text("INSERT INTO refinements VALUES ('r-1', 'Refinement', NULL, NULL)")
            )
            await connection.execute(
                text("INSERT INTO specs VALUES ('s-1', 'Spec', 3)")
            )
            await connection.execute(
                text(
                    "INSERT INTO quality_assessment_receipts "
                    "VALUES ('legacy-evidence', NULL)"
                )
            )

        first = await _steps_mod._migrate_add_human_lifecycle_editions()
        second = await _steps_mod._migrate_add_human_lifecycle_editions()
        async with engine.connect() as connection:
            contracts = {}
            for table_name in ("ideations", "refinements", "specs"):
                rows = (
                    (
                        await connection.exec_driver_sql(
                            f'PRAGMA table_info("{table_name}")'
                        )
                    )
                    .mappings()
                    .all()
                )
                contracts[table_name] = next(
                    row for row in rows if row["name"] == "edition"
                )
            editions = {
                table_name: await connection.scalar(
                    text(f'SELECT edition FROM "{table_name}" LIMIT 1')
                )
                for table_name in ("ideations", "refinements", "specs")
            }
            legacy_evidence_edition = await connection.scalar(
                text(
                    "SELECT subject_edition FROM quality_assessment_receipts "
                    "WHERE id = 'legacy-evidence'"
                )
            )
            owned_objects = set(
                (
                    await connection.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE tbl_name = 'ideations' "
                            "AND type IN ('index', 'trigger')"
                        )
                    )
                ).scalars()
            )
        await engine.dispose()
        return (
            first,
            second,
            contracts,
            editions,
            legacy_evidence_edition,
            owned_objects,
        )

    (
        first,
        second,
        contracts,
        editions,
        legacy_evidence_edition,
        owned_objects,
    ) = asyncio.run(drive())

    assert first is None
    assert second == "skipped"
    assert editions == {"ideations": 1, "refinements": 1, "specs": 3}
    assert all(contract["notnull"] == 1 for contract in contracts.values())
    assert all(
        str(contract["dflt_value"]).strip("'\"") == "1"
        for contract in contracts.values()
    )
    assert legacy_evidence_edition is None
    assert "ix_ideations_title" in owned_objects
    assert "trg_ideations_title_guard" in owned_objects
    assert "trg_ideations_lifecycle_edition_insert" in owned_objects
    assert "trg_ideations_lifecycle_edition_update" in owned_objects


def test_fresh_create_all_installs_canonical_lifecycle_edition_guards(
    tmp_path,
) -> None:
    """The ORM creation path must emit every legacy-compatible SQLite guard."""

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    from okto_pulse.community.adapters.sqlalchemy_models import (
        Base,
        HUMAN_LIFECYCLE_EDITION_SUBJECT_TABLES,
        Ideation,
        Refinement,
        Spec,
        human_lifecycle_edition_sqlite_trigger_manifest,
    )

    async def drive():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'fresh-lifecycle-guards.db'}"
        )
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=(
                        Ideation.__table__,
                        Refinement.__table__,
                        Spec.__table__,
                    ),
                )
            )
            rows = (
                await connection.execute(
                    text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' "
                        "AND name LIKE 'trg_%_lifecycle_edition_%' "
                        "ORDER BY name"
                    )
                )
            ).all()
            dependency_board_guard = await connection.scalar(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'trg_spec_dependency_spec_board_update'"
                )
            )
            assert dependency_board_guard is None
            await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await connection.execute(
                text(
                    "INSERT INTO ideations "
                    "(id, board_id, title, status, edition, version, created_by) "
                    "VALUES ('i-1', 'b-1', 'Idea', 'draft', 1, 1, 'tester')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO refinements "
                    "(id, ideation_id, board_id, title, status, edition, "
                    "version, created_by) VALUES "
                    "('r-1', 'i-1', 'b-1', 'Refinement', 'draft', 1, 1, "
                    "'tester')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO specs "
                    "(id, board_id, title, status, edition, version, created_by) "
                    "VALUES ('s-1', 'b-1', 'Spec', 'draft', 1, 1, 'tester')"
                )
            )

        manifest = human_lifecycle_edition_sqlite_trigger_manifest()
        observed = {str(row.name): (str(row.tbl_name), str(row.sql)) for row in rows}
        for table_name in HUMAN_LIFECYCLE_EDITION_SUBJECT_TABLES:
            for operation in ("insert", "update"):
                trigger_name = f"trg_{table_name}_lifecycle_edition_{operation}"
                expected_table, expected_sql = manifest[trigger_name]
                observed_table, observed_sql = observed[trigger_name]
                assert observed_table == expected_table
                # SQLite omits IF NOT EXISTS when persisting CREATE statements.
                assert " ".join(observed_sql.split()) == " ".join(
                    expected_sql.replace(" IF NOT EXISTS", "").split()
                )

        for table_name in HUMAN_LIFECYCLE_EDITION_SUBJECT_TABLES:
            async with engine.begin() as connection:
                with pytest.raises(DBAPIError, match="lifecycle_edition_invalid"):
                    await connection.execute(
                        text(f'UPDATE "{table_name}" SET edition = 0')
                    )
        await engine.dispose()

    asyncio.run(drive())


def test_first_init_is_schema_complete_and_second_init_has_no_object_drift(
    tmp_path,
    _isolate_engine,
) -> None:
    """Catch release-gate drift across every user-owned SQLite schema object."""

    from sqlalchemy import text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        human_lifecycle_edition_sqlite_trigger_manifest,
    )

    async def drive():
        _db_mod.create_database(
            f"sqlite+aiosqlite:///{tmp_path / 'first-init-idempotence.db'}"
        )
        register_community_relational_schema_lifecycle()

        async def snapshot():
            async with _db_mod.get_engine().connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT type, name, tbl_name, sql "
                            "FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%' "
                            "ORDER BY type, name"
                        )
                    )
                ).all()
            return tuple(
                (
                    str(row.type),
                    str(row.name),
                    str(row.tbl_name),
                    " ".join(str(row.sql or "").split()),
                )
                for row in rows
            )

        await _db_mod.init_db()
        first = await snapshot()
        await _db_mod.init_db()
        second = await snapshot()
        await _db_mod.get_engine().dispose()
        return first, second

    first, second = asyncio.run(drive())
    expected_triggers = set(human_lifecycle_edition_sqlite_trigger_manifest())
    first_triggers = {
        name
        for object_type, name, _table_name, _sql in first
        if object_type == "trigger"
    }
    assert expected_triggers <= first_triggers
    assert second == first


def test_ts_7aacc71a_destructive_steps_are_explicitly_allowlisted():
    ledger = build_community_migration_ledger()
    destructive = {s.step_id for s in ledger if s.destructive}
    assert destructive == {
        "_migrate_drop_spec_skills",
        "_migrate_repair_known_fixture_fk_orphans",
    }
    # _migrate_agent_permissions carries the documented schema-tail nuance.
    perms = next(s for s in ledger if s.step_id == "_migrate_agent_permissions")
    assert perms.phase == "post_create_all"
    assert perms.metadata.get("runs_at_schema_tail") is True


# ===========================================================================
# ts_5283c465 — golden replay: adapter plan reproduces the init_db schema and
# does not alter an already-init_db'd baseline.
# ===========================================================================
def test_ts_5283c465_golden_replay_matches_baseline(tmp_path, _isolate_engine):
    async def drive():
        # Baseline: real init_db on DB1.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'baseline.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        baseline_schema = await _collect_schema(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()

        # Adapter: execute the plan (real _migrate_* + create_all) on a fresh DB2.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'adapter.db'}")
        migrator = make_community_relational_schema_migrator()
        plan = migrator.plan(target="golden")
        result = await migrator.aexecute(plan)
        adapter_schema = await _collect_schema(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return baseline_schema, adapter_schema, result

    baseline_schema, adapter_schema, result = asyncio.run(drive())
    assert result.is_success
    # Equivalent to baseline init_db (schema = tables + columns + foreign keys).
    assert adapter_schema == baseline_schema
    assert baseline_schema  # sanity: non-empty schema
    sprint_foreign_keys = baseline_schema["sprints"]["foreign_keys"]
    assert (
        ("origin_sprint_id",),
        "sprints",
        ("id",),
        "SET NULL",
    ) in sprint_foreign_keys
    assert (("origin_bug_id",), "cards", ("id",), "SET NULL") in sprint_foreign_keys


def test_ts_5283c465_replay_over_baseline_does_not_alter_schema(
    tmp_path, _isolate_engine
):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'base.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        schema_before = await _collect_schema(_db_mod.get_engine())
        # Replay the adapter plan OVER the init_db'd baseline (same DB).
        migrator = make_community_relational_schema_migrator()
        result = await migrator.aexecute(migrator.plan(target="overlay"))
        schema_after = await _collect_schema(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return schema_before, schema_after, result

    before, after, result = asyncio.run(drive())
    assert result.is_success
    assert after == before  # adapter replay does not alter the baseline


# ===========================================================================
# ts_7d52dffc — idempotent replay: re-run -> skipped, no drift.
# ===========================================================================
def test_ts_7d52dffc_idempotent_replay_no_drift(tmp_path, _isolate_engine):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}")
        migrator_a = make_community_relational_schema_migrator()
        plan = migrator_a.plan(target="idem")

        r1 = await migrator_a.aexecute(plan)
        s1 = await _collect_schema(_db_mod.get_engine())
        # Same instance -> adapter ledger reports every step skipped (no re-run).
        r2 = await migrator_a.aexecute(plan)
        s2 = await _collect_schema(_db_mod.get_engine())
        # Fresh instance -> the REAL migrations actually re-run; must not drift.
        migrator_b = make_community_relational_schema_migrator()
        r3 = await migrator_b.aexecute(migrator_b.plan(target="idem2"))
        s3 = await _collect_schema(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return (r1, r2, r3, s1, s2, s3)

    r1, r2, r3, s1, s2, s3 = asyncio.run(drive())
    total = len(build_community_migration_ledger())

    repair_step = "_migrate_repair_known_fixture_fk_orphans"
    recovery_convergence_step = "_migrate_global_discovery_recovery_control_plane"
    governed_queue_convergence_step = "_migrate_add_consolidation_work_kinds"
    delivery_convergence_step = "_migrate_global_discovery_delivery_contract"
    kb_governance_convergence_step = "_migrate_add_kb_governance_metadata"
    human_lifecycle_convergence_step = "_migrate_add_human_lifecycle_editions"
    validation_cycle_convergence_step = "_migrate_validation_cycle_editions"
    first_run_skip_steps = {
        repair_step,
        governed_queue_convergence_step,
        delivery_convergence_step,
        kb_governance_convergence_step,
        human_lifecycle_convergence_step,
        "_migrate_guideline_impact_substrate",
        # Fresh create_all already emits the canonical semantic shape, so the
        # legacy rebuilds have nothing to do on a clean DB.
        "_migrate_rebuild_guideline_import_candidates_semantic_shape",
        "_migrate_rebuild_guideline_policy_v1_semantic_alignment",
        "_migrate_drop_retired_guideline_impact_v1_triggers",
        "_migrate_seed_semantic_configurations_for_legacy_bindings",
        # Fresh create_all emits the complete SK-M ledger and guards.
        "_migrate_spec_dependency_schema",
        # The pre-create compatibility step has no legacy specs table to alter;
        # create_all emits the fail-closed Code Evidence Matrix skip column.
        "_migrate_add_code_evidence_coverage_skip",
        # Fresh create_all already emits the causal rejection columns and
        # audit table, with no legacy Validation evidence to classify.
        "_migrate_card_rejected_lifecycle",
        # No Spec on a fresh database can have a lost current validation
        # pointer, while create_all already emits the immutable audit table.
        "_migrate_restore_spec_validation_pointers",
        # Fresh create_all already emits nullable Project structure storage.
        "_migrate_add_project_structure_column",
        # The durable v3 epoch seals an immutable receipt even when a fresh
        # database has zero revision rows to rewrite. Fresh instances then
        # observe that receipt and skip without touching fingerprints.
    }
    replay_skip_steps = {
        repair_step,
        recovery_convergence_step,
        governed_queue_convergence_step,
        delivery_convergence_step,
        "_migrate_cognitive_source_revision_ledger",
        "_migrate_guideline_policy_v1_schema",
        "_migrate_guideline_policy_lifecycle_substrate",
        "_migrate_guideline_impact_substrate",
        "_migrate_guideline_impact_v1_schema",
        "_migrate_policy_compliance_v1_schema",
        "_migrate_policy_waiver_v1_schema",
        "_migrate_semantic_guideline_governance_schema",
        kb_governance_convergence_step,
        human_lifecycle_convergence_step,
        validation_cycle_convergence_step,
        "_migrate_code_traceability_schema",
        "_migrate_contextual_code_evidence_schema",
        "_migrate_semantic_pinpoint_v2_schema",
        "_migrate_knowledge_propagation_v2_schema",
        "_migrate_rebuild_guideline_import_candidates_semantic_shape",
        "_migrate_rebuild_guideline_policy_v1_semantic_alignment",
        "_migrate_drop_retired_guideline_impact_v1_triggers",
        "_migrate_seed_semantic_configurations_for_legacy_bindings",
        "_migrate_recompute_cognitive_source_fingerprints_v2",
        "_migrate_spec_dependency_schema",
        "_migrate_card_rejected_lifecycle",
        "_migrate_restore_spec_validation_pointers",
        "_migrate_add_code_evidence_coverage_skip",
        "_migrate_add_project_structure_column",
    }

    # First run: clean databases skip fixture repair and convergence steps
    # because create_all already emitted their complete schemas.
    assert r1.is_success
    assert len(r1.applied_steps) == total - len(first_run_skip_steps)
    assert {step.step_id for step in r1.skipped_steps} == first_run_skip_steps

    # Second run (same instance): everything skipped -> no drift.
    assert r2.is_success
    assert not r2.applied_steps
    assert len(r2.skipped_steps) == total
    assert s2 == s1

    # Fresh instance: real migrations re-execute idempotently -> no drift.
    assert r3.is_success
    assert {step.step_id for step in r3.skipped_steps} == replay_skip_steps
    assert len(r3.applied_steps) == total - len(replay_skip_steps)
    assert s3 == s1


def test_v030_installed_schema_upgrades_to_exact_semantic_v2_and_replays(
    tmp_path,
    _isolate_engine,
):
    """Exercise the current ledger over the physical tagged v0.3.0 database.

    This is deliberately not a synthetic ``create_all`` baseline: the zip was
    generated by the tagged v0.3.0 startup lifecycle and therefore preserves
    the exact SQLite constraint/index/trigger representation seen in an
    installed release.
    """

    assert (
        hashlib.sha256(V030_SCHEMA_FIXTURE.read_bytes()).hexdigest()
        == V030_SCHEMA_FIXTURE_SHA256
    )
    with ZipFile(V030_SCHEMA_FIXTURE) as archive:
        assert archive.namelist() == ["okto-pulse-community-v0.3.0.sqlite3"]
        archive.extractall(tmp_path)
    database_path = tmp_path / archive.namelist()[0]

    async def drive():
        from sqlalchemy import text

        from okto_pulse.community.adapters.sqlalchemy_models import (
            GuidelineBoardBindingRow,
            SemanticGuidelineAssessmentReceiptRow,
            SemanticGuidelineBindingConfigurationRow,
            SemanticGuidelineFindingRow,
            SemanticGuidelineLegacyMigrationRow,
            SemanticGuidelineMetricResultRow,
            SemanticGuidelineRevisionRow,
            SemanticGuidelineSkipRow,
            SemanticGuidelineWaiverEventRow,
            SemanticGuidelineWaiverRow,
            SemanticSubjectVersionEventRow,
            SemanticSubjectVersionRow,
        )

        semantic_tables = (
            SemanticGuidelineRevisionRow.__table__,
            SemanticGuidelineBindingConfigurationRow.__table__,
            SemanticSubjectVersionEventRow.__table__,
            SemanticSubjectVersionRow.__table__,
            SemanticGuidelineAssessmentReceiptRow.__table__,
            SemanticGuidelineMetricResultRow.__table__,
            SemanticGuidelineFindingRow.__table__,
            SemanticGuidelineWaiverRow.__table__,
            SemanticGuidelineWaiverEventRow.__table__,
            SemanticGuidelineSkipRow.__table__,
            SemanticGuidelineLegacyMigrationRow.__table__,
        )
        _db_mod.create_database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        first_migrator = make_community_relational_schema_migrator()
        first = await first_migrator.aexecute(
            first_migrator.plan(target="v0.3.0-to-semantic-v2")
        )
        engine = _db_mod.get_engine()

        async def snapshot():
            async with engine.connect() as connection:
                contracts = await connection.run_sync(
                    lambda sync_connection: {
                        table.name: _steps_mod._sqlite_owned_table_contract(
                            sync_connection,
                            table,
                        )
                        for table in semantic_tables
                    }
                )
                binding_contract = await connection.run_sync(
                    lambda sync_connection: _steps_mod._sqlite_owned_table_contract(
                        sync_connection,
                        GuidelineBoardBindingRow.__table__,
                    )
                )
                owned_schema = tuple(
                    (
                        str(row.type),
                        str(row.name),
                        str(row.tbl_name),
                        str(row.sql),
                    )
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT type, name, tbl_name, sql "
                                "FROM sqlite_master "
                                "WHERE name LIKE 'semantic_%' "
                                "OR name LIKE 'trg_sgv3_%' "
                                "OR name = "
                                "'uq_guideline_binding_exact_authority' "
                                "ORDER BY type, name"
                            )
                        )
                    ).all()
                )
            return contracts, binding_contract, owned_schema

        before_replay = await snapshot()
        replay_migrator = make_community_relational_schema_migrator()
        replay = await replay_migrator.aexecute(
            replay_migrator.plan(target="v0.3.0-replay")
        )
        after_replay = await snapshot()
        await engine.dispose()
        return first, replay, before_replay, after_replay

    first, replay, before_replay, after_replay = asyncio.run(drive())
    assert first.is_success
    assert replay.is_success
    assert before_replay == after_replay
    semantic_contracts, binding_contract, owned_schema = before_replay
    assert len(semantic_contracts) == 11
    assert all(
        contract["observed"] == contract["expected"]
        for contract in semantic_contracts.values()
    )
    assert binding_contract["observed"] == binding_contract["expected"]
    assert owned_schema
    from okto_pulse.community import kg_recovery_only as recovery

    with sqlite3.connect(database_path) as connection:
        schema_objects = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, COALESCE(sql, '') "
                "FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name"
            )
        )
        exact_ack_columns = tuple(
            (str(row[1]), str(row[2]), int(row[3]))
            for row in connection.execute(
                "PRAGMA table_xinfo(exact_rebuild_consolidation_ack_journal)"
            )
        )
    # This exact installed-fixture upgrade is the terminal Community schema,
    # including migration-owned indexes and triggers (not merely ORM tables).
    assert len(schema_objects) == 863
    assert exact_ack_columns[10:13] == (
        ("membership_content_hash", "VARCHAR(64)", 1),
        ("audit_content_hash", "VARCHAR(64)", 1),
        ("consolidation_session_id", "VARCHAR(36)", 1),
    )
    assert tuple(
        (object_type, name, table_name)
        for object_type, name, table_name, _sql in schema_objects
        if name.startswith("exact_rebuild_consolidation_")
        or name == "ix_exact_rebuild_ack_scope"
    ) == (
        (
            "index",
            "ix_exact_rebuild_ack_scope",
            "exact_rebuild_consolidation_ack_journal",
        ),
        (
            "table",
            "exact_rebuild_consolidation_ack_journal",
            "exact_rebuild_consolidation_ack_journal",
        ),
        (
            "table",
            "exact_rebuild_consolidation_compensations",
            "exact_rebuild_consolidation_compensations",
        ),
    )
    assert recovery.MAX_RECOVERY_SQLITE_SCHEMA_OBJECTS >= 4 * len(schema_objects)
    assert (
        len(json.dumps(schema_objects, sort_keys=True, separators=(",", ":")).encode())
        < recovery.MAX_LEGACY_PROTECTED_QUEUE_BYTES
    )
    fingerprint = recovery._sqlite_schema_fingerprint(database_path)
    assert len(fingerprint) == 64
    assert fingerprint == recovery._sqlite_schema_fingerprint(database_path)
    logical_fingerprints = recovery._sqlite_logical_fingerprints(database_path)
    assert set(recovery.SQLITE_LOGICAL_STREAMING_POLICIES).issubset(
        logical_fingerprints
    )
    assert all(
        len(logical_fingerprints[table_name]) == 64
        for table_name in recovery.SQLITE_LOGICAL_STREAMING_POLICIES
    )


# ===========================================================================
# ts_7c1fc064 — fail-closed: failing step / invalid plan / absent migrator.
# ===========================================================================
def test_ts_7c1fc064_failing_step_yields_partial_never_success():
    def ok():
        return None

    def boom():
        raise RuntimeError("ALTER failed")

    migrator = _det_migrator(
        {"pre_a": ok, CREATE_ALL_BOUNDARY_STEP_ID: ok, "post_b": boom}
    )
    result = migrator.execute(migrator.plan(target="t"))

    assert not result.is_success
    assert result.status == "partial"  # earlier steps applied
    assert result.failed_step is not None
    assert result.failed_step.step_id == "post_b"
    assert result.failed_step.phase == "post_create_all"
    assert result.failed_step.status == "failed"
    assert "RuntimeError" in (result.failed_step.failure_reason or "")
    assert result.failed_step.remediation
    assert {s.step_id for s in result.applied_steps} == {
        "pre_a",
        CREATE_ALL_BOUNDARY_STEP_ID,
    }
    # MigrationResult fail-closed invariant: success + failed step is impossible.
    with pytest.raises(ValueError):
        MigrationResult(status="success", failed_steps=(result.failed_step,))


def test_ts_7c1fc064_first_step_failure_is_failed_not_partial():
    def boom():
        raise RuntimeError("x")

    migrator = _det_migrator(
        {
            "pre_a": boom,
            CREATE_ALL_BOUNDARY_STEP_ID: lambda: None,
            "post_b": lambda: None,
        }
    )
    result = migrator.execute(migrator.plan(target="t"))
    assert result.status == "failed"  # nothing applied before the failure
    assert not result.applied_steps
    assert not result.is_success


@pytest.mark.parametrize(
    ("stored_permissions", "expected_failure"),
    [
        ("{", "JSONDecodeError"),
        ("{}", "ValueError"),
        ('"board:read"', "ValueError"),
        ('["board:read", 7]', "ValueError"),
    ],
    ids=(
        "invalid-json",
        "json-object",
        "json-string",
        "json-array-with-non-string",
    ),
)
def test_ts_7c1fc064_agent_permission_migration_rolls_back_and_fails_closed(
    monkeypatch,
    stored_permissions,
    expected_failure,
):
    class _MalformedLegacyPermissionResult:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "id": "agent-malformed",
                    "permissions": stored_permissions,
                }
            ]

    class _TrackingSession:
        def __init__(self):
            self.rollbacks = 0
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement, parameters=None):
            return _MalformedLegacyPermissionResult()

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    session = _TrackingSession()
    monkeypatch.setattr(
        _steps_mod,
        "get_session_factory",
        lambda: lambda: session,
    )
    permission_step_id = "_migrate_agent_permissions"
    steps = (
        MigrationStep("pre_a", 1, "pre_create_all", "d", True, False, "community"),
        MigrationStep(
            CREATE_ALL_BOUNDARY_STEP_ID,
            2,
            "create_all_boundary",
            "d",
            True,
            False,
            "community",
        ),
        MigrationStep(
            permission_step_id,
            3,
            "post_create_all",
            "d",
            True,
            False,
            "community",
        ),
    )
    migrator = _det_migrator(
        {
            "pre_a": lambda: None,
            CREATE_ALL_BOUNDARY_STEP_ID: lambda: None,
            permission_step_id: _steps_mod._migrate_agent_permissions,
        },
        steps=steps,
    )

    result = migrator.execute(migrator.plan(target="malformed-agent-permissions"))

    assert result.status == "partial"
    assert not result.is_success
    assert result.failed_step is not None
    assert result.failed_step.step_id == permission_step_id
    assert expected_failure in (result.failed_step.failure_reason or "")
    assert {step.step_id for step in result.applied_steps} == {
        "pre_a",
        CREATE_ALL_BOUNDARY_STEP_ID,
    }
    assert session.rollbacks == 1
    assert session.commits == 0


def test_ts_7c1fc064_missing_callable_is_fail_closed():
    migrator = _det_migrator({"pre_a": lambda: None})  # boundary + post unbound
    result = migrator.execute(migrator.plan(target="t"))
    assert not result.is_success
    assert result.failed_step is not None
    assert result.failed_step.failure_reason == "no_callable_bound"


def test_ts_7c1fc064_invalid_plan_raises_schema_migration_error():
    migrator = make_community_relational_schema_migrator()

    # Two create_all boundaries.
    two_boundaries = MigrationPlan(
        plan_id="bad",
        target="t",
        steps=(
            MigrationStep("a", 1, "create_all_boundary", "d", True, False, "c"),
            MigrationStep("b", 2, "create_all_boundary", "d", True, False, "c"),
        ),
    )
    with pytest.raises(SchemaMigrationError):
        migrator.validate_plan(two_boundaries)

    # Empty step_id.
    empty_id = MigrationPlan(
        plan_id="bad",
        target="t",
        steps=(MigrationStep("", 1, "pre_create_all", "d", True, False, "c"),),
    )
    with pytest.raises(SchemaMigrationError):
        migrator.validate_plan(empty_id)

    # Phase out of order (post before boundary by order).
    out_of_order = MigrationPlan(
        plan_id="bad",
        target="t",
        steps=(
            MigrationStep("p", 1, "post_create_all", "d", True, False, "c"),
            MigrationStep(
                CREATE_ALL_BOUNDARY_STEP_ID,
                2,
                "create_all_boundary",
                "d",
                True,
                False,
                "c",
            ),
        ),
    )
    with pytest.raises(SchemaMigrationError):
        migrator.validate_plan(out_of_order)


def test_ts_7c1fc064_absent_migrator_fail_closed():
    with pytest.raises(SchemaMigrationError) as exc:
        require_migrator(None, target="community")
    assert exc.value.failure_reason == "migrator_absent"
    assert exc.value.remediation
    migrator = make_community_relational_schema_migrator()
    assert require_migrator(migrator) is migrator  # present -> passthrough


# ===========================================================================
# ts_35ad79e3 — layer gate.
# ===========================================================================
def _imported_modules(py_path: Path) -> set[str]:
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_ts_35ad79e3_core_ports_is_pure_no_sqlalchemy_no_community():
    imported = _imported_modules(PORT_PY)
    for mod in imported:
        low = mod.lower()
        assert "sqlalchemy" not in low, f"core/ports imports sqlalchemy: {mod!r}"
        assert "okto_pulse.community" not in low, (
            f"core/ports imports community: {mod!r}"
        )
        assert "infra.database" not in low, (
            f"core/ports imports infra.database: {mod!r}"
        )


def test_ts_35ad79e3_core_does_not_import_community():
    offenders: list[str] = []
    for py in CORE_PACKAGE_DIR.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            for mod in _imported_modules(py):
                if mod.startswith("okto_pulse.community"):
                    offenders.append(f"{py}: {mod}")
        except SyntaxError:
            continue
    assert offenders == [], f"core imports community: {offenders}"


def test_ts_35ad79e3_core_database_no_lifecycle_sql():
    source = CORE_DATABASE_PY.read_text(encoding="utf-8")
    assert "okto_pulse.core.ports.relational_runtime" in source
    assert "Base.metadata.create_all" not in source
    assert "async def _migrate_" not in source


def test_ts_35ad79e3_adapter_module_is_layer_isolated():
    adapter_py = Path(
        __import__(
            "okto_pulse.community.adapters.relational_schema_migrator",
            fromlist=["__file__"],
        ).__file__
    )
    imported = _imported_modules(adapter_py)
    # Top-level imports: only the pure core.ports contract — no sqlalchemy,
    # no infra.database, no engine (those are lazy inside the factory).
    for mod in imported:
        low = mod.lower()
        assert "sqlalchemy" not in low, f"adapter top-level imports sqlalchemy: {mod!r}"
        assert "infra.database" not in low, (
            f"adapter top-level imports infra.database: {mod!r}"
        )
    assert any(m == "okto_pulse.core.ports" for m in imported)


# ===========================================================================
# ts_83050921 — conformance.
# ===========================================================================
def test_ts_83050921_isinstance_of_port_protocol():
    migrator = make_community_relational_schema_migrator()
    assert isinstance(migrator, RelationalSchemaMigrator)


def test_ts_83050921_plan_and_execute_traffic_canonical_dtos():
    migrator = make_community_relational_schema_migrator()
    plan = migrator.plan(target="conf")
    assert type(plan) is MigrationPlan
    assert all(type(s) is MigrationStep for s in plan.steps)

    # execute returns the canonical MigrationResult (deterministic small plan).
    det = _det_migrator(
        {
            "pre_a": lambda: None,
            CREATE_ALL_BOUNDARY_STEP_ID: lambda: None,
            "post_b": lambda: None,
        }
    )
    result = det.execute(det.plan(target="t"))
    assert type(result) is MigrationResult
    assert result.is_success
    assert all(type(s) is MigrationStep for s in build_community_migration_ledger())


def test_ts_83050921_adapter_defines_no_parallel_dtos():
    adapter_py = Path(
        __import__(
            "okto_pulse.community.adapters.relational_schema_migrator",
            fromlist=["__file__"],
        ).__file__
    )
    tree = ast.parse(adapter_py.read_text(encoding="utf-8"))
    class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    # The adapter declares ONLY its implementation class — no parallel DTOs.
    assert class_names == {"CommunityRelationalSchemaMigrator"}
    for forbidden in {
        "MigrationStep",
        "MigrationPlan",
        "MigrationResult",
        "MigrationStepResult",
    }:
        assert forbidden not in class_names

    # The DTOs it traffics are the canonical port classes (identity check).
    step = build_community_migration_ledger()[0]
    assert step.__class__ is MigrationStep
    assert (
        step.__class__.__module__ == "okto_pulse.core.ports.relational_schema_migrator"
    )
