"""Native restoration owns only literal, newly introduced cognitive nodes."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from okto_pulse.core.ports.cognitive_projection import cognitive_projection_source_node
from okto_pulse.community.adapters import retirement_candidate_cognitive_restoration as restoration
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from logical_transfer_matrix_support import Corpus, seed_generation


def test_native_literal_write_replay_ownership_and_existing_identity(tmp_path, monkeypatch):
    schema = board_logical_schema()
    record = {'board_id': 'board', 'node_type': 'Decision', 'node_id': 'report', 'generation': 0,
        'source_revision': 0, 'source_session_id': 'kgses_old', 'evidence_refs': ['final_report:old'],
        'payload': {'title': 'sealed report', 'generation': 0, 'created_by_agent': 'agent-a',
            'source_artifact_ref': 'final_report:old', 'graph_layer': 'canonical',
            'maturity_status': 'canonical_eligible', 'created_at': '2026-01-01T00:00:00.123456Z'}}
    source_before = deepcopy(record)
    projection = {'boards': [{'projection': {'board_id': 'board', 'cognitive_rows': [record]}}]}
    path = tmp_path / 'graph'
    seed_generation('grafx', path, Corpus(schema, (), ()))
    binding = SimpleNamespace(physical_path=path, page_size=8192, backend='grafx')
    monkeypatch.setattr(restoration, 'CommunityGraphBackendBindingStore',
        lambda _: SimpleNamespace(inspect_board_binding=lambda _: binding))
    with pytest.raises(ValueError, match='execution_fence_lost'):
        restoration.restore_candidate_cognitive_nodes(tmp_path, projection, require_live=lambda: False, max_seconds=60)
    receipt = restoration.restore_candidate_cognitive_nodes(tmp_path, projection, require_live=lambda: True, max_seconds=60)
    created, = receipt['boards'][0]['created']
    assert created['node_id'] == 'report'
    _, nodes, edges = restoration._read(binding, 'board', _deadline(60))
    assert nodes == (cognitive_projection_source_node(schema=schema, board_id='board', record=record),)
    assert edges == () and record == source_before
    history = {'graphs': [{'scope': 'board', 'board_id': 'board', 'delta': {'introduced_nodes': [
        {'node_type': 'Decision', 'node_id': 'report', 'fingerprint': created['literal_fingerprint']}]}}]}
    assert restoration.verify_candidate_cognitive_restoration(tmp_path, projection, receipt, history, max_seconds=60) == {
        'board': {('Decision', 'report'): created['literal_fingerprint']}}
    with pytest.raises(ValueError, match='prior_identity'):
        restoration.verify_candidate_cognitive_restoration(tmp_path, projection, receipt, {'graphs': []}, max_seconds=60)
    tampered = deepcopy(receipt)
    tampered['boards'][0]['created'][0]['source_fingerprint'] = '0' * 64
    with pytest.raises(ValueError, match='receipt_changed'):
        restoration.verify_candidate_cognitive_restoration(tmp_path, projection, tampered, history, max_seconds=60)
    record['payload']['title'] = 'must not overwrite existing identity'
    repeated = restoration.restore_candidate_cognitive_nodes(tmp_path, projection, require_live=lambda: True, max_seconds=60)
    assert repeated['boards'][0]['created'] == []
    assert restoration._read(binding, 'board', _deadline(60))[1] == nodes
