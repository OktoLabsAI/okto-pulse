"""Adapt the verified complete phase to Core's exact capture composition."""

from dataclasses import asdict

from okto_pulse.core.ports.cognitive_projection import CognitiveProjectionParity
from okto_pulse.core.ports.learning_reconciliation import (
    LearningReconciliationApplicability, LearningReconciliationExecution,
    LearningReconciliationSelection, qualify_learning_reconciliation_sources,
)


def qualify_candidate_learning_sources(phase, applicability, reports):
    from .retirement_learning_execution import VerifiedLearningPhase

    if type(phase) is not VerifiedLearningPhase:
        raise TypeError('retirement_learning_verified_phase_required')
    if (type(applicability) is not dict or set(applicability) != {'format', 'state', 'observations', 'unmaterialized'}
            or applicability['format'] != 'retirement-learning-applicability/v1'
            or applicability['state'] != 'observed_not_admitted'):
        raise ValueError('retirement_learning_qualification_applicability_invalid')
    board_ids = {row['board_id'] for row in phase.boards}
    if len(board_ids) != len(phase.boards) or board_ids != set(phase.initial_graphs):
        raise ValueError('retirement_learning_qualification_scope_invalid')
    rows = {row['board_id']: row for row in reports}
    if len(rows) != len(reports) or not board_ids <= set(rows):
        raise ValueError('retirement_learning_qualification_scope_invalid')
    observations = tuple(LearningReconciliationApplicability(**row) for row in applicability['observations'])
    if any(row.board_id not in board_ids for row in observations):
        raise ValueError('retirement_learning_qualification_scope_invalid')
    unmaterialized = [step['execution'] for board in phase.boards for step in board['steps']
        if step['execution']['materialized'] is False]
    if unmaterialized != applicability['unmaterialized']:
        raise ValueError('retirement_learning_qualification_unmaterialized_changed')
    result = []
    for board in phase.boards:
        board_id = board['board_id']
        qualification = qualify_learning_reconciliation_sources(board_id=board_id,
            selections=tuple(LearningReconciliationSelection(**{**row,
                'generations': tuple(row['generations']), 'work_refs': tuple(row['work_refs']),
                'reasons': tuple(row['reasons'])}) for row in board['selection']),
            executions=tuple(LearningReconciliationExecution(**row['execution']) for row in board['steps']),
            applicability=tuple(row for row in observations if row.board_id == board_id),
            parity=tuple(CognitiveProjectionParity(**{**row,
                'differing_fields': tuple(row['differing_fields']), 'usage_differences': tuple(row['usage_differences'])})
                for row in rows[board_id]['cognitive_source_parity']))
        result.append({'board_id': board_id, **asdict(qualification),
            'reasons': list(qualification.reasons),
            'qualified_sources': [list(key) for key in qualification.qualified_sources]})
    return result
