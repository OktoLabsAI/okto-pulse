"""Frozen 0.5.0 graphs remain recoverable without runtime compatibility escapes."""

from dataclasses import asdict, replace
import sqlite3

from graph_observation_fixtures import enable_fixture_history

import pytest
from okto_grafx import connect

from okto_pulse.core.kg.logical_transfer import LogicalSchemaError, schema_digest
from okto_pulse.community.adapters import grafx_recovery_contracts as contracts
from okto_pulse.community.adapters import joint_recovery_snapshot as joint
from okto_pulse.community.adapters.grafx_schema_evolution import rebuild_grafx_schema_candidate
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source
from okto_pulse.community.adapters.logical_graph_transfer import backup_logical_graph_file
from okto_pulse.community.adapters.native_graph_recovery_snapshot import (
    capture_native_graph_snapshot, restore_native_graph_snapshot,
)
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from test_grafx_schema_evolution import _build_populated_predecessor, _durable_bytes
from test_joint_recovery_snapshot import BUILDS
from test_joint_recovery_native_history import history


def test_recovery_contract_is_frozen_and_unknown_artifact_cannot_create_destination(tmp_path, monkeypatch):
    contract = contracts.predecessor_recovery_contract()
    assert len(contract.schema.node_types) == 12 and len(contract.schema.relation_layouts) == 69
    assert sum(len(node.properties) for node in contract.schema.node_types) == 489
    assert 'source_created_at' not in contract.schema.node_type('Entity').property_names()
    with pytest.raises(LogicalSchemaError, match='unrecognized recovery artifact'):
        contracts.make_grafx_recovery_logical_sink(tmp_path / 'forbidden', scope='board', expected_schema_digest='0' * 64)
    assert not (tmp_path / 'forbidden').exists()
    monkeypatch.setattr(contracts, 'PREDECESSOR', replace(contracts.PREDECESSOR, logical_fingerprint='0' * 64))
    with pytest.raises(LogicalSchemaError, match='frozen predecessor'):
        contracts.predecessor_recovery_contract()


@pytest.mark.timeout(360)
def test_predecessor_joint_logical_and_native_recovery_keep_schema_data_and_history(tmp_path):
    live = tmp_path / 'live'
    live.mkdir()
    with _build_populated_predecessor(live / 'v0312') as old:
        rebuild_grafx_schema_candidate(old, live / 'v050', batch_size=100)
    recovery = tmp_path / 'recovery'
    recovery.mkdir()
    sql = live / 'database.sqlite3'
    with sqlite3.connect(sql) as connection:
        connection.execute('CREATE TABLE preserved(value TEXT)')
        connection.execute("INSERT INTO preserved VALUES ('old schema evidence')")
    with connect(live / 'v050', page_size=4096) as source:
        board = source.execute('MATCH (m:BoardMeta) RETURN m.board_id').rows[0][0]
        with pytest.raises(LogicalSchemaError):
            make_grafx_logical_source(source, scope='board').open_snapshot()
        reader = history(source)
        enable_fixture_history(source)
        with source.begin('write') as transaction:
            transaction.execute("MATCH (n:Decision) SET n.title='historical observation'")
        commits = reader.commits(board)
        cursor = commits['entries'][-1]['commit']
        past = reader.as_of(board, cursor, ('Decision',), ())
        identity = source.identity.database_uuid
        source.checkpoint()
        original = _durable_bytes(live / 'v050')
        snapshot = joint.create_joint_recovery_snapshot(sql, (joint.RecoveryGraph(source, 'board', board),),
            recovery, snapshot_id='predecessor', builds=BUILDS, runtime_directories=(live,), max_seconds=180)
        manifest = joint.verify_joint_recovery_snapshot(snapshot, max_seconds=180)
        assert manifest['graphs'][0]['certificate']['schema_digest'] == schema_digest(contracts.predecessor_recovery_contract().schema)
        assert _durable_bytes(live / 'v050') == original
        restored = joint.restore_joint_recovery_snapshot(snapshot, tmp_path / 'restored-logical', builds=BUILDS, max_seconds=180)
        with connect(restored / 'graph-0000', page_size=8192, read_only=True) as cold:
            certificate = backup_logical_graph_file(tmp_path / 'restored.jsonl',
                contracts.make_grafx_recovery_logical_source(cold, scope='board'))
            expected = manifest['graphs'][0]['certificate']
            assert certificate.schema_digest == expected['schema_digest']
            assert certificate.fingerprint == expected['fingerprint']
            assert asdict(certificate.counts) == expected['counts']
        native = capture_native_graph_snapshot(source, recovery / 'native', max_seconds=180)
    restored_native = tmp_path / 'restored-native'
    restore_native_graph_snapshot(native, restored_native, confirm_original_offline=True, max_seconds=180)
    with connect(restored_native, page_size=4096, read_only=True) as cold:
        joint._verify_native_logical(cold, manifest['graphs'][0], 500, _deadline(180))
        assert cold.identity.database_uuid == identity
        assert history(cold).commits(board) == commits
        assert history(cold).as_of(board, cursor, ('Decision',), ()) == past
