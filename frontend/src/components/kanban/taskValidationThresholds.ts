import type { TaskValidationConfig } from '@/types';

export type TaskValidationThresholdSource = TaskValidationConfig['resolved_from'];
type ThresholdField = 'min_confidence' | 'min_completeness' | 'max_drift';
export type ResolvedTaskValidationThresholds = Pick<TaskValidationConfig, ThresholdField> & {
  resolved_sources: Pick<TaskValidationConfig['resolved_sources'], ThresholdField>;
};
