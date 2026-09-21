"""Joint rollback retains the filesystem authority used by cognitive replay."""

import hashlib
import json

import pytest

from okto_pulse.community.adapters import joint_recovery_snapshot as joint
from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
import test_joint_recovery_snapshot as recovery

sources = recovery.sources
stored_sources = recovery.stored_sources


def test_joint_roundtrip_preserves_cognitive_pending_and_generation_evidence(stored_sources, tmp_path):
    original, uploads, _, _ = stored_sources
    kg = original[3] / 'kg'
    store = CommunityFileSystemRebuildAuditArtifactStore(kg)
    key = RebuildAuditKey('cognitive_pending', 'board-one', kg_generation_id='generation-original')
    # Deliberately opaque historical payload: recovery copies original bytes;
    # it must not reinterpret this as a current claim or approval.
    store.write_json_atomic(key, {'kg_generation_id': 'generation-original', 'historical': 'sprint:opaque'})
    retained = kg / 'rebuild' / 'audit' / 'cognitive_pending' / 'board-one' / 'generation-original.json'
    before = retained.read_bytes()
    snapshot = recovery.capture_stored(stored_sources)
    manifest = joint.verify_joint_recovery_snapshot(snapshot)
    assert 'rebuild' in manifest['routing_inventory']['other_storage_paths']
    restored = joint.restore_joint_recovery_snapshot(snapshot, tmp_path / 'restored', builds=recovery.BUILDS,
        current_storage_root=uploads, max_seconds=120)
    result = restored / 'kg-artifacts' / retained.relative_to(kg)
    assert result.is_file(), 'joint recovery omitted retained KG authority'
    assert result.read_bytes() == before
    assert retained.read_bytes() == before
    assert not (restored / 'kg-artifacts' / 'rebuild' / '.rebuild-audit-artifact-store.lock').exists()


@pytest.mark.parametrize('has_artifacts', [False, True])
def test_legacy_v4_reader_does_not_claim_omitted_kg_authority(stored_sources, tmp_path, has_artifacts):
    from okto_pulse.community.adapters.retirement_offline_run import _complete_backup
    original, uploads, _, _ = stored_sources
    if has_artifacts:
        store = CommunityFileSystemRebuildAuditArtifactStore(original[3] / 'kg')
        store.write_json_atomic(RebuildAuditKey('run_audit', 'board-one', artifact_id='original'), {'historical': True})
    snapshot = recovery.capture_stored(stored_sources)
    # Synthetic v4 wire-format fixture derived from the authenticated common
    # fields. No SQL, graph, upload or historical payload is rewritten.
    path = snapshot.directory / 'manifest.json'
    document = json.loads(path.read_bytes())
    document.pop('kg_artifacts')
    document['format'] = 'joint-recovery-snapshot/v4'
    encoded = joint._encode(document)
    path.write_bytes(encoded)
    legacy = joint.JointRecoverySnapshot(snapshot.directory, hashlib.sha256(encoded).hexdigest())
    verified = joint.verify_joint_recovery_snapshot(legacy)
    assert _complete_backup(verified) is (not has_artifacts)
    restored = joint.restore_joint_recovery_snapshot(legacy, tmp_path / 'legacy-restored', builds=recovery.BUILDS,
        current_storage_root=uploads, max_seconds=120)
    assert (restored / 'database.sqlite3').is_file()
    assert not (restored / 'kg-artifacts').exists()


@pytest.mark.parametrize('relative,reason', [
    ('unknown', 'unclassified_storage'),
])
def test_incomplete_kg_coverage_refuses_publication_without_touching_source(stored_sources, relative, reason):
    original, _, _, _ = stored_sources
    retained = original[3] / 'kg' / relative / 'opaque-history.bin'
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b'original retained authority')
    with pytest.raises(ValueError, match=reason):
        recovery.capture_stored(stored_sources)
    assert retained.read_bytes() == b'original retained authority'
    assert not (original[2] / 'capture').exists()
    assert not list(original[2].glob('*.partial'))
