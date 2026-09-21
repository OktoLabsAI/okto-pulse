"""Fresh metadata and startup cannot revive the retired operational schema."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters import sqlalchemy_models as live
from okto_pulse.community.adapters.relational_schema_migrator import build_community_migration_ledger
from okto_pulse.community.adapters.retirement_runtime_admission import (
    require_retirement_not_started, require_retirement_runtime_admission,
)
from okto_pulse.core.ports import SchemaMigrationError
from test_card_context_retirement import dump
import legacy_sprint_schema as historical


def test_historical_fixture_isolated_from_operational_metadata_and_mappers():
    assert historical.Base.metadata is not live.Base.metadata
    assert set(historical.RETIRED_TABLES) <= set(historical.Base.metadata.tables)
    assert not set(historical.RETIRED_TABLES) & set(live.Base.metadata.tables)
    for name in ("Sprint", "SprintHistory", "SprintQAItem", "SprintActivationBaseline"):
        assert not hasattr(live, name)
    assert "sprint_id" in historical.Card.__table__.c
    assert "sprint_id" not in live.Card.__table__.c
    assert not hasattr(live.Card, "sprint")
    assert not hasattr(live.Board, "sprints") and not hasattr(live.Spec, "sprints")
    assert all(fk.target_fullname.split(".")[0] not in historical.RETIRED_TABLES
        for table in live.Base.metadata.tables.values() for fk in table.foreign_keys)


def test_retired_schema_writers_are_not_in_the_active_migration_plan():
    ids = {step.step_id for step in build_community_migration_ledger()}
    retired = {"_migrate_add_card_sprint_id", "_migrate_add_sprint_scope_fields", "_migrate_add_sprint_lane_fields"}
    assert not ids & retired
    assert "sprints" not in live.GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES
    assert live.GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION == "gdsr-trigger-manifest-v9"


@pytest.mark.asyncio
async def test_fresh_schema_steps_never_recreate_retired_tables_columns_or_indexes(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        await require_retirement_runtime_admission(engine)
        for _ in range(2):
            await steps.create_all_boundary()
            await steps._migrate_add_task_validation_columns()
            await steps._migrate_add_cancellation_columns()
            await steps._migrate_pagination_indices_and_positions()
            await steps._migrate_add_agent_seen_board_id()
        async with engine.connect() as connection:
            objects = (await connection.exec_driver_sql("SELECT name FROM sqlite_schema")).scalars().all()
            assert not any("sprint" in name.lower() for name in objects)
            assert "sprint_id" not in [row[1] for row in await connection.exec_driver_sql("PRAGMA table_info(cards)")]
        await require_retirement_runtime_admission(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["legacy", "origin_only"])
async def test_old_schema_is_an_offline_source_never_runtime_admission(tmp_path, shape):
    path = tmp_path / "source.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as connection:
            if shape == "legacy":
                await connection.run_sync(historical.Base.metadata.create_all)
            else:
                await connection.exec_driver_sql("CREATE TABLE cards(id TEXT, sprint_id TEXT)")
        before = dump(path)
        await require_retirement_not_started(engine)
        with pytest.raises(SchemaMigrationError, match="legacy_schema_requires_offline_cutover"):
            await require_retirement_runtime_admission(engine)
        assert dump(path) == before
    finally:
        await engine.dispose()
