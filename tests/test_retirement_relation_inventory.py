"""Historical metadata cannot be silently promoted by current-edge comparison."""

from dataclasses import replace
import json
from types import SimpleNamespace

from okto_pulse.core.kg.logical_transfer import LogicalFingerprintAccumulator
from okto_pulse.core.ports.projection_history import ProjectionSourceRoot
from okto_pulse.community.adapters.retirement_candidate_graph_reconciliation import _board_graph
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from logical_transfer_matrix_support import Corpus, complete_node, one_node_corpus, seed_generation


def test_cold_native_history_with_unclassified_metadata_stays_preserved_and_pending(tmp_path):
    schema = one_node_corpus('board').schema
    root = complete_node(schema, 'Entity', 'root', 0, null_nullable=True)
    root = replace(root, properties={**root.properties, 'source_artifact_ref': 'board:board',
        'source_session_id': 'new', 'created_by_agent': 'system:worker', 'generation': 0,
        'graph_layer': 'working', 'maturity_status': 'working_immature'})
    old = complete_node(schema, 'Decision', 'old', 0, null_nullable=True)
    old = replace(old, properties={**old.properties, 'source_artifact_ref': 'legacy:' + 'x' * 1200,
        'generation': -1, 'superseded_by': ''})
    path = tmp_path / 'native'
    seed_generation('grafx', path, Corpus(schema, (root, old), ()))
    fingerprint = LogicalFingerprintAccumulator.for_schema(schema)
    fingerprint.add_node(old)
    prior = {'delta': {'unchanged_nodes': [{'node_type': old.type_name, 'node_id': old.key,
        'fingerprint': fingerprint.digest()}], 'retained_edges': []}}
    plan = {'format': 'deterministic-board-projection-plan/v3', 'board_id': 'board',
        'captured_at': '2026-09-22T00:00:00+00:00', 'source_rows': [], 'cognitive_rows': [],
        'census': {}, 'dependency_closure': [], 'plans': [{
            'source': {'board_id': 'board', 'artifact_type': 'spec', 'artifact_id': 's'},
            'projection': {'nodes': [{'candidate_id': 'root', 'node_type': 'Entity', 'title': 'root',
                'source_artifact_ref': 'board:board', 'graph_layer': 'working',
                'maturity_status': 'working_immature'}], 'edges': []}}]}
    report = _board_graph(SimpleNamespace(physical_path=path, page_size=8192), {('Entity', 'root'): 'new'},
        0, {}, _deadline(60), prior, board_id='board',
        expected_partitions={ProjectionSourceRoot('Entity', 'board:board'): ('working', 'working_immature')},
        expected_edge_sessions={'new': 0},
        planned_document=json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    assert report['source_relation_comparison']['unresolved_count'] == 0
    assert report['source_relation_comparison']['unexpected_new_count'] == 0
    assert report['historical_node_count'] == 1
    assert report['history_classification'] == 'pending'
    assert report['zero_orphan_validation'] == 'pending_history_classification'
