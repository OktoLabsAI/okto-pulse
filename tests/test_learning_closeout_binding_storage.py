"""Additive upgrade never invents Learning proof for historical Done cards."""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.relational_schema_steps import _ensure_learning_closeout_bindings


async def test_schema_upgrade_is_idempotent_preserves_history_and_starts_unbound(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "old.db"}')
    try:
        async with engine.begin() as connection:
            await connection.execute(text('CREATE TABLE cards (id TEXT PRIMARY KEY, status TEXT, conclusions JSON)'))
            await connection.execute(text("INSERT INTO cards VALUES ('old', 'done', '[{\"text\":\"Historic report\"}]')"))
            assert await _ensure_learning_closeout_bindings(connection, create=False) == 'missing'
            await _ensure_learning_closeout_bindings(connection)
            assert await _ensure_learning_closeout_bindings(connection) == 'skipped'
            row = (await connection.execute(text('SELECT * FROM cards'))).mappings().one()
            assert dict(row) == dict(id='old', status='done', conclusions='[{"text":"Historic report"}]',
                learning_closeout_bindings=None)
    finally:
        await engine.dispose()


@pytest.mark.parametrize('definition', ["TEXT", "JSON NOT NULL", "JSON DEFAULT '[]'"])
async def test_existing_incompatible_storage_is_not_silently_repaired(tmp_path, definition):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "drift.db"}')
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE TABLE cards (id TEXT PRIMARY KEY, learning_closeout_bindings {definition})'))
            with pytest.raises(RuntimeError, match='binding_schema_drift'):
                await _ensure_learning_closeout_bindings(connection)
    finally:
        await engine.dispose()


async def test_model_and_migrator_declare_the_same_additive_contract(tmp_path):
    from okto_pulse.community.adapters.sqlalchemy_models import Board, Card
    from test_bug_cognitive_context_adapter import _runtime

    engine, factory = await _runtime(tmp_path / 'model.db')
    try:
        async with engine.begin() as connection:
            assert await _ensure_learning_closeout_bindings(connection, create=False) == 'skipped'
        async with factory() as session:
            session.add(Board(id='board', name='Board', owner_id='owner'))
            session.add(Card(id='bug', board_id='board', title='Bug', card_type='bug', created_by='owner'))
            await session.commit()
            card = await session.get(Card, 'bug')
            assert card.learning_closeout_bindings is None
    finally:
        await engine.dispose()
