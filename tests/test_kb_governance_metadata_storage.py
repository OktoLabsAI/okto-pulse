"""Community persistence contract for knowledge-governance metadata."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import JSON, bindparam, event, inspect as sa_inspect, select, text
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


def test_governance_metadata_orm_columns_are_nullable_json_without_defaults() -> None:
    for model in KB_MODELS:
        column = model.__table__.c.governance_metadata
        assert isinstance(column.type, JSON)
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None


async def _create_legacy_kb_tables(engine, *, malformed_table: str | None = None) -> None:
    async with engine.begin() as connection:
        for model in KB_MODELS:
            table_name = model.__tablename__
            extra = (
                ", governance_metadata TEXT NOT NULL DEFAULT '{}'"
                if table_name == malformed_table
                else ""
            )
            await connection.execute(
                text(
                    f'CREATE TABLE "{table_name}" '
                    f'(id VARCHAR(36) PRIMARY KEY, content TEXT NOT NULL{extra})'
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{table_name}" (id, content) '
                    "VALUES (:id, :content)"
                ),
                {"id": f"{table_name}-1", "content": "legacy"},
            )


async def _inspect_governance_columns(engine) -> dict[str, dict[str, Any] | None]:
    def _inspect(sync_connection):
        inspector = sa_inspect(sync_connection)
        return {
            model.__tablename__: next(
                (
                    dict(column)
                    for column in inspector.get_columns(model.__tablename__)
                    if column["name"] == "governance_metadata"
                ),
                None,
            )
            for model in KB_MODELS
        }

    async with engine.connect() as connection:
        return await connection.run_sync(_inspect)


@pytest.mark.asyncio
async def test_migration_adds_columns_preserves_legacy_rows_and_replays(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb-governance-legacy.db'}"
    )
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
    await _create_legacy_kb_tables(engine)

    first = await schema_steps._migrate_add_kb_governance_metadata()
    second = await schema_steps._migrate_add_kb_governance_metadata()
    observed = await _inspect_governance_columns(engine)

    assert first is None
    assert second == "skipped"
    for column in observed.values():
        assert column is not None
        assert str(column["type"]).lower() == "json"
        assert column["nullable"] is True
        assert column["default"] is None

    payload = {
        "version": 1,
        "classification": "technical_reference",
        "provenance": {"kind": "authored"},
    }
    table = IdeationKnowledgeBase.__table__
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE ideation_knowledge_bases "
                "SET governance_metadata=:governance_metadata WHERE id=:id"
            ).bindparams(bindparam("governance_metadata", type_=JSON)),
            {
                "governance_metadata": payload,
                "id": "ideation_knowledge_bases-1",
            },
        )
        stored = (
            await connection.execute(
                select(table.c.governance_metadata).where(
                    table.c.id == "ideation_knowledge_bases-1"
                )
            )
        ).scalar_one()
        legacy_nulls = {}
        for model in KB_MODELS[1:]:
            model_table = model.__table__
            legacy_nulls[model.__tablename__] = (
                await connection.execute(
                    select(model_table.c.governance_metadata).where(
                        model_table.c.id == f"{model.__tablename__}-1"
                    )
                )
            ).scalar_one()

    assert stored == payload
    assert set(legacy_nulls.values()) == {None}
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_fails_closed_before_mutating_other_tables(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb-governance-malformed.db'}"
    )
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
    await _create_legacy_kb_tables(
        engine,
        malformed_table="ideation_knowledge_bases",
    )

    with pytest.raises(RuntimeError, match="non-canonical"):
        await schema_steps._migrate_add_kb_governance_metadata()

    observed = await _inspect_governance_columns(engine)
    assert observed["ideation_knowledge_bases"] is not None
    assert observed["refinement_knowledge_bases"] is None
    assert observed["spec_knowledge_bases"] is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_rolls_back_every_column_when_second_alter_fails(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb-governance-mid-ddl.db'}"
    )
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
    await _create_legacy_kb_tables(engine)
    alter_count = 0

    def fail_second_alter(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal alter_count
        if statement.lstrip().upper().startswith("ALTER TABLE"):
            alter_count += 1
            if alter_count == 2:
                raise RuntimeError("injected second ALTER failure")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_second_alter)
    try:
        with pytest.raises(RuntimeError, match="injected second ALTER failure"):
            await schema_steps._migrate_add_kb_governance_metadata()
    finally:
        event.remove(
            engine.sync_engine, "before_cursor_execute", fail_second_alter
        )

    assert alter_count == 2
    observed = await _inspect_governance_columns(engine)
    assert set(observed.values()) == {None}
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_rolls_back_when_postcondition_audit_detects_drift(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb-governance-postcondition.db'}"
    )
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
    await _create_legacy_kb_tables(engine)
    monkeypatch.setattr(
        schema_steps,
        "_normalize_sqlite_contract_type",
        lambda _raw: "injected_non_canonical_type",
    )

    with pytest.raises(RuntimeError, match="non-canonical"):
        await schema_steps._migrate_add_kb_governance_metadata()

    observed = await _inspect_governance_columns(engine)
    assert set(observed.values()) == {None}
    await engine.dispose()
