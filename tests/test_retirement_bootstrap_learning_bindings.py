"""The additive empty Learning binding column preserves frozen Card content."""
import sqlite3

import pytest

from okto_pulse.community.adapters import retirement_bootstrap as bootstrap
from okto_pulse.community.adapters import retirement_offline_run as offline
from test_retirement_offline_bootstrap import prepare, resume
from test_retirement_offline_run import MIGRATION
from test_retirement_v034_cards import dump


@pytest.mark.asyncio
async def test_frozen_v034_adds_only_empty_learning_bindings_during_bootstrap(tmp_path, monkeypatch):
    runtime, storage, run, path = await prepare(tmp_path)
    reads = []
    original = bootstrap._load_cards
    async def observe(*args, **kwargs):
        rows = await original(*args, **kwargs)
        reads.append(rows)
        return rows
    monkeypatch.setattr(bootstrap, '_load_cards', observe)
    try:
        with sqlite3.connect(path) as sql:
            assert 'learning_closeout_bindings' not in {row[1] for row in sql.execute('PRAGMA table_info(cards)')}
        try:
            result = await resume(runtime, storage, run)
        finally:
            # Characterize the exact old/new cells even on the original failure.
            assert len(reads) == 2
            before = {row['id']: {key: value for key, value in row.items() if key != 'position'} for row in reads[0]}
            after = {row['id']: {key: value for key, value in row.items() if key != 'position'} for row in reads[1]}
            assert after == {key: {**row, 'learning_closeout_bindings': None} for key, row in before.items()}
        assert result['state'] == 'bootstrap_complete'
        monkeypatch.setattr(bootstrap, '_load_cards', original)
        committed = dump(path)
        assert await resume(runtime, storage, run) == result
        assert dump(path) == committed
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['fabricated-binding', 'unexpected-column'])
async def test_only_declared_null_addition_is_allowed_and_late_failure_rolls_back(tmp_path, monkeypatch, damage):
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        await offline.resume_offline_retirement_schema(runtime, storage, (), run, migration_builds=MIGRATION)
        before = dump(path)
        from okto_pulse.community.adapters.relational_schema_lifecycle import CommunityRelationalSchemaLifecycleOrchestrator
        original = CommunityRelationalSchemaLifecycleOrchestrator.initialize_schema
        async def changed(self):
            result = await original(self)
            from okto_pulse.community.adapters.sqlalchemy_database import get_engine
            async with get_engine().begin() as connection:
                await connection.exec_driver_sql(
                    "UPDATE cards SET learning_closeout_bindings='[]' WHERE id='card-a'"
                    if damage == 'fabricated-binding' else 'ALTER TABLE cards ADD COLUMN unplanned_binding JSON')
            return result
        with monkeypatch.context() as scoped:
            scoped.setattr(CommunityRelationalSchemaLifecycleOrchestrator, 'initialize_schema', changed)
            with pytest.raises(ValueError, match='retirement_bootstrap_card_content_changed'):
                await resume(runtime, storage, run)
        assert dump(path) == before
    finally:
        await runtime.close()
