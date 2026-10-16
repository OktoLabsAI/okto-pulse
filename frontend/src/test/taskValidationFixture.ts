import type { ValidationEntry } from '@/types';

/** Complete native review; individual tests override only their scenario. */
export function taskValidationFixture(overrides: Partial<ValidationEntry> = {}): ValidationEntry {
  return {
    id: 'validation-1', card_id: 'card-1', board_id: 'board-1',
    reviewer_id: 'reviewer', reviewer_name: 'Reviewer',
    confidence: 90, confidence_justification: 'Evidence was reviewed.',
    estimated_completeness: 95, completeness_justification: 'Scope was reviewed.',
    estimated_drift: 0, drift_justification: 'No scope drift was found.',
    general_justification: 'The implementation meets the acceptance criteria.',
    recommendation: 'approve', outcome: 'success', validation_outcome: 'success',
    completion_outcome: 'completed', card_status: 'done',
    threshold_violations: [], completion_gate_failures: [],
    resolved_thresholds: { min_confidence: 70, min_completeness: 80, max_drift: 50 },
    reviewer_separation: { mode: 'enforce', allowed: true, warning: false, conflicts: [], source: 'board_settings' },
    expected_subject_version: 1, subject_version: 2,
    created_at: '2026-10-02T12:00:00Z',
    ...overrides,
  };
}
