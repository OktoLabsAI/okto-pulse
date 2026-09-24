"""The frozen additive evolution preserves history evidence and old properties."""

from dataclasses import replace

from graph_observation_fixtures import enable_fixture_history

import pytest
from okto_grafx import connect

from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL, LogicalNode, LogicalTimestamp, encode_value, schema_digest, transfer_logical_graph,
)
from okto_pulse.community.adapters import grafx_recovery_contracts as contracts
from okto_pulse.community.adapters import retirement_schema_evolution as evolution
from okto_pulse.community.adapters.retirement_historical_graph_census import read_retirement_historical_graph_census
from okto_pulse.community.adapters.retirement_schema_baseline import read_retirement_schema_baseline
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source
from logical_transfer_matrix_support import Corpus, MaterializedSource, complete_relation
import test_joint_recovery_snapshot as recovery
from test_joint_recovery_native_history import capture, history


@pytest.fixture
def sources(tmp_path, monkeypatch):
    original_corpus, original_seed = recovery.one_node_corpus, recovery.seed_generation

    def corpus(scope):
        sample = original_corpus(scope)
        if scope != 'board':
            return sample
        schema = contracts.predecessor_recovery_contract().schema
        original = sample.nodes[0]
        node = replace(original, properties={name: value for name, value in original.properties.items()
            if name in schema.node_type(original.type_name).property_names()})
        metadata = LogicalNode('BoardMeta', 'board-one', {'board_id': 'board-one', 'schema_version': '0.5.0',
            'bootstrapped_at': LogicalTimestamp(0), 'embedding_model': 'retained-fixture', 'embedding_dimension': 384})
        relation = complete_relation(schema, schema.relation_layout('supersedes', 'Decision', 'Decision'),
            node.key, node.key, 7)
        return Corpus(schema, (node, metadata), (relation, relation))

    def seed(backend, path, value):
        if value.schema.scope != 'board':
            return original_seed(backend, path, value)
        # The shared recovery fixture deliberately retains just its first domain
        # node when assigning retired provenance. Evolution also needs BoardMeta.
        metadata = LogicalNode('BoardMeta', 'board-one', {'board_id': 'board-one', 'schema_version': '0.5.0',
            'bootstrapped_at': LogicalTimestamp(0), 'embedding_model': 'retained-fixture', 'embedding_dimension': 384})
        value = replace(value, nodes=(*value.nodes, metadata))
        sink = contracts.make_grafx_recovery_logical_sink(path, scope='board', expected_schema_digest=schema_digest(value.schema))
        return transfer_logical_graph(MaterializedSource(value), sink)

    monkeypatch.setattr(recovery, 'one_node_corpus', corpus)
    monkeypatch.setattr(recovery, 'seed_generation', seed)
    yield from recovery.sources.__wrapped__(tmp_path)


stored_sources = recovery.stored_sources


@pytest.mark.timeout(180)
def test_additive_candidate_keeps_all_old_values_parallel_edges_and_native_backup(stored_sources, tmp_path):
    original = stored_sources[0]
    graph = original[1][0].database
    reader = history(graph)
    enable_fixture_history(graph)
    with graph.begin('write') as transaction:
        transaction.execute("MATCH (n:Decision) SET n.title='retained native history'")
    commits = reader.commits('board-one')
    snapshot = capture(stored_sources)
    target = tmp_path / 'v060'
    result = evolution.build_retirement_v060_graph(snapshot, target, board_id='board-one', builds=recovery.BUILDS)
    assert result['state'] == 'evolved_not_reconciled'
    assert result['runtime_admission'] == 'not_authorized'
    assert result['history_access'] == 'retained_predecessor_native_backup'
    assert reader.commits('board-one') == commits
    assert result['after']['counts']['relations'] == result['before']['counts']['relations'] == 2
    assert result['after']['counts']['properties'] == result['before']['counts']['properties'] + 5
    census, digest = read_retirement_historical_graph_census(snapshot)
    baseline, baseline_digest = read_retirement_schema_baseline(snapshot, census, digest, (result,))
    assert baseline_digest != digest and baseline['original_census_sha256'] == digest
    assert baseline['schema_evolutions'] == [result]
    changed = {**result, 'after': {**result['after'], 'fingerprint': '0' * 64}}
    with pytest.raises(ValueError, match='retirement_schema_evolution_receipt_changed'):
        read_retirement_schema_baseline(snapshot, census, digest, (changed,))
    with pytest.raises(ValueError, match='retirement_schema_evolution_scope_invalid'):
        read_retirement_schema_baseline(snapshot, census, digest, (result, result))
    before_reader = contracts.make_grafx_recovery_logical_source(graph, scope='board').open_snapshot()
    try:
        old = {(node.type_name, node.key): node for batch in before_reader.iter_nodes(batch_size=50) for node in batch}
    finally:
        before_reader.close()
    with connect(target, page_size=8192, read_only=True) as candidate:
        assert candidate.identity.database_uuid != graph.identity.database_uuid
        after_reader = make_grafx_logical_source(candidate, scope='board').open_snapshot()
        try:
            for batch in after_reader.iter_nodes(batch_size=50):
                for node in batch:
                    previous = old.pop((node.type_name, node.key))
                    for name, value in previous.properties.items():
                        if node.type_name == 'BoardMeta' and name == 'schema_version':
                            assert node.properties[name] == '0.6.0' and value == '0.5.0'
                        else:
                            assert encode_value(node.properties[name]) == encode_value(value)
                    if node.type_name != 'BoardMeta':
                        assert all(node.properties[name] is LOGICAL_NULL for name in evolution._INTRODUCED)
            assert not old
        finally:
            after_reader.close()
    # Existing destinations and source overlap must not be overwritten.
    with pytest.raises(Exception):
        evolution.build_retirement_v060_graph(snapshot, target, board_id='board-one', builds=recovery.BUILDS)
    with pytest.raises(ValueError, match='overlaps_source'):
        evolution.build_retirement_v060_graph(snapshot, snapshot.directory / 'forbidden',
            board_id='board-one', builds=recovery.BUILDS)


@pytest.mark.timeout(180)
def test_inconsistent_predecessor_stamp_refuses_and_abandons_new_candidate(stored_sources, tmp_path):
    graph = stored_sources[0][1][0].database
    with graph.begin('write') as transaction:
        transaction.execute("MATCH (n:BoardMeta) SET n.schema_version='unknown'")
    snapshot = capture(stored_sources)
    target = tmp_path / 'refused'
    with pytest.raises(Exception, match='board metadata ambiguous'):
        evolution.build_retirement_v060_graph(snapshot, target, board_id='board-one', builds=recovery.BUILDS)
    assert not target.exists()
    assert graph.execute('MATCH (n:BoardMeta) RETURN n.schema_version').rows == (('unknown',),)
