"""Cold native records are compared with the captured durable cognitive source."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from okto_pulse.core.kg.logical_transfer import LogicalFingerprintAccumulator
from okto_pulse.community.adapters import retirement_candidate_graph_reconciliation as reconciliation
from okto_pulse.community.adapters.retirement_candidate_graph_reconciliation import _board_graph
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from logical_transfer_matrix_support import Corpus, complete_node, one_node_corpus, seed_generation


@pytest.mark.parametrize('state', ['matched', 'different', 'missing_node'])
def test_cold_cognitive_source_parity_never_admits_history(tmp_path, state, monkeypatch):
    schema = one_node_corpus('board').schema
    node = complete_node(schema, 'Decision', 'durable-old', 0, null_nullable=True)
    node = replace(node, properties={**node.properties, 'title': 'changed' if state == 'different' else 'sealed',
        'generation': 0, 'source_session_id': 'session', 'source_artifact_ref': 'spec:old'})
    nodes = () if state == 'missing_node' else (node,)
    path = tmp_path / 'native'
    seed_generation('grafx', path, Corpus(schema, nodes, ()))
    measured = LogicalFingerprintAccumulator.for_schema(schema)
    measured.add_node(node)
    history = {'delta': {'unchanged_nodes': [] if not nodes else [
        {'node_type': 'Decision', 'node_id': node.key, 'fingerprint': measured.digest()}], 'retained_edges': []}}
    source = {'board_id': 'board', 'node_type': 'Decision', 'node_id': node.key, 'generation': 0,
        'source_revision': 0, 'source_session_id': 'session', 'evidence_refs': ['spec:old'],
        'payload': {'title': 'sealed', 'generation': 0, 'source_artifact_ref': 'spec:old'}}
    report = _board_graph(SimpleNamespace(physical_path=path, page_size=8192), {}, 0, {}, _deadline(60),
        history, board_id='board', cognitive_rows=(source,))
    assert report['cognitive_source_parity'][0]['state'] == state
    assert report['cognitive_source_parity'][0]['differing_fields'] == (['title'] if state == 'different' else [])
    assert report['history_classification'] == ('pending' if nodes else 'not_applicable')
    assert len(report['historical_connectivity']) == (1 if nodes else 0)
    if nodes:
        # Matching durable content does not repair missing semantic edges.
        assert report['historical_connectivity'][0]['outcome'] == 'rejected'
    assert json.loads(json.dumps(report)) == report  # Checkpoint replay compares parsed receipts.
    old = {**source, 'source_revision': -1}
    with pytest.raises(ValueError, match='source_invalid'):
        _board_graph(SimpleNamespace(physical_path=path, page_size=8192), {}, 0, {}, _deadline(60),
            history, board_id='board', cognitive_rows=(old, source))
    old = {**source, 'source_revision': 0, 'generation': False}
    current = {**source, 'source_revision': 1}
    with pytest.raises(ValueError, match='source_invalid'):
        _board_graph(SimpleNamespace(physical_path=path, page_size=8192), {}, 0, {}, _deadline(60),
            history, board_id='board', cognitive_rows=(old, current))
    if state == 'missing_node':
        binding = SimpleNamespace(physical_path=path, page_size=8192, backend='grafx',
            generation='private', binding_sha256='a' * 64)
        monkeypatch.setattr(reconciliation, 'CommunityGraphBackendBindingStore',
            lambda _: SimpleNamespace(inspect_board_binding=lambda _: binding))
        monkeypatch.setattr(reconciliation, '_relational_evidence', lambda *_: ({}, {}))
        result = reconciliation.verify_candidate_graph_reconciliation(tmp_path,
            [{'acks': [], 'binding': {'board_id': 'board', 'generation': 'private'}}],
            projection={'boards': [{'projection': {'format': 'deterministic-board-projection-plan/v3',
                'board_id': 'board', 'plans': [], 'cognitive_rows': [source], 'source_rows': [],
                'captured_at': '2026-09-22T00:00:00+00:00', 'census': {}, 'dependency_closure': []}}]},
            deadline=_deadline(60))
        assert result['state'] == 'source_projection_reconciled_history_pending'
