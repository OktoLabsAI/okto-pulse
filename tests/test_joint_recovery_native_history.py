"""Operator rollback must retain history exposed by the original runtime."""

import asyncio
import hashlib

import pytest
from okto_grafx import connect

from okto_pulse.community.adapters.grafx_observations import CommunityGrafxHistory
from okto_pulse.community.adapters import joint_recovery_snapshot as joint
import test_joint_recovery_snapshot as recovery

sources = recovery.sources
stored_sources = recovery.stored_sources


def capture(stored_sources):
    original, uploads, _, _ = stored_sources
    return recovery.capture(original, kg_base_dir=original[3] / 'kg', storage_root=uploads, include_native=True)


def history(database):
    return CommunityGrafxHistory(lambda _: database, lambda *args: None)


def test_rollback_preserves_native_commit_history_and_original_cursors(stored_sources, tmp_path):
    original, uploads, _, _ = stored_sources
    database = original[1][0].database
    reader = history(database)
    reader.activate('board-one', ('Decision',), (), reason='disposable rollback history fixture')
    with database.begin('write') as writer:
        writer.execute("MATCH (n:Decision {id: 'baseline'}) SET n.title = 'retained history'")
    expected = reader.commits('board-one')
    assert expected['entries']
    cursor = expected['entries'][-1]['commit']
    picture = reader.as_of('board-one', cursor, ('Decision',), ())
    with database.begin('write') as writer:
        writer.execute("MATCH (n:Decision {id: 'baseline'}) SET n.title = 'latest title'")
    expected = reader.commits('board-one')
    snapshot = capture(stored_sources)
    assert reader.commits('board-one') == expected  # Checkpoint adds no domain/history event.
    for graph in original[1]:
        graph.database.close()
    restored = joint.restore_joint_recovery_snapshot(snapshot, tmp_path / 'restored', builds=recovery.BUILDS,
        current_storage_root=uploads, confirm_original_offline=True, max_seconds=120)
    with connect(restored / 'graph-0000', page_size=8192, read_only=True) as cold:
        assert history(cold).commits('board-one') == expected
        assert history(cold).as_of('board-one', cursor, ('Decision',), ()) == picture


def test_native_restore_requires_explicit_offline_confirmation_before_sql(stored_sources, tmp_path, monkeypatch):
    snapshot = capture(stored_sources)
    monkeypatch.setattr(joint, 'restore_sqlite_recovery_snapshot', lambda *args, **kwargs: pytest.fail('SQL created before offline check'))
    for flag in (False, 1, 'yes'):
        with pytest.raises(ValueError, match='original_offline_required'):
            joint.restore_joint_recovery_snapshot(snapshot, tmp_path / 'restored', builds=recovery.BUILDS,
                current_storage_root=stored_sources[1], confirm_original_offline=flag)
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('damage', ['object', 'extra', 'erasure'])
def test_native_backup_damage_or_later_erasure_refuses_before_sql(stored_sources, tmp_path, monkeypatch, damage):
    snapshot = capture(stored_sources)
    for graph in stored_sources[0][1]:
        graph.database.close()
    if damage == 'object':
        (snapshot.directory / 'native-0000/objects/000000').write_bytes(b'damaged')
    elif damage == 'extra':
        (snapshot.directory / 'native-0000/objects/999999').write_bytes(b'extra')
    else:
        asyncio.run(stored_sources[2].purge_board('board-one'))
    monkeypatch.setattr(joint, 'restore_sqlite_recovery_snapshot', lambda *a, **k: pytest.fail('SQL created before refusal'))
    with pytest.raises(ValueError, match='native_graph_snapshot_|newer_erasure_refused'):
        joint.restore_joint_recovery_snapshot(snapshot, tmp_path / 'restored', builds=recovery.BUILDS,
            current_storage_root=stored_sources[1], confirm_original_offline=True)
    assert not (tmp_path / 'restored').exists()


def test_native_capture_cannot_hide_a_commit_after_its_checkpoint(stored_sources, monkeypatch):
    original = joint.capture_native_graph_snapshot
    def race(database, *args, **kwargs):
        snapshot = original(database, *args, **kwargs)
        if database is stored_sources[0][1][0].database:
            with database.begin('write') as writer:
                writer.execute("MATCH (n:Decision {id: 'baseline'}) SET n.title = 'concurrent'")
        return snapshot
    monkeypatch.setattr(joint, 'capture_native_graph_snapshot', race)
    with pytest.raises(ValueError, match='graph_changed_during_capture'):
        capture(stored_sources)
    assert not (stored_sources[0][2] / 'capture').exists()


def test_privacy_fence_spans_native_restore_and_final_publication(stored_sources, tmp_path, monkeypatch):
    snapshot = capture(stored_sources)
    for graph in stored_sources[0][1]:
        graph.database.close()
    original = joint.restore_native_graph_snapshot
    def race(*args, **kwargs):
        report = original(*args, **kwargs)
        digest = hashlib.sha256(b'external-erasure').hexdigest()
        (stored_sources[1] / '.board_lifecycle' / f'{digest}.erased').write_bytes(b'erased\n')
        return report
    monkeypatch.setattr(joint, 'restore_native_graph_snapshot', race)
    with pytest.raises(ValueError, match='privacy_state_changed'):
        joint.restore_joint_recovery_snapshot(snapshot, tmp_path / 'restored', builds=recovery.BUILDS,
            current_storage_root=stored_sources[1], confirm_original_offline=True, max_seconds=120)
    assert not (tmp_path / 'restored').exists()
    assert not list(tmp_path.glob('*.restore'))
