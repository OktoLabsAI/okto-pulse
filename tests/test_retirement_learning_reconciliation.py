"""Upgrade records exact authored work and unavailable historical semantics.

This stage selects work; it must neither invent a Learning nor grant admission.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters import retirement_candidate_cognitive_restoration as restoration
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from logical_transfer_matrix_support import Corpus, seed_generation


def historical_record():
    return {'board_id': 'board', 'node_type': 'Learning', 'node_id': 'historical', 'generation': 0,
        'source_revision': 0, 'source_session_id': 'kgses_old', 'evidence_refs': ['final_report:old'],
        'payload': {'title': 'Historical lesson', 'content': 'Original content', 'generation': 0,
            'created_by_agent': 'agent-a', 'source_artifact_ref': 'final_report:old',
            'graph_layer': 'canonical', 'maturity_status': 'canonical_eligible',
            'created_at': '2026-01-01T00:00:00.123456Z'}}


def prepare(tmp_path, monkeypatch, record):
    schema = board_logical_schema()
    path = tmp_path / 'graph'
    seed_generation('grafx', path, Corpus(schema, (), ()))
    binding = SimpleNamespace(physical_path=path, page_size=8192, backend='grafx')
    monkeypatch.setattr(restoration, 'CommunityGraphBackendBindingStore',
        lambda _: SimpleNamespace(inspect_board_binding=lambda _: binding))
    projection = {'boards': [{'projection': {'board_id': 'board', 'cognitive_rows': [record]}}]}
    return projection, binding


def test_upgrade_marks_missing_semantic_history_without_fabricating_capture(tmp_path, monkeypatch):
    record = historical_record()
    projection, binding = prepare(tmp_path, monkeypatch, record)
    before = deepcopy(projection)
    receipt = restoration.restore_candidate_cognitive_nodes(tmp_path, projection,
        require_live=lambda: True, max_seconds=60)
    plan, = receipt['boards'][0]['learning_reconciliation']
    assert plan['node_id'] == 'historical'
    assert plan['state'] == 'source_unavailable'
    assert plan['reasons'] == ['learning_original_semantic_source_unavailable']
    assert plan['work_refs'] == []
    assert projection == before
    _, nodes, edges = restoration._read(binding, 'board', _deadline(60))
    assert edges == ()
    assert all(node.properties['content'] == 'Original content' for node in nodes)


def test_reconciliation_plan_is_rederived_and_old_receipt_keeps_old_meaning(tmp_path, monkeypatch):
    record = historical_record()
    projection, _ = prepare(tmp_path, monkeypatch, record)
    receipt = restoration.restore_candidate_cognitive_nodes(tmp_path, projection,
        require_live=lambda: True, max_seconds=60)
    created = receipt['boards'][0]['created']
    history = {'graphs': [{'scope': 'board', 'board_id': 'board', 'delta': {'introduced_nodes': [
        {'node_type': row['node_type'], 'node_id': row['node_id'], 'fingerprint': row['literal_fingerprint']}
        for row in created]}}]}
    owned = restoration.verify_candidate_cognitive_restoration(tmp_path, projection,
        receipt, history, max_seconds=60)
    changed = deepcopy(receipt)
    changed['boards'][0]['learning_reconciliation'][0]['reasons'] = []
    with pytest.raises(ValueError, match='receipt_changed'):
        restoration.verify_candidate_cognitive_restoration(tmp_path, projection,
            changed, history, max_seconds=60)
    legacy = deepcopy(receipt)
    legacy['format'] = 'retirement-cognitive-restoration/v1'
    del legacy['boards'][0]['learning_reconciliation']
    assert restoration.verify_candidate_cognitive_restoration(tmp_path, projection,
        legacy, history, max_seconds=60) == owned
    legacy['boards'][0]['learning_reconciliation'] = []
    with pytest.raises(ValueError, match='scope_changed'):
        restoration.verify_candidate_cognitive_restoration(tmp_path, projection,
            legacy, history, max_seconds=60)


@pytest.mark.parametrize('encoded', [False, True])
def test_upgrade_selects_real_capture_identity_without_materializing_it(tmp_path, monkeypatch, encoded):
    from okto_pulse.core.domain.learning_materialization_work import parse_learning_capture_work_ref
    from okto_pulse.core.ports.kg_cognitive_source import canonical_cognitive_source_fingerprint
    record = {'board_id': 'board', 'node_type': 'Learning', 'node_id': 'captured', 'generation': 0,
        'source_revision': 0, 'evidence_refs': ['test_task:test-a'],
        'payload': {'capture_format': 'learning-capture/v1', 'capture_id': 'capture-a',
            'author_id': 'author', 'captured_at': '2026-09-24T12:00:00+00:00',
            'content': 'Retry only idempotent operations.', 'context': 'Worker retries',
            'applicability': 'Operations with an idempotency key',
            'source': {'board_id': 'board', 'bug_id': 'bug-a', 'policy_version': 2,
                'digest': 'a' * 64, 'evidence_refs': ['test_task:test-a']},
            'intent': {'kind': 'create', 'target_node_id': None, 'target_generation': None,
                'expected_fingerprint': None, 'reason': None}}}
    record['record_fingerprint'] = canonical_cognitive_source_fingerprint(
        **{key: record[key] for key in ('board_id', 'node_type', 'node_id', 'generation', 'payload', 'evidence_refs')})
    if encoded:
        record = {**record, 'payload': json.dumps(record['payload']),
            'evidence_refs': json.dumps(record['evidence_refs'])}
    projection, binding = prepare(tmp_path, monkeypatch, record)
    before = deepcopy(projection)
    receipt = restoration.restore_candidate_cognitive_nodes(tmp_path, projection,
        require_live=lambda: True, max_seconds=60)
    board, = receipt['boards']
    assert board['created'] == []
    selection, = board['learning_reconciliation']
    assert selection['state'] == 'awaiting_revalidation'
    work, = [parse_learning_capture_work_ref(ref) for ref in selection['work_refs']]
    assert (work.bug_id, work.learning_id, work.generation, work.fingerprint) == (
        'bug-a', 'captured', 0, record['record_fingerprint'])
    assert restoration._read(binding, 'board', _deadline(60))[1:] == ((), ())
    assert projection == before
    assert restoration.verify_candidate_cognitive_restoration(tmp_path, projection, receipt,
        {'graphs': []}, max_seconds=60) == {'board': {}}
