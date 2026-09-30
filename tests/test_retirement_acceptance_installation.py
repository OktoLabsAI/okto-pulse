"""BASE T25/T28: fresh bootstrap and retired-only effective authority."""
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.permission_retirement_checkpoint import capture_permission_retirement_checkpoint
from okto_pulse.community.adapters.permission_retirement_cleanup import retire_permission_documents
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from okto_pulse.community.adapters.sqlalchemy_models import Agent, PermissionPreset
from okto_pulse.core.ports.permission_policy import flatten_permission_flags, registered_permission_flags, set_permission_flag
from test_permission_retirement_review_installation import RETIRED
from test_sprint_retirement_access import add_agent
from test_sprint_retirement_inventory import database as _database

database = _database


def test_fresh_full_bootstrap_and_second_start_have_no_operational_sprint(tmp_path):
    path = tmp_path / 'fresh.sqlite3'
    environment = {**os.environ, 'DATA_DIR': str(tmp_path), 'KG_BASE_DIR': str(tmp_path / 'kg'),
                   'DATABASE_URL': f'sqlite+aiosqlite:///{path}', 'KG_EMBEDDING_MODE': 'stub'}
    script = '''
import asyncio, os
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle
async def main():
    db.configure_community_database(os.environ['DATABASE_URL'])
    register_community_relational_schema_lifecycle()
    try:
        await db.init_db()
    finally:
        await db.close_db()
asyncio.run(main())
'''
    previous = None
    for _ in range(2):
        result = subprocess.run([sys.executable, '-c', script], env=environment, cwd=tmp_path,
                                capture_output=True, text=True, timeout=150)
        assert result.returncode == 0, result.stdout + result.stderr
        with sqlite3.connect(path) as db:
            names = [row[0] for row in db.execute('SELECT name FROM sqlite_schema')]
            assert not any('sprint' in name.lower() for name in names)
            assert 'sprint_id' not in [row[1] for row in db.execute('PRAGMA table_info(cards)')]
            presets = db.execute('SELECT id, flags FROM permission_presets ORDER BY id').fetchall()
            assert presets
            for _, flags in presets:
                paths = flatten_permission_flags(json.loads(flags))
                assert not set(paths) & set(RETIRED)
            if previous is not None:
                assert presets == previous
            previous = presets


@pytest.mark.asyncio
@pytest.mark.parametrize('layer', ['direct', 'preset'])
async def test_retired_only_agent_keeps_identity_without_new_operational_authority(database, layer):
    engine, _ = database
    flags = registered_permission_flags()
    active = flatten_permission_flags(flags)
    for flag in active:
        set_permission_flag(flags, flag, False)
    for flag in RETIRED:
        set_permission_flag(flags, flag, True)
    async with engine.begin() as connection:
        if layer == 'preset':
            await connection.execute(insert(PermissionPreset).values(id='retired', name='Retired only', flags=flags))
        await add_agent(connection, 'retired-only', flags=flags if layer == 'direct' else None,
                        preset='retired' if layer == 'preset' else None)
        before = dict((await connection.execute(select(Agent.__table__).where(Agent.id == 'retired-only'))).mappings().one())
    checkpoint = await capture_permission_retirement_checkpoint(engine, migration_id='retired-only')
    await retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    async with engine.connect() as connection:
        after = dict((await connection.execute(select(Agent.__table__).where(Agent.id == 'retired-only'))).mappings().one())
    changing = {'permission_flags', 'permission_migration_review'}
    assert {key: value for key, value in before.items() if key not in changing} == {
        key: value for key, value in after.items() if key not in changing}
    async with AsyncSession(engine) as session:
        context = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context('retired-only', board_id='board-a')
        assert context is not None
        assert not any(context.permissions.has(flag) for flag in active)
