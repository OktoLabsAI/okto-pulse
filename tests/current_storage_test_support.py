"""Disposable native storage shared by current-format recovery tests."""

import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card


@pytest_asyncio.fixture
async def database(tmp_path):
    path = tmp_path / "current.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        await initialize_current_schema(engine, current_schema_contract())
        async with engine.begin() as connection:
            await connection.execute(insert(Board), [
                {"id": board, "name": board, "owner_id": "owner"}
                for board in ("board-a", "board-b")
            ])
        yield engine, path
    finally:
        await engine.dispose()


async def add_card(engine):
    async with engine.begin() as connection:
        await connection.execute(insert(Card).values(
            id="card", board_id="board-a", title="Current work", created_by="owner",
        ))
