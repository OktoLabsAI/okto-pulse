"""Relational storage contract for canonical Knowledge Base fingerprints."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import String, inspect as sa_inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

import okto_pulse.community.adapters.relational_schema_steps as schema_steps
from okto_pulse.community.adapters.sqlalchemy_models import (
    IdeationKnowledgeBase,
    RefinementKnowledgeBase,
    SpecKnowledgeBase,
)


KB_MODELS = (
    IdeationKnowledgeBase,
    RefinementKnowledgeBase,
    SpecKnowledgeBase,
)


def test_content_hash_orm_columns_are_nullable_sha256_slots() -> None:
    for model in KB_MODELS:
        column = model.__table__.c.content_hash
        assert isinstance(column.type, String)
        assert column.type.length == 64
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None


async def _create_legacy_kb_tables(engine: Any) -> None:
    async with engine.begin() as connection:
        for model in KB_MODELS:
            table_name = model.__tablename__
            await connection.execute(
                text(
                    f'CREATE TABLE "{table_name}" '
                    "(id VARCHAR(36) PRIMARY KEY, content TEXT NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{table_name}" (id, content) '
                    "VALUES (:id, :content)"
                ),
                {"id": f"{table_name}-legacy", "content": "legacy"},
            )


@pytest.mark.asyncio
async def test_lineage_migration_adds_hash_preserves_legacy_rows_and_replays(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb-lineage-legacy.db'}"
    )
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
    await _create_legacy_kb_tables(engine)

    await schema_steps._migrate_add_kb_lineage_columns()
    await schema_steps._migrate_add_kb_lineage_columns()

    def inspect_columns(sync_connection: Any) -> dict[str, dict[str, Any]]:
        inspector = sa_inspect(sync_connection)
        return {
            model.__tablename__: {
                str(column["name"]): dict(column)
                for column in inspector.get_columns(model.__tablename__)
            }
            for model in KB_MODELS
        }

    async with engine.begin() as connection:
        observed = await connection.run_sync(inspect_columns)
        for model in KB_MODELS:
            table_name = model.__tablename__
            columns = observed[table_name]
            assert {
                "root_source_kb_id",
                "immediate_parent_kb_id",
                "content_hash",
            } <= set(columns)
            assert str(columns["content_hash"]["type"]).lower() == "varchar(64)"
            assert columns["content_hash"]["nullable"] is True
            assert columns["content_hash"]["default"] is None

            table = model.__table__
            legacy_hash = (
                await connection.execute(
                    select(table.c.content_hash).where(
                        table.c.id == f"{table_name}-legacy"
                    )
                )
            ).scalar_one()
            assert legacy_hash is None

        digest = "a" * 64
        await connection.execute(
            text(
                "UPDATE ideation_knowledge_bases "
                "SET content_hash=:digest WHERE id=:id"
            ),
            {"digest": digest, "id": "ideation_knowledge_bases-legacy"},
        )
        stored = (
            await connection.execute(
                select(IdeationKnowledgeBase.__table__.c.content_hash).where(
                    IdeationKnowledgeBase.__table__.c.id
                    == "ideation_knowledge_bases-legacy"
                )
            )
        ).scalar_one()
        assert stored == digest

    await engine.dispose()


@pytest.mark.asyncio
async def test_lineage_migration_rejects_a_preexisting_noncanonical_column(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb-lineage-malformed.db'}"
    )
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
    await _create_legacy_kb_tables(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE ideation_knowledge_bases "
                "ADD COLUMN content_hash INTEGER NOT NULL DEFAULT 0"
            )
        )

    with pytest.raises(RuntimeError, match="KB lineage column is non-canonical"):
        await schema_steps._migrate_add_kb_lineage_columns()

    async with engine.begin() as connection:
        observed = await connection.run_sync(
            lambda sync_connection: {
                str(column["name"])
                for column in sa_inspect(sync_connection).get_columns(
                    "refinement_knowledge_bases"
                )
            }
        )
    assert "content_hash" not in observed
    await engine.dispose()
