import type {
  GuidelineMetric,
  GuidelineMetricDirection,
  GuidelineMetricInput,
  PolicyEntityType,
} from '@/types/policy-governance';

import { createGuidelineClientId } from './guidelineEditorShared';

export const SEMANTIC_SCORE_MIN = 0;
export const SEMANTIC_SCORE_MAX = 100;
export const DEFAULT_CONFIDENCE_THRESHOLD = 70;
export const DEFAULT_CUSTOM_METRIC_THRESHOLD = 70;
export const METRIC_CODE_PATTERN = /^[A-Za-z][A-Za-z0-9_.:-]*$/;
export const METRIC_CODE_MAX_LENGTH = 128;

export function isValidCustomMetricCode(code: string): boolean {
  const trimmed = code.trim();
  return (
    trimmed.length > 0
    && trimmed.length <= METRIC_CODE_MAX_LENGTH
    && METRIC_CODE_PATTERN.test(trimmed)
    && trimmed.toLowerCase() !== 'confidence'
  );
}

export interface SemanticMetricDraft {
  localId: string;
  metricId: string;
  code: string;
  originalCode: string | null;
  title: string;
  description: string;
  evaluationRubric: string;
  targetEntityTypes: PolicyEntityType[];
  direction: GuidelineMetricDirection;
  defaultThreshold: string;
}

export interface SystemConfidenceMetric {
  metricId: 'confidence';
  title: 'Confidence';
  description: string;
  evaluationRubric: string;
  direction: 'minimum';
  defaultThreshold: number;
}

export const SYSTEM_CONFIDENCE_METRIC: SystemConfidenceMetric = {
  metricId: 'confidence',
  title: 'Confidence',
  description:
    'How certain the evaluator is that the assessment is supported by the available context and evidence.',
  evaluationRubric:
    'Score 0 when evidence is absent or contradictory, 50 when the conclusion is plausible but incomplete, and 100 when the conclusion is directly supported and independently traceable.',
  direction: 'minimum',
  defaultThreshold: DEFAULT_CONFIDENCE_THRESHOLD,
};

export function suggestMetricCode(title: string): string {
  const normalized = title
    .trim()
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .replace(/_+/g, '_');
  if (!normalized) return '';
  return /^[a-z]/.test(normalized)
    ? normalized.slice(0, METRIC_CODE_MAX_LENGTH)
    : `metric_${normalized}`.slice(0, METRIC_CODE_MAX_LENGTH);
}

export function newSemanticMetricDraft(): SemanticMetricDraft {
  return {
    localId: createGuidelineClientId('semantic-metric-draft'),
    metricId: createGuidelineClientId('metric'),
    code: '',
    originalCode: null,
    title: '',
    description: '',
    evaluationRubric: '',
    targetEntityTypes: [],
    direction: 'minimum',
    defaultThreshold: String(DEFAULT_CUSTOM_METRIC_THRESHOLD),
  };
}

export function semanticMetricToDraft(
  metric: GuidelineMetric,
): SemanticMetricDraft {
  return {
    localId: metric.metric_id,
    metricId: metric.metric_id,
    code: metric.code,
    originalCode: metric.code,
    title: metric.title,
    description: metric.description,
    evaluationRubric: metric.evaluation_rubric,
    targetEntityTypes: [...metric.target_entity_types],
    direction: metric.direction,
    defaultThreshold: String(metric.default_threshold),
  };
}

export function semanticMetricDraftToInput(
  draft: SemanticMetricDraft,
): GuidelineMetricInput {
  const error = validateSemanticMetricDraft(draft);
  if (error) throw new Error(error);
  const [firstTarget, ...remainingTargets] = draft.targetEntityTypes;
  return {
    metric_id: draft.metricId,
    code: draft.code.trim(),
    title: draft.title.trim(),
    description: draft.description.trim(),
    evaluation_rubric: draft.evaluationRubric.trim(),
    target_entity_types: [firstTarget, ...remainingTargets],
    direction: draft.direction,
    default_threshold: Number(draft.defaultThreshold),
  };
}

export function validateSemanticMetricDraft(
  draft: SemanticMetricDraft,
): string | null {
  if (!draft.metricId.trim()) return 'Metric identity is unavailable.';
  if (!draft.title.trim()) return 'Metric title is required.';
  if (!draft.code.trim()) return 'Metric key could not be generated.';
  if (
    draft.code.trim().length > METRIC_CODE_MAX_LENGTH
    || !METRIC_CODE_PATTERN.test(draft.code.trim())
  ) {
    return 'Metric key must be at most 128 characters, start with a letter, and contain only letters, numbers, underscores, periods, colons, or hyphens.';
  }
  if (draft.code.trim().toLowerCase() === 'confidence') {
    return 'Confidence is system-owned and cannot be created as a custom metric.';
  }
  if (!draft.description.trim()) return 'Metric description is required.';
  if (!draft.evaluationRubric.trim()) {
    return 'Evaluation rubric is required.';
  }
  if (draft.targetEntityTypes.length === 0) {
    return 'Select at least one entity type for this metric.';
  }
  if (new Set(draft.targetEntityTypes).size !== draft.targetEntityTypes.length) {
    return 'Metric entity types must be unique.';
  }
  if (draft.direction !== 'minimum' && draft.direction !== 'maximum') {
    return 'Choose whether higher or lower scores are better.';
  }
  if (!/^\d+$/.test(draft.defaultThreshold)) {
    return 'Default threshold must be a whole number from 0 to 100.';
  }
  const threshold = Number(draft.defaultThreshold);
  if (
    !Number.isInteger(threshold)
    || threshold < SEMANTIC_SCORE_MIN
    || threshold > SEMANTIC_SCORE_MAX
  ) {
    return 'Default threshold must be a whole number from 0 to 100.';
  }
  return null;
}

export function validateSemanticMetricDrafts(
  drafts: readonly SemanticMetricDraft[],
): string | null {
  for (let index = 0; index < drafts.length; index += 1) {
    const error = validateSemanticMetricDraft(drafts[index]);
    if (error) return `Metric ${index + 1}: ${error}`;
  }
  const metricIds = drafts.map((draft) => draft.metricId);
  if (new Set(metricIds).size !== metricIds.length) {
    return 'Metric identities must be unique.';
  }
  const codes = drafts.map((draft) => draft.code.trim().toLowerCase());
  if (new Set(codes).size !== codes.length) {
    return 'Metric keys must be unique within a revision.';
  }
  return null;
}

export function canonicalSemanticMetrics(
  metrics: readonly GuidelineMetricInput[],
): GuidelineMetricInput[] {
  return metrics.map((metric) => ({
    ...metric,
    target_entity_types: [...metric.target_entity_types].sort() as
      GuidelineMetricInput['target_entity_types'],
  }));
}
