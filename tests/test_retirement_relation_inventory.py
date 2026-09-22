"""Historical metadata cannot be silently promoted by current-edge comparison."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL, LogicalFingerprintAccumulator, LogicalRelation, LogicalTimestamp
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


@pytest.mark.parametrize('case', ['covered', 'duplicate', 'unplanned', 'child_chronology', 'missing_source'])
def test_native_history_qualifies_only_exact_current_source_coverage(tmp_path, case):
    schema = one_node_corpus('board').schema
    nodes, candidates, partitions = [], [], {}
    for kind, key, ref in (('Entity', 'root', 'spec:s'), ('Requirement', 'child', 'spec:s:fr:r')):
        original = complete_node(schema, kind, key, 0, null_nullable=True)
        properties = {**original.properties, 'source_artifact_ref': ref, 'generation': 0,
            'source_session_id': 'old', 'created_by_agent': 'system:worker',
            'graph_layer': 'working', 'maturity_status': 'working_immature'}
        if key == 'root':
            properties['source_status'] = 'draft'
        if key == 'child' and case == 'child_chronology':
            properties['source_updated_at'] = LogicalTimestamp(42)
        nodes.append(replace(original, properties=properties))
        candidates.append({'candidate_id': key, 'node_type': kind, 'title': key, 'source_artifact_ref': ref,
            'graph_layer': 'working', 'maturity_status': 'working_immature'})
        partitions[ProjectionSourceRoot(kind, ref)] = ('working', 'working_immature')
    if case == 'missing_source':
        old = complete_node(schema, 'Decision', 'unclassified', 0, null_nullable=True)
        nodes.append(replace(old, properties={**old.properties, 'source_artifact_ref': 'legacy:unknown'}))
    layout = schema.relation_layout('belongs_to', 'Requirement', 'Entity')
    attrs = {field.name: LOGICAL_NULL for field in layout.properties}
    attrs.update(confidence=1.0, layer='deterministic', rule_id='unplanned' if case == 'unplanned' else 'belongs_to/source',
        created_by='system:worker', created_by_session_id='old', fallback_reason='')
    relation = LogicalRelation('belongs_to', 'Requirement', 'Entity', 'child', 'root', attrs)
    relations = (relation,) * (2 if case == 'duplicate' else 1)
    path = tmp_path / 'native'
    seed_generation('grafx', path, Corpus(schema, tuple(nodes), relations))
    prior_nodes = []
    for node in nodes:
        digest = LogicalFingerprintAccumulator.for_schema(schema)
        digest.add_node(node)
        prior_nodes.append({'node_type': node.type_name, 'node_id': node.key, 'fingerprint': digest.digest()})
    digest = LogicalFingerprintAccumulator.for_schema(schema)
    digest.add_relation(relation)
    history = {'delta': {'unchanged_nodes': prior_nodes, 'retained_edges': [{'edge_type': 'belongs_to',
        'source_type': 'Requirement', 'source_id': 'child', 'target_type': 'Entity', 'target_id': 'root',
        'fingerprint': digest.digest(), 'count': len(relations)}]}}
    plan = {'format': 'deterministic-board-projection-plan/v3', 'board_id': 'board',
        'captured_at': '2026-09-22T00:00:00+00:00', 'source_rows': [], 'cognitive_rows': [],
        'census': {}, 'dependency_closure': [], 'plans': [{
            'source': {'board_id': 'board', 'artifact_type': 'spec', 'artifact_id': 's'},
            'projection': {'nodes': candidates, 'edges': [{'candidate_id': 'edge', 'edge_type': 'belongs_to',
                'from_candidate_id': 'child', 'to_candidate_id': 'root', 'confidence': 1.0,
                'layer': 'deterministic', 'rule_id': 'belongs_to/source', 'created_by': 'system:worker'}]}}]}
    metadata = {'source_created_at': None, 'source_updated_at': None, 'source_status': 'draft',
        'severity': None, 'resolved_at': None}
    report = _board_graph(SimpleNamespace(physical_path=path, page_size=8192), {}, 0,
        {ProjectionSourceRoot('Entity', 'spec:s'): metadata}, _deadline(60), history, board_id='board',
        expected_partitions=partitions, expected_edge_sessions={'new': 0},
        planned_document=json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    qualified = report['history_qualification']
    assert qualified['state'] == ('current_source_reconciled' if case == 'covered' else 'pending')
    assert qualified['current_source_node_count'] == (1 if case == 'child_chronology' else 2)
    assert qualified['unclassified_node_count'] == (1 if case in ('missing_source', 'child_chronology') else 0)
    assert qualified['unclassified_relation_count'] == (len(relations) if case in ('duplicate', 'unplanned') else 0)
    if case == 'duplicate':
        assert report['source_relation_comparison']['duplicate_expected_count'] == 2
