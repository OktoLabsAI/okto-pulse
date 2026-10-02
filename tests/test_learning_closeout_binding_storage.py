"""Current Learning binding storage starts unbound; incompatible files are refused."""
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import JSON, inspect
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    StorageFormatError, current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_models import Card
from test_current_relational_schema import snapshot
from test_kb_governance_metadata_storage import native_kb_database

__all__ = ['native_kb_database']


@pytest.mark.asyncio
async def test_native_card_has_no_invented_learning_binding_after_restart(native_kb_database):
    engine, factory, _ = native_kb_database
    column = Card.__table__.c.learning_closeout_bindings
    assert isinstance(column.type, JSON) and column.nullable
    assert column.default is None and column.server_default is None
    async with engine.connect() as connection:
        columns = await connection.run_sync(lambda sync: inspect(sync).get_columns('cards'))
        stored = next(item for item in columns if item['name'] == 'learning_closeout_bindings')
        assert isinstance(stored['type'], JSON) and stored['nullable'] and stored['default'] is None
    async with factory() as session:
        session.add(Card(id='bug', board_id='board', title='Native bug', card_type='bug', created_by='author'))
        await session.commit()
        assert (await session.get(Card, 'bug')).learning_closeout_bindings is None
    await engine.dispose()
    await initialize_current_schema(engine, current_schema_contract())
    async with factory() as session:
        card = await session.get(Card, 'bug')
        assert card.learning_closeout_bindings is None
        assert card.title == 'Native bug' and card.status.value == 'not_started'


@pytest.mark.asyncio
@pytest.mark.parametrize('definition', ['TEXT', 'JSON NOT NULL', "JSON DEFAULT '[]'"])
async def test_incompatible_binding_storage_is_refused_without_repair(tmp_path, definition):
    path = tmp_path / 'incompatible.db'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f'CREATE TABLE cards (id TEXT PRIMARY KEY, learning_closeout_bindings {definition})')
        connection.commit()
    before = snapshot(path)
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    try:
        with pytest.raises(StorageFormatError):
            await initialize_current_schema(engine, current_schema_contract())
    finally:
        await engine.dispose()
    assert snapshot(path) == before
