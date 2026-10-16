"""Physical-1 wire negatives, separate from native cold-open integration."""

import hashlib
import json

import pytest

from okto_pulse.community.adapters import native_graph_recovery_snapshot as native


def artifact(tmp_path, mutate):
    root = tmp_path / 'artifact'
    (root / 'objects').mkdir(parents=True)
    document = {'format': 'okto-grafx-physical-1', 'database_uuid': 'a' * 32,
        'page_size': 8192, 'partitions_per_table': 4, 'checkpoint_lsn': 12, 'files': []}
    for index, name in enumerate(sorted(native._REQUIRED_FILES)):
        obj = f'objects/{index:06d}'
        (root / obj).write_bytes(b'x')
        document['files'].append({'name': name, 'object': obj, 'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()})
    mutate(document)
    encoded = json.dumps(document).encode()
    (root / 'manifest.json').write_bytes(encoded)
    return native.NativeGraphRecoverySnapshot(root, hashlib.sha256(encoded).hexdigest())


@pytest.mark.parametrize('change', [
    lambda doc: doc['files'][0].update(size=True),
    lambda doc: doc['files'][0].update(size=1.0),
    lambda doc: doc['files'][0].update(object='../escape'),
    lambda doc: doc['files'][0].update(name='../escape'),
    lambda doc: doc['files'][0].update(name='unclassified.bin'),
    lambda doc: doc.update(checkpoint_lsn=True),
    lambda doc: doc['files'].pop(),
])
def test_authenticated_malformed_native_manifest_fails_before_restore(tmp_path, change):
    snapshot = artifact(tmp_path, change)
    with pytest.raises(ValueError, match='native_graph_snapshot_'):
        native.restore_native_graph_snapshot(snapshot, tmp_path / 'restored', confirm_original_offline=True)
    assert not (tmp_path / 'restored').exists()


def test_duplicate_native_manifest_key_is_not_silently_replaced(tmp_path):
    snapshot = artifact(tmp_path, lambda doc: None)
    path = snapshot.directory / 'manifest.json'
    encoded = path.read_bytes().replace(b'"checkpoint_lsn": 12', b'"checkpoint_lsn": 0, "checkpoint_lsn": 12')
    path.write_bytes(encoded)
    snapshot = native.NativeGraphRecoverySnapshot(snapshot.directory, hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ValueError, match='duplicate_key'):
        native.verify_native_graph_snapshot(snapshot)
