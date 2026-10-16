"""Raw authority files retain byte identity and their existing writer fence."""

import subprocess
import sys

import pytest

from okto_pulse.community.adapters import kg_artifact_recovery as recovery
from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore


def source(tmp_path):
    root = tmp_path / 'kg'
    root.mkdir()
    CommunityFileSystemRebuildAuditArtifactStore(root)
    for relative in ('rebuild/audit/cognitive_pending/board/generation.json',
            'rebuild/generations/board/current.json', 'contingency/original/contingency.json',
            'stress/original/evidence.json'):
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'{ "opaque" : "historical Sprint" }\r\n\x00')
    (root / 'rebuild' / 'empty').mkdir()
    return root


def capture(root, tmp_path):
    with recovery.kg_artifact_capture_window(root, selected_paths=('contingency', 'rebuild', 'stress')) as window:
        return window.capture(tmp_path / 'captured')


def test_exact_bytes_empty_directories_and_real_artifact_writer_exclusion(tmp_path):
    root = source(tmp_path)
    with recovery.kg_artifact_capture_window(root, selected_paths=('contingency', 'rebuild', 'stress')) as window:
        result = subprocess.run([sys.executable, '-c', '''
import sys
from filelock import Timeout
from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
store = CommunityFileSystemRebuildAuditArtifactStore(sys.argv[1])
store._file_lock.timeout = .01
try:
    store.write_json_atomic(RebuildAuditKey('run_audit', 'board', artifact_id='raced'), {'raced': True})
except Timeout:
    print('blocked')
else:
    print('wrote')
''', str(root)], check=True, capture_output=True, text=True, timeout=30)
        assert result.stdout.strip() == 'blocked'
        snapshot = window.capture(tmp_path / 'captured')
    document = recovery.verify_kg_artifact_snapshot(snapshot)
    assert len(document['files']) == 4
    recovery.restore_kg_artifact_snapshot(snapshot, tmp_path / 'restored')
    for entry in document['files']:
        assert (tmp_path / 'restored' / entry['path']).read_bytes() == (root / entry['path']).read_bytes()
    assert (tmp_path / 'restored' / 'rebuild' / 'empty').is_dir()
    assert not (tmp_path / 'restored' / recovery._MUTEX).exists()
    with pytest.raises(FileExistsError):
        recovery.restore_kg_artifact_snapshot(snapshot, tmp_path / 'restored')


@pytest.mark.parametrize('damage', ['blob', 'missing', 'extra', 'manifest'])
def test_damaged_snapshot_fails_before_restoration(tmp_path, damage):
    snapshot = capture(source(tmp_path), tmp_path)
    target = snapshot.directory / 'files/rebuild/generations/board/current.json'
    if damage == 'blob':
        target.write_bytes(b'corrupt')
    elif damage == 'missing':
        target.unlink()
    elif damage == 'extra':
        (target.parent / 'unexpected.json').write_bytes(b'{}')
    else:
        (snapshot.directory / 'manifest.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='kg_artifact_recovery_'):
        recovery.restore_kg_artifact_snapshot(snapshot, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_changed_source_refuses_capture_and_never_repairs_original(tmp_path, monkeypatch):
    root = source(tmp_path)
    copy = recovery._copy
    changed = root / 'rebuild/generations/board/current.json'
    def race(*args, **kwargs):
        digest = copy(*args, **kwargs)
        changed.write_bytes(b'changed by an unfenced writer')
        return digest
    monkeypatch.setattr(recovery, '_copy', race)
    with pytest.raises(ValueError, match='source_changed|size_changed'):
        capture(root, tmp_path)
    assert changed.read_bytes() == b'changed by an unfenced writer'


@pytest.mark.parametrize('path', ['unknown', 'rebuild/../boards', '/rebuild', 'rebuild\\audit'])
def test_unclassified_or_aliased_namespaces_are_not_claimed_as_protected(tmp_path, path):
    root = source(tmp_path)
    with pytest.raises(ValueError, match='unclassified_storage'):
        with recovery.kg_artifact_capture_window(root, selected_paths=(path,)):
            pytest.fail('invalid scope admitted')


def test_empty_selection_is_an_explicit_empty_certificate(tmp_path):
    root = tmp_path / 'kg'
    root.mkdir()
    with recovery.kg_artifact_capture_window(root, selected_paths=()) as window:
        snapshot = window.capture(tmp_path / 'captured')
    assert recovery.verify_kg_artifact_snapshot(snapshot)['files'] == []
    assert list(root.iterdir()) == []
