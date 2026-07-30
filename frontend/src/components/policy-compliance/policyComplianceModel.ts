import {
  PolicyGovernanceApiError,
} from '@/services/policy-governance-api';
import type {
  PolicyComplianceFindingDetail,
  PolicyComplianceFindingListItem,
  PolicyComplianceReceiptListItem,
  PolicyComplianceReceiptSummary,
  PolicyComplianceReasonCode,
  PolicyComplianceState,
  PolicyCurrentness,
  PolicyCurrentnessReason,
  PolicyEntityType,
  PolicyEvaluationOutcome,
  GuidelineEnforcement,
} from '@/types/policy-governance';
import type { CursorCollectionError } from '@/hooks/useOpaqueCursorCollection';

const COMPLIANCE_STATES = new Set<PolicyComplianceState>([
  'ready',
  'blocked',
  'ready_with_waivers',
  'not_applicable',
]);
const CURRENTNESS_STATES = new Set<PolicyCurrentness>([
  'current',
  'stale',
]);
const EVALUATION_OUTCOMES = new Set<PolicyEvaluationOutcome>([
  'pass',
  'fail',
  'not_applicable',
  'error',
]);
const ENFORCEMENT_STATES = new Set<GuidelineEnforcement>([
  'advisory',
  'blocking',
]);
const CURRENTNESS_REASONS = new Set<PolicyCurrentnessReason>([
  'current_snapshot_missing',
  'subject_version_changed',
  'subject_content_changed',
  'input_digest_changed',
  'policy_set_changed',
  'catalog_version_changed',
  'ruleset_version_changed',
  'binding_head_changed',
]);
const COMPLIANCE_REASON_CODES = new Set<PolicyComplianceReasonCode>([
  'no_applicable_rules',
  'policy_evaluation_unavailable',
  'policy_evaluation_degraded',
]);
const SUMMARY_FORBIDDEN_FIELDS = [
  'subject_content_digest',
  'input_digest',
  'policy_set_digest',
  'binding_head_digest',
  'catalog_version',
  'ruleset_version',
  'adopted_revisions',
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isUniqueClosedStringArray<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
): value is T[] {
  if (
    !Array.isArray(value)
    || value.some((item) =>
      typeof item !== 'string' || !allowed.has(item as T),
    )
  ) {
    return false;
  }
  return new Set(value).size === value.length;
}

function subjectMatches(
  value: unknown,
  expected: {
    boardId: string;
    entityType: PolicyEntityType;
    subjectId: string;
  },
): boolean {
  if (!isRecord(value)) return false;
  return (
    value.board_id === expected.boardId
    && value.entity_type === expected.entityType
    && value.subject_id === expected.subjectId
    && Number.isInteger(value.subject_version)
    && Number(value.subject_version) > 0
  );
}

export function isPolicyComplianceReceiptSummaryForSubject(
  value: PolicyComplianceReceiptListItem | unknown,
  expected: {
    boardId: string;
    entityType: PolicyEntityType;
    subjectId: string;
  },
): value is PolicyComplianceReceiptSummary {
  if (!isRecord(value) || value.projection !== 'summary') return false;
  const currentnessReasons = value.currentness_reasons;
  const currentnessReasonsValid = isUniqueClosedStringArray(
    currentnessReasons,
    CURRENTNESS_REASONS,
  );
  const reasonCodesValid = isUniqueClosedStringArray(
    value.reason_codes,
    COMPLIANCE_REASON_CODES,
  );
  const countsValid =
    isNonNegativeInteger(value.finding_count)
    && isNonNegativeInteger(value.rule_count)
    && isNonNegativeInteger(value.failed_rule_count)
    && isNonNegativeInteger(value.error_rule_count)
    && isNonNegativeInteger(value.blocking_finding_count)
    && isNonNegativeInteger(value.waived_finding_count)
    && Number(value.blocking_finding_count) <= Number(value.finding_count)
    && Number(value.waived_finding_count) <= Number(value.finding_count)
    && Number(value.failed_rule_count) + Number(value.error_rule_count)
      <= Number(value.rule_count);
  const slimSummary = SUMMARY_FORBIDDEN_FIELDS.every(
    (field) => !Object.prototype.hasOwnProperty.call(value, field),
  );
  const currentnessConsistent =
    value.currentness === 'current'
      ? currentnessReasonsValid && currentnessReasons.length === 0
      : value.currentness === 'stale'
        && currentnessReasonsValid
        && currentnessReasons.length > 0;
  return (
    isNonEmptyString(value.receipt_id)
    && subjectMatches(value.subject, expected)
    && COMPLIANCE_STATES.has(value.state as PolicyComplianceState)
    && EVALUATION_OUTCOMES.has(value.outcome as PolicyEvaluationOutcome)
    && CURRENTNESS_STATES.has(value.currentness as PolicyCurrentness)
    && currentnessConsistent
    && reasonCodesValid
    && isNonEmptyString(value.evaluator_version)
    && isNonEmptyString(value.evaluated_by)
    && isNonEmptyString(value.evaluated_at)
    && countsValid
    && slimSummary
  );
}

export function isPolicyComplianceFindingDetailForReceipt(
  value: PolicyComplianceFindingListItem | unknown,
  expected: {
    boardId: string;
    entityType: PolicyEntityType;
    subjectId: string;
    receiptId: string;
  },
): value is PolicyComplianceFindingDetail {
  if (!isRecord(value) || value.projection !== 'detail') return false;
  return (
    isNonEmptyString(value.finding_id)
    && value.receipt_id === expected.receiptId
    && subjectMatches(value.subject, expected)
    && isNonEmptyString(value.guideline_id)
    && isNonEmptyString(value.revision_id)
    && isNonEmptyString(value.rule_id)
    && EVALUATION_OUTCOMES.has(value.outcome as PolicyEvaluationOutcome)
    && ENFORCEMENT_STATES.has(value.enforcement as GuidelineEnforcement)
    && isNonNegativeInteger(value.severity_rank)
    && typeof value.blocking === 'boolean'
    && isNonEmptyString(value.created_at)
    && typeof value.message === 'string'
    && Array.isArray(value.evidence_refs)
    && value.evidence_refs.every(isNonEmptyString)
    && (
      value.waiver_id === undefined
      || isNonEmptyString(value.waiver_id)
    )
  );
}

export function formatPolicyToken(value: string): string {
  return value
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

export function formatPolicyTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export function createPolicyUiId(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `${prefix}-${uuid}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function policyUiErrorMessage(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) {
    return error.nextAction
      ? `${error.message} Next: ${error.nextAction}.`
      : error.message;
  }
  return error instanceof Error
    ? error.message
    : 'Unexpected policy error.';
}

export function classifyPolicyCursorError(
  error: unknown,
): CursorCollectionError {
  if (
    error instanceof PolicyGovernanceApiError
    && error.kind === 'invalid_cursor'
  ) {
    return {
      message: 'This cursor expired or no longer matches the active filters.',
      restartRequired: true,
    };
  }
  return {
    message: policyUiErrorMessage(error),
    restartRequired: false,
  };
}

export interface PolicyComplianceStatePresentation {
  label: string;
  headline: string;
  description: string;
  tone: 'success' | 'danger' | 'warning' | 'neutral';
}

export const POLICY_COMPLIANCE_STATE_PRESENTATION: Record<
  PolicyComplianceState,
  PolicyComplianceStatePresentation
> = {
  ready: {
    label: 'Ready',
    headline: 'Policy requirements are ready',
    description: 'No open blocking policy finding prevents the governed transition.',
    tone: 'success',
  },
  blocked: {
    label: 'Blocked',
    headline: 'Policy requirements are blocking',
    description: 'At least one open blocking policy finding requires remediation or a governed waiver.',
    tone: 'danger',
  },
  ready_with_waivers: {
    label: 'Ready with waivers',
    headline: 'Policy requirements are ready with waivers',
    description: 'Blocking findings are covered by currently effective governed waivers.',
    tone: 'warning',
  },
  not_applicable: {
    label: 'Not applicable',
    headline: 'No adopted rule applies',
    description: 'The current adopted policy set has no executable rule for this subject.',
    tone: 'neutral',
  },
};
