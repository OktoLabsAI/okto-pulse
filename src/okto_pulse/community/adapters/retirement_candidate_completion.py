"""Adapt freshly rederived private observations to the Core completion gate."""

from okto_pulse.core.ports.global_projection import GlobalProjectionComparison
from okto_pulse.core.ports.cognitive_projection import CognitiveReplayQualification
from okto_pulse.core.ports.projection_completion import BoardProjectionCompletion, require_projection_completion
from okto_pulse.core.ports.projection_qualification import ProjectionHistoryQualification
from okto_pulse.core.ports.projection_relations import ProjectionRelationComparison


def require_candidate_projection_completion(projection, report):
    """Only called after checkpoint authentication and full observation replay."""
    if report['format'] not in {'retirement-candidate-graph-reconciliation/v16', 'retirement-candidate-graph-reconciliation/v17'}:
        raise ValueError('retirement_completion_report_invalid')
    global_report = report['global_projection_comparison']
    if not global_report or global_report['state'] != 'matched':
        raise ValueError('retirement_completion_global_pending')
    global_evidence = GlobalProjectionComparison(**{key: global_report[key]
        for key in GlobalProjectionComparison.__dataclass_fields__})
    boards = []
    for row in report['boards']:
        if (any(row[field] != 'passed' for field in ('edge_session_validation',
                'source_metadata_validation', 'source_partition_validation', 'zero_orphan_validation'))
                or row['history_qualification'] is None or row['source_relation_comparison'] is None):
            raise ValueError('retirement_completion_board_pending')
        history = dict(row['history_qualification'])
        history['reasons'] = tuple(history['reasons'])
        relations = dict(row['source_relation_comparison'])
        relations['issues'] = tuple(relations['issues'])
        # Older sealed installations retain their original stricter meaning.
        # No v16 report can acquire a qualification it never observed.
        unqualified = row['restored_cognitive_node_count']
        if type(unqualified) is not int or not 0 <= unqualified <= 100_000:
            raise ValueError('retirement_completion_cognitive_evidence_invalid')
        if report['format'] == 'retirement-candidate-graph-reconciliation/v17':
            reported_unqualified = row['unqualified_restored_cognitive_node_count']
            if type(reported_unqualified) is not int or not 0 <= reported_unqualified <= unqualified:
                raise ValueError('retirement_completion_cognitive_evidence_invalid')
            qualifications = row['restored_cognitive_qualification']
            if (type(qualifications) is not list or len(qualifications) != row['restored_cognitive_node_count']
                    or len({(item['node_type'], item['node_id']) for item in qualifications}) != len(qualifications)):
                raise ValueError('retirement_completion_cognitive_evidence_invalid')
            observations = tuple(CognitiveReplayQualification(**{**item, 'reasons': tuple(item['reasons'])})
                for item in qualifications)
            unqualified = sum(item.state != 'durable_replay_reconciled' for item in observations)
            if unqualified != reported_unqualified:
                raise ValueError('retirement_completion_cognitive_evidence_invalid')
        boards.append(BoardProjectionCompletion(row['board_id'], ProjectionHistoryQualification(**history),
            ProjectionRelationComparison(**relations), row['historical_orphan_count'],
            sum(item['outcome'] not in {'passed', 'allowlisted'} for item in row['historical_connectivity']),
            sum(item['state'] != 'matched' for item in row['cognitive_source_parity']),
            unqualified))
    require_projection_completion(expected_boards=tuple(item['projection']['board_id'] for item in projection['boards']),
        boards=tuple(boards), global_comparison=global_evidence)
    if report['state'] != 'source_graph_reconciled':
        raise ValueError('retirement_completion_summary_pending')
