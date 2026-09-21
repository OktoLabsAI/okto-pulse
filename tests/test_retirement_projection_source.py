"""Post-bootstrap source authority consumed by the final graph projection."""

import sqlite3

import pytest

from okto_pulse.community.adapters.board_source_reader import (
    CommunityBoardSourceReader, read_realm_cognitive_source_snapshot, read_realm_source_snapshot,
)
from okto_pulse.community.adapters.global_discovery_recovery import CommunityRelationalRecoverySnapshotFingerprint
from okto_pulse.community.adapters.sqlalchemy_models import GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
from test_retirement_offline_bootstrap import prepare, resume
from test_retirement_v034_cards import dump


@pytest.mark.asyncio
async def test_real_predecessor_bootstrap_exposes_coherent_final_source_and_fence(tmp_path):
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        await resume(runtime, storage, run)
        before = dump(path)
        with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            fence = CommunityRelationalRecoverySnapshotFingerprint.read_fence_from_connection(connection)
            boards, sources = read_realm_source_snapshot(connection, realm_id='local')
            cognitive = read_realm_cognitive_source_snapshot(connection, realm_id='local')
            assert fence.trigger_manifest_version == GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
            assert {row['board_id'] for row in boards} == {'board-a'}
            assert {row['id'] for row in sources['board-a']} == {'spec-a', 'card-a', 'card-b'}
            assert all(row['artifact_type'] != 'sprint' for row in sources['board-a'])
            assert cognitive == {'board-a': ()}
            assert CommunityRelationalRecoverySnapshotFingerprint.read_fence_from_connection(connection) == fence
        per_board = CommunityBoardSourceReader(db_path=path).fetch('board-a')
        assert per_board.complete
        assert sorted(per_board.rows, key=lambda row: row['source_ref']) == sorted(sources['board-a'], key=lambda row: row['source_ref'])
        assert dump(path) == before
    finally:
        await runtime.close()
