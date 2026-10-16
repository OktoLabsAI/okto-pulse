"""Native Knowledge Base lineage storage; no upgrade or backfill."""
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import String, inspect, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    StorageFormatError, current_schema_contract, initialize_current_schema,
)
from test_current_relational_schema import snapshot
from test_kb_governance_metadata_storage import KB_MODELS, native_kb_database

__all__ = ['native_kb_database']


def test_content_hash_orm_columns_are_nullable_sha256_slots():
    for model in KB_MODELS:
        column = model.__table__.c.content_hash
        assert isinstance(column.type, String) and column.type.length == 64
        assert column.nullable and column.default is None and column.server_default is None


@pytest.mark.asyncio
async def test_native_lineage_persists_authored_fingerprint_and_relationships(native_kb_database):
    engine, factory, _ = native_kb_database
    async with engine.connect() as connection:
        for model in KB_MODELS:
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns(model.__tablename__))
            observed = {item['name']: item for item in columns}
            assert {'root_source_kb_id', 'immediate_parent_kb_id', 'content_hash'} <= observed.keys()
            assert str(observed['content_hash']['type']).lower() == 'varchar(64)'
            assert observed['content_hash']['nullable'] and observed['content_hash']['default'] is None
    async with factory() as session:
        for index, model in enumerate(KB_MODELS):
            item = await session.get(model, model.__tablename__)
            assert item.content_hash is None and item.root_source_kb_id is None and item.immediate_parent_kb_id is None
            await session.execute(update(model).values(content_hash=str(index + 1) * 64,
                root_source_kb_id=KB_MODELS[0].__tablename__,
                immediate_parent_kb_id=KB_MODELS[max(0, index - 1)].__tablename__))
        await session.commit()
    await engine.dispose()
    await initialize_current_schema(engine, current_schema_contract())
    async with factory() as session:
        for index, model in enumerate(KB_MODELS):
            item = (await session.scalars(select(model))).one()
            assert item.content_hash == str(index + 1) * 64
            assert item.root_source_kb_id == KB_MODELS[0].__tablename__
            assert item.immediate_parent_kb_id == KB_MODELS[max(0, index - 1)].__tablename__
            assert item.content == 'Native content'


@pytest.mark.asyncio
async def test_incompatible_lineage_storage_is_refused_without_conversion(tmp_path):
    path = tmp_path / 'incompatible.db'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('CREATE TABLE ideation_knowledge_bases (id TEXT PRIMARY KEY, content_hash INTEGER NOT NULL DEFAULT 0)')
        connection.execute("INSERT INTO ideation_knowledge_bases (id) VALUES ('preserve')")
        connection.commit()
    before = snapshot(path)
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    try:
        with pytest.raises(StorageFormatError):
            await initialize_current_schema(engine, current_schema_contract())
    finally:
        await engine.dispose()
    assert snapshot(path) == before
