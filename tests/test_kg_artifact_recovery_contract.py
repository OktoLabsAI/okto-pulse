"""Authenticated manifests still need a strict typed storage contract."""

import hashlib
import json

import pytest

from okto_pulse.community.adapters import kg_artifact_recovery as recovery
from test_kg_artifact_recovery import source, capture


@pytest.mark.parametrize('invalid_size', [True, 1.0])
def test_authenticated_manifest_rejects_non_integer_byte_count(tmp_path, invalid_size):
    root = source(tmp_path)
    relative = 'rebuild/generations/board/current.json'
    (root / relative).write_bytes(b'x')
    snapshot = capture(root, tmp_path)
    manifest = snapshot.directory / 'manifest.json'
    document = json.loads(manifest.read_bytes())
    entry = next(row for row in document['files'] if row['path'] == relative)
    assert entry['size'] == 1
    entry['size'] = invalid_size
    # A synthetically authenticated malformed document must fail its contract;
    # digest equality alone does not turn a boolean into a byte count.
    encoded = recovery._encode(document)
    manifest.write_bytes(encoded)
    malformed = recovery.KGArtifactRecoverySnapshot(snapshot.directory, hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ValueError, match='kg_artifact_recovery_manifest_invalid'):
        recovery.verify_kg_artifact_snapshot(malformed)
