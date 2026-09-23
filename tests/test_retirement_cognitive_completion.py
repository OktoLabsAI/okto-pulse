"""Old terminal proofs keep their stricter meaning; new proofs need typed details."""
from dataclasses import asdict

import pytest

from okto_pulse.core.ports.global_projection import GlobalProjectionComparison
from okto_pulse.core.ports.projection_qualification import ProjectionHistoryQualification
from okto_pulse.core.ports.projection_relations import ProjectionRelationComparison
from okto_pulse.community.adapters.retirement_candidate_completion import require_candidate_projection_completion


def report(version='v17', restored=1):
    return {'format': 'retirement-candidate-graph-reconciliation/' + version, 'state': 'source_graph_reconciled',
        'global_projection_comparison': asdict(GlobalProjectionComparison('matched', 1, 1, 0, 0, 0, 0, 0, 'a' * 64)),
        'boards': [{'board_id': 'board', 'edge_session_validation': 'passed', 'source_metadata_validation': 'passed',
            'source_partition_validation': 'passed', 'zero_orphan_validation': 'passed',
            'history_qualification': asdict(ProjectionHistoryQualification('not_applicable', 0, 0, 0, 0, ())),
            'source_relation_comparison': asdict(ProjectionRelationComparison(0, 0, 0, 0, 0, 0, 0, 'b' * 64, (), False)),
            'historical_orphan_count': 0, 'historical_connectivity': [], 'cognitive_source_parity': [],
            'restored_cognitive_node_count': restored, 'unqualified_restored_cognitive_node_count': 0,
            'restored_cognitive_qualification': [{'node_type': 'Decision', 'node_id': 'report',
                'state': 'durable_replay_reconciled', 'reasons': [], 'source_fingerprint': 'c' * 64}] if restored else []}]}


def check(value):
    return require_candidate_projection_completion({'boards': [{'projection': {'board_id': 'board'}}]}, value)


def test_v16_cannot_borrow_new_qualification_even_if_extra_fields_are_present():
    with pytest.raises(ValueError, match='cognitive_pending'):
        check(report('v16'))
    assert check(report('v16', 0)) is None
    assert check(report()) is None


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'fingerprint', 'reason', 'pending', 'boolean_count',
    'boolean_unqualified', 'negative_count', 'excessive_count', 'negative_unqualified'])
def test_summary_or_count_cannot_hide_incomplete_qualification(mutation):
    value = report()
    row = value['boards'][0]
    entry = row['restored_cognitive_qualification'][0]
    if mutation == 'missing': row['restored_cognitive_qualification'] = []
    if mutation == 'duplicate': row['restored_cognitive_qualification'].append(dict(entry))
    if mutation == 'fingerprint': entry['source_fingerprint'] = 'forged'
    if mutation == 'reason': entry['reasons'] = ['unproven']
    if mutation == 'pending': entry.update(state='pending', reasons=['unproven'])
    if mutation == 'boolean_count': row['restored_cognitive_node_count'] = True
    if mutation == 'boolean_unqualified': row['unqualified_restored_cognitive_node_count'] = False
    if mutation == 'negative_count': row['restored_cognitive_node_count'] = -1
    if mutation == 'excessive_count': row['restored_cognitive_node_count'] = 100_001
    if mutation == 'negative_unqualified': row['unqualified_restored_cognitive_node_count'] = -1
    with pytest.raises((TypeError, ValueError)):
        check(value)
