"""Convergence investigation using the real offline cut and lifecycle writers."""

import hashlib
import json

import pytest
import pytest_asyncio
from sqlalchemy import inspect, insert
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.ports.relational_runtime import configure_database_runtime
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters.data_bootstrapper import make_community_data_bootstrapper
from okto_pulse.community.adapters.relational_schema_migrator import make_community_relational_schema_migrator
from okto_pulse.community.adapters.relational_schema_lifecycle import CommunityRelationalSchemaLifecycleOrchestrator
from okto_pulse.community.adapters.retirement_schema_storage import _rows, _objects
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Spec
from legacy_sprint_schema import Base as HistoricalBase, RETIRED_TABLES, Sprint
from test_retirement_offline_schema import schema
from test_retirement_offline_materialization import prepare, setup


@pytest_asyncio.fixture
async def database(tmp_path):
    # Synthetic old origin plus the real current protected storage surfaces.
    # to_metadata copies do NOT copy DDL event listeners. Use the actual live
    # metadata so missing audit guards cannot be mistaken for bootstrap drift.
    path = tmp_path / "convergence.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    configure_database_runtime(runtime=db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine)))
    # The real v0.3.4 init_db creates these two tables before create_all. Its
    # physical TIMESTAMP/default/PK contract differs from ORM-only creation.
    await steps._migrate_add_event_tables()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(lambda sync: HistoricalBase.metadata.create_all(sync,
            tables=[HistoricalBase.metadata.tables[name] for name in RETIRED_TABLES]))
        await connection.exec_driver_sql("ALTER TABLE cards ADD COLUMN sprint_id VARCHAR(36) REFERENCES sprints(id) ON DELETE SET NULL")
        await connection.exec_driver_sql("CREATE INDEX ix_cards_sprint_id ON cards(sprint_id)")
        for suffix in ("a", "b"):
            await connection.execute(insert(Board).values(id=f"board-{suffix}", name=suffix, owner_id="owner"))
            await connection.execute(insert(Spec).values(id=f"spec-{suffix}", board_id=f"board-{suffix}",
                title=suffix, created_by="owner"))
        await connection.execute(insert(Sprint).values(id="sprint", board_id="board-a", spec_id="spec-a",
            title="Historical Sprint", created_by="owner", status="closed"))
    try:
        yield engine, path
    finally:
        await engine.dispose()


def snapshot(connection):
    return {name: (rows["count"], hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())
        for kind, name, _, _ in _objects(connection) if kind == "table"
        for rows in [_rows(connection, name)]}


def operational_schema(connection):
    schema = inspect(connection)
    result = {}
    for name in sorted(schema.get_table_names()):
        result[name] = {
            "columns": [(col["name"], str(col["type"]), col["nullable"], col["default"], col["primary_key"])
                for col in schema.get_columns(name)],
            "foreign_keys": sorted(schema.get_foreign_keys(name), key=repr),
            "indexes": sorted(schema.get_indexes(name), key=repr),
            "unique": sorted(schema.get_unique_constraints(name), key=repr),
            "checks": sorted(schema.get_check_constraints(name), key=repr),
        }
    # SQLite partial-index predicates are SQLAlchemy TextClause instances;
    # compare their SQL, not Python object identities in two separate engines.
    return json.loads(json.dumps(result, default=str, sort_keys=True))


async def lifecycle(runtime):
    configure_database_runtime(runtime=runtime)
    # Explicitly exercise the migration writers on a disposable candidate under
    # the schema lock. This is NOT a runtime admission path or a ready receipt.
    orchestrator = CommunityRelationalSchemaLifecycleOrchestrator(
        migrator=make_community_relational_schema_migrator(),
        bootstrapper=make_community_data_bootstrapper())
    async with db._serialized_schema_lifecycle(runtime):
        await orchestrator.initialize_schema()


async def audit_rows(connection):
    return {row["id"]: dict(row) for row in (await connection.exec_driver_sql("SELECT * FROM permission_introduction_audit")).mappings()}


@pytest.mark.asyncio
async def test_cut_candidate_converges_and_bootstrap_replay_only_appends_noop_permission_audits(database, tmp_path):
    engine, _ = database
    args, graphs = await setup(engine, tmp_path)
    fresh = None
    try:
        run = await prepare(args, graphs)
        await schema(args, graphs, run)
        async with engine.connect() as connection:
            before = await connection.run_sync(snapshot)
            before_cells = {table: [dict(row) for row in (await connection.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id")).mappings()]
                for table in ("cards", "boards")}
        await lifecycle(args[0])
        async with engine.connect() as connection:
            after = await connection.run_sync(snapshot)
            audit_before_replay = await audit_rows(connection)
            upgraded = await connection.run_sync(operational_schema)
            assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
            after_cells = {table: [dict(row) for row in (await connection.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id")).mappings()]
                for table in ("cards", "boards")}
        for table, allowed in (("cards", {"position"}), ("boards", {"realm_id"})):
            assert len(before_cells[table]) == len(after_cells[table])
            for old, new in zip(before_cells[table], after_cells[table]):
                assert {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)} <= allowed
        assert before["retirement_data_checkpoints"] == after["retirement_data_checkpoints"]
        fresh = db.configure_community_database(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
        await lifecycle(fresh)
        async with fresh.engine.connect() as connection:
            empty = await connection.run_sync(operational_schema)
        differences = {name: (upgraded.get(name), empty.get(name))
            for name in upgraded.keys() | empty.keys() if upgraded.get(name) != empty.get(name)}
        await lifecycle(args[0])
        async with engine.connect() as connection:
            replay = await connection.run_sync(snapshot)
            audit_after_replay = await audit_rows(connection)
        assert not differences, differences
        assert {name for name in after.keys() | replay.keys() if after.get(name) != replay.get(name)} == {"permission_introduction_audit"}
        assert all(audit_after_replay[identity] == row for identity, row in audit_before_replay.items())
        appended = [row for identity, row in audit_after_replay.items() if identity not in audit_before_replay]
        assert appended and all(row["mutation_count"] == 0 and not row["owner_review_required"] for row in appended)
        # Ordinary startup records its no-op reconciliation runs. The final
        # migration coordinator must retain its own receipt and avoid replaying
        # these writers merely because an operator retries after a lost response.
    finally:
        if fresh is not None:
            await fresh.engine.dispose()
        for graph in graphs:
            graph.database.close()
