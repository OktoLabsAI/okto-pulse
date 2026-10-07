"""Current relational creation and Core ownership boundaries, without upgrades."""
import ast
import asyncio
from pathlib import Path
import pytest
import okto_pulse.core.infra.database as _db_mod
import okto_pulse.core.ports.schema_lifecycle as lifecycle_port
from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle

CORE_DATABASE_PY = Path(_db_mod.__file__)
CORE_PACKAGE_DIR = CORE_DATABASE_PY.parents[1]
PORT_PY = Path(lifecycle_port.__file__)

@pytest.fixture
def _isolate_engine():
    lifecycle_port.reset_relational_schema_lifecycle_orchestrator()
    try:
        yield
    finally:
        lifecycle_port.reset_relational_schema_lifecycle_orchestrator()

def test_fresh_create_all_installs_canonical_lifecycle_edition_guards(
    tmp_path,
) -> None:
    """The ORM creation path must emit every current SQLite guard."""

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
            from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
            await connection.execute(
                Spec.__table__.insert().values(
                    id="s-1", board_id="b-1", title="Spec", status="draft",
                    edition=1, version=1, created_by="tester",
                    architecture_adoption=ArchitectureAdoptionScope(
                        board_id="b-1", spec_id="s-1", adopted_in_edition=1,
                        actor_id="tester", inherited_resource_ids=(),
                    ).model_dump(mode="json"),
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
