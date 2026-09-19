"""Additive upgrade preserves legacy authority and fails closed on schema drift."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters.relational_schema_migrator import build_community_migration_ledger


@pytest.mark.asyncio
async def test_upgrade_preserves_legacy_rows_and_is_idempotent(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.sqlite'}")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE specs (id TEXT PRIMARY KEY, status TEXT, version INTEGER)"))
            await conn.execute(text("INSERT INTO specs VALUES ('a', 'draft', 3), ('b', 'in_progress', 7), ('c', 'done', 9)"))
        await steps._migrate_add_spec_architecture_adoption()
        await steps._migrate_add_spec_architecture_adoption()
        async with engine.connect() as conn:
            assert list((await conn.execute(text("SELECT id,status,version,architecture_adoption FROM specs ORDER BY id"))).tuples()) == [
                ("a", "draft", 3, None), ("b", "in_progress", 7, None), ("c", "done", 9, None),
            ]
        step = next(item for item in build_community_migration_ledger() if item.step_id == "_migrate_add_spec_architecture_adoption")
        assert step.phase == "pre_create_all" and step.idempotent and not step.destructive
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_does_not_hide_incompatible_existing_column(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drift.sqlite'}")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE specs (id TEXT PRIMARY KEY, architecture_adoption INTEGER NOT NULL)"))
        with pytest.raises(RuntimeError, match="spec_architecture_adoption_schema_drift"):
            await steps._migrate_add_spec_architecture_adoption()
    finally:
        await engine.dispose()
