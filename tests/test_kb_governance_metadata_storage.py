"""Native Knowledge Base storage on the complete current schema."""
import sqlite3
from contextlib import closing

import pytest
import pytest_asyncio
from sqlalchemy import JSON, inspect, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    StorageFormatError, current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory, install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Ideation, Refinement, Spec,
    IdeationKnowledgeBase, RefinementKnowledgeBase, SpecKnowledgeBase,
)
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from test_current_relational_schema import snapshot

KB_MODELS = (IdeationKnowledgeBase, RefinementKnowledgeBase, SpecKnowledgeBase)


@pytest_asyncio.fixture
async def native_kb_database(tmp_path):
    path = tmp_path / 'knowledge.db'
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    install_community_sqlite_pragmas(engine)
    await initialize_current_schema(engine, current_schema_contract())
    factory = build_community_session_factory(engine)
    try:
        async with factory() as session:
            session.add(Board(id='board', realm_id='local', name='Board', owner_id='author'))
            await session.flush()
            session.add(Ideation(id='idea', board_id='board', title='Idea', created_by='author'))
            await session.flush()
            session.add(Refinement(id='refinement', ideation_id='idea', board_id='board',
                title='Refinement', created_by='author'))
            session.add(Spec(id='spec', board_id='board', title='Spec', created_by='author',
                architecture_adoption=ArchitectureAdoptionScope(board_id='board', spec_id='spec',
                    adopted_in_edition=1, actor_id='author', inherited_resource_ids=()).model_dump(mode='json'),
                execution_contract=new_execution_contract(board_id='board', spec_id='spec',
                    edition=1, actor_id='author', origin='new_spec')))
            await session.flush()
            for model, parent in zip(KB_MODELS, ({'ideation_id': 'idea'},
                {'refinement_id': 'refinement'}, {'spec_id': 'spec'}), strict=True):
                session.add(model(id=model.__tablename__, title='Reference', content='Native content',
                    created_by='author', **parent))
            await session.commit()
        yield engine, factory, path
    finally:
        await engine.dispose()


def test_governance_metadata_orm_columns_are_nullable_json_without_defaults():
    for model in KB_MODELS:
        column = model.__table__.c.governance_metadata
        assert isinstance(column.type, JSON)
        assert column.nullable and column.default is None and column.server_default is None


@pytest.mark.asyncio
async def test_native_metadata_roundtrip_and_restart_preserve_exact_payload(native_kb_database):
    engine, factory, _ = native_kb_database
    payload = {'version': 1, 'classification': 'technical_reference', 'provenance': {'kind': 'authored'}}
    async with engine.connect() as connection:
        for model in KB_MODELS:
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns(model.__tablename__))
            column = next(item for item in columns if item['name'] == 'governance_metadata')
            assert isinstance(column['type'], JSON) and column['nullable'] and column['default'] is None
    async with factory() as session:
        for model in KB_MODELS:
            assert await session.scalar(select(model.governance_metadata)) is None
            await session.execute(update(model).values(governance_metadata=payload))
        await session.commit()
    await engine.dispose()
    await initialize_current_schema(engine, current_schema_contract())
    async with factory() as session:
        for model in KB_MODELS:
            assert await session.scalar(select(model.governance_metadata)) == payload
            assert await session.scalar(select(model.content)) == 'Native content'


@pytest.mark.asyncio
@pytest.mark.parametrize('definition', ['TEXT', "JSON NOT NULL DEFAULT '{}'", 'INTEGER'])
async def test_incompatible_metadata_storage_is_refused_without_writes(tmp_path, definition):
    path = tmp_path / 'incompatible.db'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f'CREATE TABLE ideation_knowledge_bases (id TEXT PRIMARY KEY, governance_metadata {definition})')
        connection.commit()
    before = snapshot(path)
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    try:
        with pytest.raises(StorageFormatError):
            await initialize_current_schema(engine, current_schema_contract())
    finally:
        await engine.dispose()
    assert snapshot(path) == before
