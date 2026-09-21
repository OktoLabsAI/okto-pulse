"""Inactive graph custody never opens, repairs, or promotes the original."""

import hashlib
import json

import pytest

from logical_transfer_matrix_support import one_node_corpus, seed_generation
from okto_pulse.community.adapters import joint_recovery_snapshot as joint
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
import test_joint_recovery_snapshot as recovery

sources = recovery.sources
stored_sources = recovery.stored_sources


def tree(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('scope', ['board', 'global_discovery'])
def test_joint_capture_retains_inactive_graph_and_quarantine_as_opaque_bytes(stored_sources, tmp_path, scope):
    original, uploads, _, _ = stored_sources
    kg = original[3] / 'kg'
    bindings = CommunityGraphBackendBindingStore(kg)
    inactive = (bindings.board_grafx_path('board-one', 'retained') if scope == 'board'
        else bindings.global_grafx_path('retained'))
    seed_generation('grafx', inactive, one_node_corpus(scope))
    quarantine = kg / 'quarantine' / 'retained-evidence'
    quarantine.mkdir(parents=True)
    (quarantine / 'original.bin').write_bytes(b'opaque damaged history\x00')
    before = tree(inactive)
    binding = (bindings.inspect_board_binding('board-one'), bindings.inspect_global_binding())
    snapshot = recovery.capture_stored(stored_sources)
    manifest = joint.verify_joint_recovery_snapshot(snapshot)
    assert manifest['format'] == 'joint-recovery-snapshot/v6'
    assert manifest['routing_inventory']['unselected_generation_paths'] == [inactive.relative_to(kg).as_posix()]
    restored = joint.restore_joint_recovery_snapshot(snapshot, tmp_path / 'restored', builds=recovery.BUILDS,
        current_storage_root=uploads, max_seconds=120)
    assert tree(restored / 'kg-artifacts' / inactive.relative_to(kg)) == before
    assert (restored / 'kg-artifacts/quarantine/retained-evidence/original.bin').read_bytes() == b'opaque damaged history\x00'
    assert tree(inactive) == before
    assert (bindings.inspect_board_binding('board-one'), bindings.inspect_global_binding()) == binding
    assert not (restored / 'kg-artifacts/boards/board-one/graph_backend_binding.json').exists()


@pytest.mark.parametrize('has_artifact', [False, True])
def test_retained_v5_snapshot_still_verifies_and_restores(stored_sources, tmp_path, has_artifact):
    if has_artifact:
        from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
        from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
        store = CommunityFileSystemRebuildAuditArtifactStore(stored_sources[0][3] / 'kg')
        store.write_json_atomic(RebuildAuditKey('run_audit', 'board-one', artifact_id='original'), {'original': True})
    snapshot = recovery.capture_stored(stored_sources)
    # Synthetic old wire format; all physical SQL, graph and upload bytes stay
    # unchanged. The nested custody manifest used v1 in v5 joint sets.
    nested_path = snapshot.directory / 'kg-artifacts/manifest.json'
    nested = json.loads(nested_path.read_bytes())
    assert nested['roots'] == (['rebuild'] if has_artifact else [])
    nested['format'] = 'kg-artifact-recovery/v1'
    encoded = joint._encode(nested)
    nested_path.write_bytes(encoded)
    manifest_path = snapshot.directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['format'] = 'joint-recovery-snapshot/v5'
    manifest['kg_artifacts']['manifest_sha256'] = hashlib.sha256(encoded).hexdigest()
    encoded = joint._encode(manifest)
    manifest_path.write_bytes(encoded)
    legacy = joint.JointRecoverySnapshot(snapshot.directory, hashlib.sha256(encoded).hexdigest())
    assert joint.verify_joint_recovery_snapshot(legacy)['format'] == 'joint-recovery-snapshot/v5'
    restored = joint.restore_joint_recovery_snapshot(legacy, tmp_path / 'legacy', builds=recovery.BUILDS,
        current_storage_root=stored_sources[1], max_seconds=120)
    assert (restored / 'database.sqlite3').is_file()
    assert (restored / 'kg-artifacts').is_dir()
    if has_artifact:
        assert (restored / 'kg-artifacts/rebuild/audit/original.json').read_bytes() == (
            stored_sources[0][3] / 'kg/rebuild/audit/original.json').read_bytes()


@pytest.mark.parametrize('relative', ['boards/board-one/grafx/retained/opaque', 'quarantine/opaque'])
def test_retained_source_change_during_graph_exports_refuses_entire_set(stored_sources, monkeypatch, relative):
    original, _, _, _ = stored_sources
    retained = original[3] / 'kg' / relative
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b'before capture')
    export = joint.backup_logical_graph_file
    def race(*args, **kwargs):
        result = export(*args, **kwargs)
        retained.write_bytes(b'concurrent external change')
        return result
    monkeypatch.setattr(joint, 'backup_logical_graph_file', race)
    with pytest.raises(ValueError, match='kg_artifact_recovery_source_changed'):
        recovery.capture_stored(stored_sources)
    assert retained.read_bytes() == b'concurrent external change'
    assert not (original[2] / 'capture').exists()
    assert not list(original[2].glob('*.partial'))


def test_retired_physical_payloads_are_preserved_without_loading_an_old_runtime(stored_sources, tmp_path):
    original, uploads, _, _ = stored_sources
    kg = original[3] / 'kg'
    payloads = ('boards/board-one/graph.lbug', 'boards/board-one/graph.lbug.wal',
        'global/discovery.lbug', 'global/discovery.lbug.wal')
    for index, relative in enumerate(payloads):
        (kg / relative).write_bytes(b'opaque retired payload\x00' + bytes([index]))
    snapshot = recovery.capture_stored(stored_sources)
    restored = joint.restore_joint_recovery_snapshot(snapshot, tmp_path / 'retained-physical',
        builds=recovery.BUILDS, current_storage_root=uploads, max_seconds=120)
    for index, relative in enumerate(payloads):
        expected = b'opaque retired payload\x00' + bytes([index])
        assert (kg / relative).read_bytes() == expected
        assert (restored / 'kg-artifacts' / relative).read_bytes() == expected
