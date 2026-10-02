"""Fresh format, restart, and refusal before persistent SQLite effects."""

import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, StorageFormatError,
    current_schema_contract, initialize_current_schema, require_current_database_file,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    CommunityRelationalSchemaLifecycleOrchestrator,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    CommunityDatabaseRuntime, build_community_session_factory, install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, DiscoveryIntent


@pytest.fixture(scope="module")
def contract():
    return current_schema_contract()


def snapshot(path):
    return {item.name: item.read_bytes() for item in path.parent.glob(path.name + "*")
            if item.is_file() and not item.name.endswith(".lock")}


@pytest.mark.asyncio
async def test_fresh_schema_restart_preserves_data_and_identity(tmp_path, contract):
    path = tmp_path / "pulse.db"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    install_community_sqlite_pragmas(engine)
    try:
        await initialize_current_schema(engine, contract)
        sessions = build_community_session_factory(engine)
        async with sessions() as session:
            session.add(Board(id="current", realm_id="local", name="Preserve", owner_id="owner"))
            await session.commit()
    finally:
        await engine.dispose()
    before = snapshot(path)
    require_current_database_file(url, contract)
    assert snapshot(path) == before
    engine = create_async_engine(url)
    install_community_sqlite_pragmas(engine)
    try:
        await initialize_current_schema(engine, contract)
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT name FROM boards WHERE id='current'")).scalar() == "Preserve"
            assert (await connection.exec_driver_sql("PRAGMA application_id")).scalar() == APPLICATION_ID
            assert (await connection.exec_driver_sql("PRAGMA user_version")).scalar() == SCHEMA_VERSION
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "PRAGMA user_version=399",
    "PRAGMA application_id=0",
    "ALTER TABLE cards ADD COLUMN sprint_id TEXT",
    "ALTER TABLE cards ADD COLUMN knowledge_bases JSON",
    "ALTER TABLE semantic_guideline_revisions ADD COLUMN authority_state TEXT",
    "ALTER TABLE semantic_guideline_revisions ADD COLUMN legacy_rules_digest TEXT",
    "ALTER TABLE semantic_subject_versions ADD COLUMN editor_source TEXT",
    "ALTER TABLE semantic_subject_version_events ADD COLUMN editor_source TEXT",
    "CREATE TABLE semantic_guideline_legacy_migrations (migration_id TEXT PRIMARY KEY)",
    "CREATE TABLE card_rejected_lifecycle_migrations (migration_id TEXT PRIMARY KEY)",
    "CREATE TABLE spec_validation_pointer_repairs (migration_id TEXT PRIMARY KEY)",
    "CREATE TABLE sprints (id TEXT PRIMARY KEY)",
    "DROP INDEX ix_boards_realm_id",
    "DROP TRIGGER trg_global_discovery_source_revision_singleton_delete_guard",
])
async def test_incompatible_schema_is_refused_without_wal_or_content_changes(tmp_path, contract, mutation):
    path = tmp_path / "incompatible.db"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    try:
        await initialize_current_schema(engine, contract)
    finally:
        await engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(mutation)
        connection.commit()
    before = snapshot(path)
    engine = create_async_engine(url)
    try:
        with pytest.raises(StorageFormatError):
            install_community_sqlite_pragmas(engine)
        with pytest.raises(StorageFormatError):
            await initialize_current_schema(engine, contract)
    finally:
        await engine.dispose()
    assert snapshot(path) == before


@pytest.mark.asyncio
async def test_connection_rechecks_format_before_wal_if_file_changes_after_composition(tmp_path):
    path = tmp_path / "race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    install_community_sqlite_pragmas(engine)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE old_format (id INTEGER)")
        connection.commit()
    before = snapshot(path)
    try:
        with pytest.raises(StorageFormatError):
            async with engine.connect():
                pytest.fail("incompatible connection admitted")
    finally:
        await engine.dispose()
    assert snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change_schema", [False, True])
async def test_pending_wal_is_inspected_without_touching_source_files(tmp_path, contract, change_schema):
    path = tmp_path / "pending-wal.db"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    try:
        await initialize_current_schema(engine, contract)
    finally:
        await engine.dispose()
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        if change_schema:
            writer.execute("ALTER TABLE cards ADD COLUMN sprint_id TEXT")
        else:
            writer.execute("INSERT INTO boards(id, name, owner_id, realm_id) VALUES ('wal-row', 'Keep', 'owner', 'local')")
        writer.commit()
        before = snapshot(path)
        assert before[path.name + "-wal"]
        if change_schema:
            with pytest.raises(StorageFormatError, match="schema_fingerprint"):
                require_current_database_file(url, contract)
        else:
            require_current_database_file(url, contract)
        assert snapshot(path) == before


@pytest.mark.asyncio
async def test_creation_failure_rolls_back_ddl_and_version_then_can_retry(tmp_path, contract):
    path = tmp_path / "interrupted.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted creation")
    event.listen(Base.metadata, "after_create", interrupted)
    try:
        with pytest.raises(RuntimeError, match="interrupted creation"):
            await initialize_current_schema(engine, contract)
    finally:
        event.remove(Base.metadata, "after_create", interrupted)
    try:
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT count(*) FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'")).scalar() == 0
            assert (await connection.exec_driver_sql("PRAGMA user_version")).scalar() == 0
        await initialize_current_schema(engine, contract)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lifecycle_never_seeds_incompatible_storage(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sprints (id TEXT)")
        connection.commit()
    before = snapshot(path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    runtime = CommunityDatabaseRuntime(engine, build_community_session_factory(engine))
    seed = AsyncMock()
    lifecycle = CommunityRelationalSchemaLifecycleOrchestrator(runtime_provider=lambda: runtime, seed=seed)
    try:
        with pytest.raises(StorageFormatError):
            await lifecycle.initialize_schema()
        seed.assert_not_awaited()
    finally:
        await runtime.close()
    assert snapshot(path) == before


@pytest.mark.asyncio
async def test_current_source_fence_advances_and_cannot_be_removed(tmp_path, contract):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fence.db'}")
    try:
        await initialize_current_schema(engine, contract)
        async with engine.begin() as connection:
            before = (await connection.exec_driver_sql("SELECT revision FROM global_discovery_source_revision")).scalar_one()
            await connection.exec_driver_sql("INSERT INTO boards(id,name,owner_id,realm_id) VALUES ('current', 'Board', 'owner', 'local')")
            after = (await connection.exec_driver_sql("SELECT revision FROM global_discovery_source_revision")).scalar_one()
            assert after > before
        async with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.exec_driver_sql("DELETE FROM global_discovery_source_revision")
    finally:
        await engine.dispose()


def test_non_database_file_is_preserved(tmp_path, contract):
    path = tmp_path / "not-a-database"
    path.write_bytes(b"not SQLite, preserve this")
    before = snapshot(path)
    with pytest.raises(StorageFormatError, match="unreadable_database"):
        require_current_database_file(f"sqlite+aiosqlite:///{path}", contract)
    assert snapshot(path) == before


@pytest.mark.asyncio
async def test_current_catalog_seed_is_repeatable_and_preserves_user_choices(tmp_path, contract, monkeypatch):
    from okto_pulse.community.adapters import current_data_seeds, permission_preset_reconciliation

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalogs.db'}")
    sessions = build_community_session_factory(engine)
    monkeypatch.setattr(current_data_seeds, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(permission_preset_reconciliation, "get_session_factory", lambda: sessions)
    try:
        await initialize_current_schema(engine, contract)
        await current_data_seeds.seed_current_catalogs()
        async with sessions() as session:
            rows = list((await session.execute(select(DiscoveryIntent))).scalars())
            ids = {row.name: row.id for row in rows}
            assert ids
            rows[0].active = False
            rows[0].label = "My label"
            changed_id = rows[0].id
            session.add(DiscoveryIntent(name="custom", label="Custom", category="custom",
                                        tool_binding="custom.tool", is_seed=False))
            await session.commit()
        await current_data_seeds.seed_current_catalogs()
        async with sessions() as session:
            rows = list((await session.execute(select(DiscoveryIntent))).scalars())
            assert {row.name: row.id for row in rows if row.is_seed} == ids
            selected = next(row for row in rows if row.id == changed_id)
            assert selected.active is False and selected.label == "My label"
            assert next(row for row in rows if row.name == "custom").tool_binding == "custom.tool"
    finally:
        await engine.dispose()
