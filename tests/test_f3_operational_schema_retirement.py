"""Current metadata and native initialization cannot expose Sprint storage."""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from okto_pulse.community.adapters import sqlalchemy_models as live
from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema

def test_operational_metadata_has_no_sprint_mappers_or_relations():
    assert not any("sprint" in name.lower() for name in live.Base.metadata.tables)
    for name in ("Sprint", "SprintHistory", "SprintQAItem", "SprintActivationBaseline"):
        assert not hasattr(live, name)
    assert "sprint_id" not in live.Card.__table__.c
    assert not hasattr(live.Card, "sprint")
    assert not hasattr(live.Board, "sprints") and not hasattr(live.Spec, "sprints")
    assert all("sprint" not in fk.target_fullname.lower()
               for table in live.Base.metadata.tables.values() for fk in table.foreign_keys)
    assert "sprints" not in live.GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES

@pytest.mark.asyncio
async def test_native_creation_and_restart_have_no_sprint_objects(tmp_path):
    engine=create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'current.db'}")
    try:
        contract=current_schema_contract()
        await initialize_current_schema(engine,contract)
        await initialize_current_schema(engine,contract)
        async with engine.connect() as connection:
            objects=(await connection.exec_driver_sql("SELECT name FROM sqlite_schema")).scalars().all()
            assert not any("sprint" in name.lower() for name in objects)
            assert "sprint_id" not in [row[1] for row in await connection.exec_driver_sql("PRAGMA table_info(cards)")]
    finally:
        await engine.dispose()
