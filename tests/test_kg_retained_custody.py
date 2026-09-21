"""Retained generations are bounded opaque custody, never binding authority."""

import hashlib
import json

import pytest

from okto_pulse.community.adapters import kg_artifact_recovery as custody
from okto_pulse.community.adapters.recovery_graph_inventory import (
    RecoveryGraphInventory, RecoveryGraphRoute, require_retained_generation_inventory,
)


def capture(tmp_path):
    root = tmp_path / 'kg'
    retained = 'boards/b/grafx/retained'
    for relative in (retained + '/grafx.meta', 'boards/b/grafx/active/original', 'quarantine/opaque'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'opaque\x00')
    with custody.kg_artifact_capture_window(root, selected_paths=('quarantine',),
            retained_generations=(retained,)) as window:
        snapshot = window.capture(tmp_path / 'snapshot')
    return snapshot


def test_exact_retained_selection_does_not_copy_active_siblings(tmp_path):
    snapshot = capture(tmp_path)
    manifest = custody.verify_kg_artifact_snapshot(snapshot)
    assert manifest['roots'] == ['boards/b/grafx/retained', 'quarantine']
    assert {row['path'] for row in manifest['files']} == {'boards/b/grafx/retained/grafx.meta', 'quarantine/opaque'}
    custody.restore_kg_artifact_snapshot(snapshot, tmp_path / 'restored')
    assert not (tmp_path / 'restored/boards/b/grafx/active').exists()


@pytest.mark.parametrize('authenticated', [False, True])
def test_extra_sibling_is_rejected_even_when_added_to_authenticated_manifest(tmp_path, authenticated):
    snapshot = capture(tmp_path)
    sibling = snapshot.directory / 'files/boards/b/graph_backend_binding.json'
    sibling.write_bytes(b'{}')
    if authenticated:
        manifest_path = snapshot.directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        manifest['files'].append({'path': 'boards/b/graph_backend_binding.json', 'size': 2,
            'sha256': hashlib.sha256(b'{}').hexdigest()})
        manifest['files'].sort(key=lambda row: row['path'])
        encoded = custody._encode(manifest)
        manifest_path.write_bytes(encoded)
        snapshot = custody.KGArtifactRecoverySnapshot(snapshot.directory, hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ValueError, match='kg_artifact_recovery_'):
        custody.restore_kg_artifact_snapshot(snapshot, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('relative', ['boards', 'boards/b', 'boards/b/grafx', 'boards/b/grafx/a/extra',
    'global/grafx', 'global/elsewhere/a', 'boards/b/grafx/../a', '/global/grafx/a'])
def test_retained_path_must_select_one_canonical_generation(tmp_path, relative):
    with pytest.raises(ValueError, match='kg_artifact_recovery_'):
        with custody.kg_artifact_capture_window(tmp_path, selected_paths=(), retained_generations=(relative,)):
            pytest.fail('invalid retained generation admitted')


@pytest.mark.parametrize('paths', [
    ('boards/b/grafx/active',), ('boards/b/grafx/ACTIVE',),
    ('boards/unknown/grafx/old',), ('global/grafx/active',),
    ('boards/b/grafx/old', 'boards/b/grafx/OLD'),
])
def test_retained_inventory_cannot_relabel_active_or_unknown_storage(paths):
    inventory = RecoveryGraphInventory('unused', ('b',), (
        RecoveryGraphRoute('board', 'b', 'bound', 'active'),
        RecoveryGraphRoute('global_discovery', None, 'bound', 'active'),
    ), paths, (), ())
    with pytest.raises(ValueError, match='recovery_graph_inventory_retained_'):
        require_retained_generation_inventory(inventory)


def test_legacy_nested_manifest_cannot_claim_extended_retained_roots(tmp_path):
    snapshot = capture(tmp_path)
    path = snapshot.directory / 'manifest.json'
    manifest = json.loads(path.read_bytes())
    manifest['format'] = 'kg-artifact-recovery/v1'
    encoded = custody._encode(manifest)
    path.write_bytes(encoded)
    legacy = custody.KGArtifactRecoverySnapshot(snapshot.directory, hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ValueError, match='manifest_roots_invalid'):
        custody.verify_kg_artifact_snapshot(legacy)


def test_historical_directory_and_sidecar_do_not_authorize_neighbor_bindings(tmp_path):
    root = tmp_path / 'kg'
    historical = root / 'boards/b/graph.lbug'
    historical.mkdir(parents=True)
    (historical / 'opaque').write_bytes(b'damaged historical database')
    (historical.parent / 'graph.lbug.wal').write_bytes(b'historical sidecar')
    (historical.parent / 'graph_backend_binding.json').write_bytes(b'current authority')
    with custody.kg_artifact_capture_window(root,
            selected_paths=('boards/b/graph.lbug', 'boards/b/graph.lbug.wal')) as window:
        snapshot = window.capture(tmp_path / 'snapshot')
    custody.restore_kg_artifact_snapshot(snapshot, tmp_path / 'restored')
    assert (tmp_path / 'restored/boards/b/graph.lbug/opaque').read_bytes() == b'damaged historical database'
    assert (tmp_path / 'restored/boards/b/graph.lbug.wal').read_bytes() == b'historical sidecar'
    assert not (tmp_path / 'restored/boards/b/graph_backend_binding.json').exists()


def test_unknown_board_cannot_be_invented_for_retired_payload():
    inventory = RecoveryGraphInventory('unused', ('b',), (), (), ('boards/unknown/graph.lbug',), ())
    with pytest.raises(ValueError, match='recovery_graph_inventory_retained_scope_invalid'):
        require_retained_generation_inventory(inventory)
