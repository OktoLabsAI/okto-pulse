import type {
  CanonicalAnalyticsRecord,
  FlowHealthResponse,
} from './analyticsCanonicalTypes';

export interface FlowHealthMetrics {
  blockerOccurrences: number | null;
  blockerSubjects: number | null;
  rejectedWip: number | null;
  rejectedP95Hours: number | null;
  recoveryRate: number | null;
  recoverySample: number | null;
  dependencyWaitP50Hours: number | null;
  dependencyDepth: number | null;
  openBugs: number | null;
  highSeverityBugs: number | null;
}

export type FlowHealthAuthorityState =
  | 'available'
  | 'empty'
  | 'restricted'
  | 'unavailable'
  | 'inconsistent';

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function firstNumber(
  record: Record<string, unknown> | null | undefined,
  keys: string[],
): number | null {
  if (!record) return null;
  for (const key of keys) {
    const value = finiteNumber(record[key]);
    if (value !== null) return value;
  }
  return null;
}

function percentile(values: number[], percentileRank: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(
    sorted.length - 1,
    Math.max(0, Math.ceil(percentileRank * sorted.length) - 1),
  );
  return sorted[index];
}

function recordState(record: CanonicalAnalyticsRecord): string {
  return String(record.state ?? record.outcome ?? record.status ?? '').toLowerCase();
}

function isRecovered(record: CanonicalAnalyticsRecord): boolean {
  if (record.completed_at || record.recovered_at || record.resolved_at) return true;
  return ['completed', 'done', 'recovered', 'resolved', 'passed'].includes(recordState(record));
}

function reportRecords(
  data: FlowHealthResponse,
  kind: 'dependency' | 'defect',
): CanonicalAnalyticsRecord[] {
  return data.items.flatMap((item) => {
    const direct = kind === 'dependency' ? item.dependency_report : item.defect_report;
    const nested = item.reports?.[kind];
    return [direct, nested]
      .filter((value): value is CanonicalAnalyticsRecord => Boolean(value) && !Array.isArray(value));
  });
}

function completeRecordNumbers(
  records: CanonicalAnalyticsRecord[],
  keys: string[],
): number[] | null {
  if (records.length === 0) return null;
  const values = records.map((record) => firstNumber(record, keys));
  if (values.some((value) => value === null)) return null;
  return values as number[];
}

export function flowHealthAuthorityState(data: FlowHealthResponse): FlowHealthAuthorityState {
  const summary = data.summary ?? {};
  if ((finiteNumber(summary.inconsistent) ?? 0) > 0) return 'inconsistent';
  if ((finiteNumber(summary.restricted) ?? 0) > 0) return 'restricted';
  if ((finiteNumber(summary.unavailable) ?? 0) > 0) return 'unavailable';
  if (data.items.length === 0) return 'empty';
  return 'available';
}

export function deriveFlowHealthMetrics(data: FlowHealthResponse): FlowHealthMetrics {
  const summary = data.summary ?? {};
  const authorityState = flowHealthAuthorityState(data);
  const canInferRows = authorityState === 'available' || authorityState === 'empty';

  const blockerOccurrences = firstNumber(summary, [
    'blocker_occurrences',
    'active_blocker_occurrences',
    'active_blockers',
  ]) ?? (canInferRows
    ? data.items.reduce((total, item) => total + (item.blockers ?? []).length, 0)
    : null);
  const blockerSubjects = firstNumber(summary, [
    'blocker_entities',
    'unique_blocker_entities',
    'blocked_entities',
  ]) ?? (canInferRows
    ? data.items.filter((item) => (item.blockers ?? []).length > 0).length
    : null);

  const rejectedItems = data.items.filter((item) => item.current_episode?.state === 'rejected');
  const rejectedAgesHours = rejectedItems
    .map((item) => finiteNumber(item.current_episode?.age_seconds))
    .filter((value): value is number => value !== null)
    .map((seconds) => seconds / 3600);
  const rejectedWip = firstNumber(summary, ['rejected_wip', 'rejected_active'])
    ?? (canInferRows ? rejectedItems.length : null);
  const rejectedP95Hours = firstNumber(summary, [
    'rejected_age_p95_hours',
    'rejected_wip_p95_hours',
  ]) ?? (canInferRows ? percentile(rejectedAgesHours, 0.95) : null);

  const rework = data.items.flatMap((item) => item.rework ?? []);
  const recovered = rework.filter(isRecovered).length;
  const recoverySample = firstNumber(summary, ['recovery_n', 'recovery_sample', 'rework_episodes'])
    ?? (canInferRows ? rework.length : null);
  const recoveryRate = firstNumber(summary, ['recovery_rate', 'rework_recovery_rate'])
    ?? (canInferRows && rework.length > 0 ? recovered / rework.length : null);

  const dependencies = reportRecords(data, 'dependency');
  const dependencyWaitValues = completeRecordNumbers(dependencies, [
    'wait_p50_hours',
    'p50_hours',
    'wait_hours',
  ]);
  const dependencyWaitP50Hours = firstNumber(summary, [
    'dependency_wait_p50_hours',
    'dependency_p50_hours',
  ]) ?? (canInferRows && dependencyWaitValues
    ? percentile(dependencyWaitValues, 0.5)
    : null);
  const dependencyDepthValues = completeRecordNumbers(dependencies, [
    'max_depth',
    'depth',
    'longest_chain',
  ]);
  const dependencyDepth = firstNumber(summary, [
    'dependency_depth',
    'dependency_max_depth',
    'longest_dependency_chain',
  ]) ?? (canInferRows && dependencyDepthValues
    ? Math.max(...dependencyDepthValues)
    : null);

  const defects = reportRecords(data, 'defect');
  const openBugValues = completeRecordNumbers(defects, ['open_bugs', 'open_count', 'open']);
  const openBugs = firstNumber(summary, ['open_bugs', 'open_defects'])
    ?? (canInferRows && openBugValues
      ? openBugValues.reduce((total, value) => total + value, 0)
      : null);
  const highSeverityBugValues = completeRecordNumbers(defects, [
    'high_severity',
    'high_count',
    'critical_and_high',
  ]);
  const highSeverityBugs = firstNumber(summary, ['high_severity_bugs', 'high_severity_defects'])
    ?? (canInferRows && highSeverityBugValues
      ? highSeverityBugValues.reduce((total, value) => total + value, 0)
      : null);

  return {
    blockerOccurrences,
    blockerSubjects,
    rejectedWip,
    rejectedP95Hours,
    recoveryRate,
    recoverySample,
    dependencyWaitP50Hours,
    dependencyDepth,
    openBugs,
    highSeverityBugs,
  };
}

export function formatMetric(value: number | null, suffix = ''): string {
  if (value === null) return 'N/A';
  return `${Number.isInteger(value) ? value : value.toFixed(1)}${suffix}`;
}
