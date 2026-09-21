"""Every namespace on the real artifact port remains recoverable verbatim."""

from typing import get_args

from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey, RebuildAuditNamespace
from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from okto_pulse.community.adapters import kg_artifact_recovery as custody


def test_every_declared_artifact_namespace_is_preserved(tmp_path):
    root = tmp_path / 'kg'
    root.mkdir()
    store = CommunityFileSystemRebuildAuditArtifactStore(root)
    for namespace in get_args(RebuildAuditNamespace):
        store.write_json_atomic(RebuildAuditKey(namespace, 'b', kg_generation_id='g', artifact_id=namespace),
            {'original': namespace, 'opaque_history': 'Sprint'})
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*.json')}
    assert len(before) == 16
    with custody.kg_artifact_capture_window(root,
            selected_paths=tuple(sorted(path.name for path in root.iterdir()))) as window:
        snapshot = window.capture(tmp_path / 'snapshot')
    custody.restore_kg_artifact_snapshot(snapshot, tmp_path / 'restored')
    observed = {path.relative_to(tmp_path / 'restored').as_posix(): path.read_bytes()
        for path in (tmp_path / 'restored').rglob('*.json')}
    assert observed == before
    assert {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*.json')} == before
