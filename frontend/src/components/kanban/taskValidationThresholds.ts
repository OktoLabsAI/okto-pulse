import type {
  BoardSettings,
  TaskValidationGateOverride,
  MigratedTaskValidationPolicy,
} from '@/types';

export type TaskValidationThresholdSource =
  | 'card_compatibility'
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
  migratedValue: number | null | undefined,
  sprintValue: number | null | undefined,
  specValue: number | null | undefined,
  boardValue: number | null | undefined,
  defaultValue: number,
): [number, TaskValidationThresholdSource] {
  if (migratedValue != null) return [migratedValue, 'card_compatibility'];
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
  migratedPolicy,
}: {
  boardSettings?: BoardTaskValidationThresholds | null;
  spec?: TaskValidationGateOverride | null;
  sprint?: TaskValidationGateOverride | null;
  migratedPolicy?: MigratedTaskValidationPolicy | null;
}): ResolvedTaskValidationThresholds {
  const [minConfidence, minConfidenceSource] = resolveThreshold(
    migratedPolicy?.overrides.min_confidence,
    sprint?.validation_min_confidence,
    spec?.validation_min_confidence,
    boardSettings?.min_confidence,
    70,
  );
  const [minCompleteness, minCompletenessSource] = resolveThreshold(
    migratedPolicy?.overrides.min_completeness,
    sprint?.validation_min_completeness,
    spec?.validation_min_completeness,
    boardSettings?.min_completeness,
    80,
  );
  const [maxDrift, maxDriftSource] = resolveThreshold(
    migratedPolicy?.overrides.max_drift,
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
