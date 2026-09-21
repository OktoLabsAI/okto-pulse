"""Old ordinal constraints upgrade without rewriting retained evidence bytes."""

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from okto_pulse.community.adapters.retirement_data_journal import ensure_retirement_data_journal


@pytest.fixture(params=[3, 5])
def old_max(request):
    return request.param


@pytest.fixture
async def old_journal(tmp_path, old_max):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old-journal.db'}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"""CREATE TABLE retirement_data_checkpoints (
            migration_id VARCHAR(128) NOT NULL, ordinal INTEGER NOT NULL,
            record_json JSON NOT NULL, sha256 VARCHAR(64) NOT NULL,
            PRIMARY KEY(migration_id,ordinal),
            CONSTRAINT ck_retirement_checkpoint_ordinal CHECK (ordinal >= 0 AND ordinal <= {old_max}))""")
        for ordinal in (0, old_max):
            await connection.exec_driver_sql("INSERT INTO retirement_data_checkpoints VALUES (?,?,?,?)",
                ("original", ordinal, '{ "opaque" : "Ω original spacing" }', str(ordinal) * 64))
        for operation in ("UPDATE", "DELETE"):
            await connection.exec_driver_sql(f"CREATE TRIGGER retirement_data_no_{operation.lower()} "
                f"BEFORE {operation} ON retirement_data_checkpoints BEGIN SELECT RAISE(ABORT,'immutable'); END")
    try:
        yield engine
    finally:
        await engine.dispose()


async def snapshot(engine):
    async with engine.connect() as connection:
        records = (await connection.exec_driver_sql("SELECT migration_id,ordinal,CAST(record_json AS BLOB),sha256 "
            "FROM retirement_data_checkpoints ORDER BY ordinal")).all()
        schema = (await connection.exec_driver_sql("SELECT type,name,sql FROM sqlite_schema WHERE "
            "name LIKE '%retirement%' ORDER BY name")).all()
        return records, schema


@pytest.mark.asyncio
async def test_expansion_keeps_raw_cells_and_immutable_triggers(old_journal):
    before, _ = await snapshot(old_journal)
    async with old_journal.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await ensure_retirement_data_journal(connection)
        await connection.commit()
    assert (await snapshot(old_journal))[0] == before
    for operation in ("DELETE FROM retirement_data_checkpoints", "UPDATE retirement_data_checkpoints SET sha256='changed'"):
        async with old_journal.begin() as connection:
            with pytest.raises(Exception, match="immutable"):
                await connection.exec_driver_sql(operation)
    async with old_journal.begin() as connection:
        await connection.exec_driver_sql("INSERT INTO retirement_data_checkpoints VALUES ('probe',6,'{}',?)", ("a" * 64,))
        with pytest.raises(Exception, match="ck_retirement_checkpoint_ordinal"):
            await connection.exec_driver_sql("INSERT INTO retirement_data_checkpoints VALUES ('probe',7,'{}',?)", ("a" * 64,))
    unchanged = await snapshot(old_journal)
    async with old_journal.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await ensure_retirement_data_journal(connection)
        await connection.commit()
    assert await snapshot(old_journal) == unchanged


@pytest.mark.asyncio
async def test_failure_after_drop_restores_original_schema_rows_and_triggers(old_journal, old_max, monkeypatch):
    before = await snapshot(old_journal)
    execute = AsyncConnection.exec_driver_sql
    async def fail(self, statement, *a, **kw):
        if statement.startswith("ALTER TABLE retirement_data_checkpoints_expanded"):
            raise RuntimeError("lost before rename")
        return await execute(self, statement, *a, **kw)
    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncConnection, "exec_driver_sql", fail)
        async with old_journal.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            with pytest.raises(RuntimeError, match="lost before rename"):
                await ensure_retirement_data_journal(connection)
            await connection.rollback()
    assert await snapshot(old_journal) == before
    async with old_journal.begin() as connection:
        with pytest.raises(Exception, match="ck_retirement_checkpoint_ordinal"):
            await connection.exec_driver_sql("INSERT INTO retirement_data_checkpoints VALUES ('probe',?,'{}',?)", (old_max + 1, "a" * 64))


@pytest.mark.asyncio
async def test_unclassified_trigger_blocks_upgrade_without_dropping_it(old_journal):
    async with old_journal.begin() as connection:
        await connection.exec_driver_sql("CREATE TRIGGER foreign_retirement_guard BEFORE INSERT ON retirement_data_checkpoints BEGIN SELECT 1; END")
    before = await snapshot(old_journal)
    async with old_journal.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="unclassified_dependency"):
            await ensure_retirement_data_journal(connection)
        await connection.rollback()
    assert await snapshot(old_journal) == before
