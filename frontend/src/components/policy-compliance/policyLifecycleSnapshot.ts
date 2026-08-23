import type {
  PolicyComplianceLifecycleBinding,
  PolicyComplianceLifecycleBindingStatus,
  PolicyComplianceLifecycleCounts,
  PolicyComplianceLifecycleDetails,
  PolicyComplianceLifecycleMetric,
  ValidationCycleCheckSummary,
} from '@/types';

const MAX_BINDINGS = 100;
const MAX_METRICS_PER_BINDING = 100;

const BINDING_STATUSES = new Set<PolicyComplianceLifecycleBindingStatus>([
  'passed',
  'failed',
  'waived',
  'skipped',
  'pending',
  'inconsistent',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function isBoundedScore(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0 && Number(value) <= 100;
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function parseCounts(value: unknown): PolicyComplianceLifecycleCounts | null {
  if (!isRecord(value)) return null;
  const keys = [
    'applicable',
    'completed',
    'passed',
    'failed',
    'waived',
    'skipped',
    'pending',
    'context_only',
    'inconsistent',
    'scope_inconsistent',
    'blocking',
    'advisory',
    'blocking_failed',
    'blocking_pending',
    'advisory_failed',
    'advisory_pending',
    'failed_metrics',
    'waived_metrics',
    'unwaived_failed_metrics',
  ] as const;
  if (!keys.every((key) => isNonNegativeInteger(value[key]))) return null;
  return Object.fromEntries(keys.map((key) => [key, value[key]])) as unknown as PolicyComplianceLifecycleCounts;
}

function parseMetric(value: unknown): PolicyComplianceLifecycleMetric | null {
  if (!isRecord(value)) return null;
  if (
    !isNonEmptyString(value.metric_id)
    || !isNonEmptyString(value.code)
    || !isNonEmptyString(value.title)
    || typeof value.description !== 'string'
    || typeof value.description_truncated !== 'boolean'
    || typeof value.evaluation_rubric !== 'string'
    || typeof value.evaluation_rubric_truncated !== 'boolean'
    || (
      value.assessment_outcome !== 'passed'
      && value.assessment_outcome !== 'failed'
      && value.assessment_outcome !== 'waived'
      && value.assessment_outcome !== 'pending'
    )
    || (value.direction !== 'minimum' && value.direction !== 'maximum')
    || !isBoundedScore(value.default_threshold)
    || !isBoundedScore(value.effective_threshold)
    || (value.threshold_source !== 'default' && value.threshold_source !== 'override')
  ) {
    return null;
  }
  return {
    metric_id: value.metric_id,
    code: value.code,
    title: value.title,
    description: value.description,
    description_truncated: value.description_truncated,
    evaluation_rubric: value.evaluation_rubric,
    evaluation_rubric_truncated: value.evaluation_rubric_truncated,
    assessment_outcome: value.assessment_outcome,
    direction: value.direction,
    default_threshold: value.default_threshold,
    effective_threshold: value.effective_threshold,
    threshold_source: value.threshold_source,
  };
}

function parseBinding(value: unknown): PolicyComplianceLifecycleBinding | null {
  if (!isRecord(value)) return null;
  if (
    !isNonEmptyString(value.binding_id)
    || !isNonEmptyString(value.guideline_id)
    || !isNonEmptyString(value.revision_id)
    || !isNonEmptyString(value.title)
    || (value.enforcement !== 'advisory' && value.enforcement !== 'blocking')
    || !isBoundedScore(value.minimum_confidence)
    || !isNonEmptyString(value.status)
    || !BINDING_STATUSES.has(value.status as PolicyComplianceLifecycleBindingStatus)
    || !isNonNegativeInteger(value.failed_metric_count)
    || !isNonNegativeInteger(value.waived_metric_count)
    || !isNonNegativeInteger(value.unwaived_failed_metric_count)
    || !Array.isArray(value.metrics)
    || value.metrics.length === 0
    || value.metrics.length > MAX_METRICS_PER_BINDING
  ) {
    return null;
  }
  const metrics = value.metrics.map(parseMetric);
  if (metrics.some((metric) => metric === null)) return null;
  const metricIds = new Set(metrics.map((metric) => metric!.metric_id));
  const metricCodes = new Set(metrics.map((metric) => metric!.code));
  if (metricIds.size !== metrics.length || metricCodes.size !== metrics.length) {
    return null;
  }
  const failedOutcomes = metrics.filter(
    (metric) => metric?.assessment_outcome === 'failed',
  ).length;
  const waivedOutcomes = metrics.filter(
    (metric) => metric?.assessment_outcome === 'waived',
  ).length;
  const passedOutcomes = metrics.filter(
    (metric) => metric?.assessment_outcome === 'passed',
  ).length;
  const pendingOutcomes = metrics.filter(
    (metric) => metric?.assessment_outcome === 'pending',
  ).length;
  if (
    value.failed_metric_count
      !== value.waived_metric_count + value.unwaived_failed_metric_count
    || value.failed_metric_count > metrics.length
    || value.waived_metric_count !== waivedOutcomes
    || value.unwaived_failed_metric_count !== failedOutcomes
    || (
      value.status === 'passed'
      && (
        passedOutcomes !== metrics.length
        || value.failed_metric_count !== 0
      )
    )
    || (
      value.status === 'waived'
      && (
        value.waived_metric_count === 0
        || value.unwaived_failed_metric_count !== 0
        || passedOutcomes + waivedOutcomes !== metrics.length
      )
    )
    || (
      value.status === 'failed'
      && (
        value.unwaived_failed_metric_count === 0
        || passedOutcomes + waivedOutcomes + failedOutcomes !== metrics.length
      )
    )
    || (
      (
        value.status === 'pending'
        || value.status === 'skipped'
        || value.status === 'inconsistent'
      )
      && (
        pendingOutcomes !== metrics.length
        || value.failed_metric_count !== 0
      )
    )
  ) {
    return null;
  }
  return {
    binding_id: value.binding_id,
    guideline_id: value.guideline_id,
    revision_id: value.revision_id,
    title: value.title,
    enforcement: value.enforcement,
    minimum_confidence: value.minimum_confidence,
    status: value.status as PolicyComplianceLifecycleBindingStatus,
    failed_metric_count: value.failed_metric_count,
    waived_metric_count: value.waived_metric_count,
    unwaived_failed_metric_count: value.unwaived_failed_metric_count,
    metrics: metrics as PolicyComplianceLifecycleMetric[],
  };
}

/**
 * Parses the bounded human projection emitted from an immutable validation
 * scope. A malformed or missing snapshot fails closed; callers must never
 * substitute the board's current guideline configuration.
 */
export function parsePolicyComplianceLifecycleDetails(
  check: ValidationCycleCheckSummary | undefined,
): PolicyComplianceLifecycleDetails | null {
  if (check?.result_type !== 'policy_compliance' || !isRecord(check.details)) {
    return null;
  }
  const counts = parseCounts(check.details.counts);
  const rawBindings = check.details.applicable_bindings;
  if (
    !counts
    || !Array.isArray(rawBindings)
    || rawBindings.length > MAX_BINDINGS
  ) {
    return null;
  }
  const bindings = rawBindings.map(parseBinding);
  if (bindings.some((binding) => binding === null)) return null;
  const applicableBindings = bindings as PolicyComplianceLifecycleBinding[];
  if (
    new Set(applicableBindings.map((binding) => binding.binding_id)).size
      !== applicableBindings.length
    || counts.applicable !== applicableBindings.length
    || counts.completed
      !== counts.passed + counts.failed + counts.waived + counts.skipped
    || counts.applicable !== counts.completed + counts.pending + counts.inconsistent
    || counts.applicable !== counts.blocking + counts.advisory
    || counts.failed !== counts.blocking_failed + counts.advisory_failed
    || counts.pending !== counts.blocking_pending + counts.advisory_pending
    || counts.failed_metrics
      !== counts.waived_metrics + counts.unwaived_failed_metrics
  ) {
    return null;
  }
  const statuses = applicableBindings.reduce<Record<PolicyComplianceLifecycleBindingStatus, number>>(
    (result, binding) => ({
      ...result,
      [binding.status]: result[binding.status] + 1,
    }),
    {
      passed: 0,
      failed: 0,
      waived: 0,
      skipped: 0,
      pending: 0,
      inconsistent: 0,
    },
  );
  if (
    statuses.passed !== counts.passed
    || statuses.failed !== counts.failed
    || statuses.waived !== counts.waived
    || statuses.skipped !== counts.skipped
    || statuses.pending !== counts.pending
    || statuses.inconsistent !== counts.inconsistent
    || applicableBindings.filter((binding) => binding.enforcement === 'blocking').length
      !== counts.blocking
    || applicableBindings.filter((binding) => binding.enforcement === 'advisory').length
      !== counts.advisory
    || applicableBindings.filter(
      (binding) => binding.enforcement === 'blocking' && binding.status === 'failed',
    ).length !== counts.blocking_failed
    || applicableBindings.filter(
      (binding) => binding.enforcement === 'advisory' && binding.status === 'failed',
    ).length !== counts.advisory_failed
    || applicableBindings.filter(
      (binding) => binding.enforcement === 'blocking' && binding.status === 'pending',
    ).length !== counts.blocking_pending
    || applicableBindings.filter(
      (binding) => binding.enforcement === 'advisory' && binding.status === 'pending',
    ).length !== counts.advisory_pending
    || applicableBindings.reduce(
      (total, binding) => total + binding.failed_metric_count,
      0,
    ) !== counts.failed_metrics
    || applicableBindings.reduce(
      (total, binding) => total + binding.waived_metric_count,
      0,
    ) !== counts.waived_metrics
    || applicableBindings.reduce(
      (total, binding) => total + binding.unwaived_failed_metric_count,
      0,
    ) !== counts.unwaived_failed_metrics
  ) {
    return null;
  }
  return { counts, applicable_bindings: applicableBindings };
}
