"""Adapt freshly rederived private observations to the Core completion gate."""

from okto_pulse.core.ports.global_projection import GlobalProjectionComparison
from okto_pulse.core.ports.projection_completion import BoardProjectionCompletion, require_projection_completion
from okto_pulse.core.ports.projection_qualification import ProjectionHistoryQualification
from okto_pulse.core.ports.projection_relations import ProjectionRelationComparison


def require_candidate_projection_completion(projection, report):
    """Only called after checkpoint authentication and full observation replay."""
    if report['format'] != 'retirement-candidate-graph-reconciliation/v16':
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
        boards.append(BoardProjectionCompletion(row['board_id'], ProjectionHistoryQualification(**history),
            ProjectionRelationComparison(**relations), row['historical_orphan_count'],
            sum(item['outcome'] not in {'passed', 'allowlisted'} for item in row['historical_connectivity']),
            sum(item['state'] != 'matched' for item in row['cognitive_source_parity']),
            row['restored_cognitive_node_count']))
    require_projection_completion(expected_boards=tuple(item['projection']['board_id'] for item in projection['boards']),
        boards=tuple(boards), global_comparison=global_evidence)
    if report['state'] != 'source_graph_reconciled':
        raise ValueError('retirement_completion_summary_pending')
