import type {
  BoardSettings,
  TaskValidationGateOverride,
} from '@/types';

export type TaskValidationThresholdSource =
  | 'sprint'
  | 'spec'
  | 'board'
  | 'default';

export interface ResolvedTaskValidationThresholds {
  min_confidence: number;
  min_completeness: number;
  max_drift: number;
  resolved_sources: {
    min_confidence: TaskValidationThresholdSource;
    min_completeness: TaskValidationThresholdSource;
    max_drift: TaskValidationThresholdSource;
  };
}

type BoardTaskValidationThresholds = Pick<
  BoardSettings,
  'min_confidence' | 'min_completeness' | 'max_drift'
>;

function resolveThreshold(
  sprintValue: number | null | undefined,
  specValue: number | null | undefined,
  boardValue: number | null | undefined,
  defaultValue: number,
): [number, TaskValidationThresholdSource] {
  if (sprintValue != null) return [sprintValue, 'sprint'];
  if (specValue != null) return [specValue, 'spec'];
  if (boardValue != null) return [boardValue, 'board'];
  return [defaultValue, 'default'];
}

/** Mirrors the backend's independent sprint -> spec -> board -> default lookup. */
export function resolveTaskValidationThresholds({
  boardSettings,
  spec,
  sprint,
}: {
  boardSettings?: BoardTaskValidationThresholds | null;
  spec?: TaskValidationGateOverride | null;
  sprint?: TaskValidationGateOverride | null;
}): ResolvedTaskValidationThresholds {
  const [minConfidence, minConfidenceSource] = resolveThreshold(
    sprint?.validation_min_confidence,
    spec?.validation_min_confidence,
    boardSettings?.min_confidence,
    70,
  );
  const [minCompleteness, minCompletenessSource] = resolveThreshold(
    sprint?.validation_min_completeness,
    spec?.validation_min_completeness,
    boardSettings?.min_completeness,
    80,
  );
  const [maxDrift, maxDriftSource] = resolveThreshold(
    sprint?.validation_max_drift,
    spec?.validation_max_drift,
    boardSettings?.max_drift,
    50,
  );

  return {
    min_confidence: minConfidence,
    min_completeness: minCompleteness,
    max_drift: maxDrift,
    resolved_sources: {
      min_confidence: minConfidenceSource,
      min_completeness: minCompletenessSource,
      max_drift: maxDriftSource,
    },
  };
}
