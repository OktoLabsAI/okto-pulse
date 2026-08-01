import { AuthenticatedFetchError } from '@/lib/authFetch';
import type {
  AllowedTransition,
  AllowedTransitionEntityType,
  AllowedTransitionsResponse,
  PolicyComplianceBindingDecision,
  PolicyComplianceTransitionDecision,
  PolicyCurrentness,
  PolicyTransitionDiagnosticCode,
  PolicyTransitionReasonCode,
  SemanticAssessmentCurrentnessReason,
  SemanticAssessmentInadmissibilityCause,
} from '@/types';

export type PolicyTransitionPreviewLoadState =
  | {
      status: 'loading';
      transitions: [];
      error: null;
    }
  | {
      status: 'ready';
      transitions: AllowedTransition[];
      error: null;
    }
  | {
      status: 'error';
      transitions: [];
      error: string;
    };

export interface GovernedPolicyTransition {
  toStatus: string;
  label: string;
  gate: string;
  blockedReason: string | null;
  decision: PolicyComplianceTransitionDecision;
}

export interface UngovernedPolicyTransition {
  toStatus: string;
  label: string;
  gate: string;
}

export interface PolicyTransitionProjection {
  governed: GovernedPolicyTransition[];
  ungoverned: UngovernedPolicyTransition[];
}

export interface PolicyTransitionEnvelopeExpectation {
  boardId: string;
  entityType: AllowedTransitionEntityType;
  subjectId: string;
  currentStatus: string;
}

export interface PolicyTransitionRejectionExpectation
  extends PolicyTransitionEnvelopeExpectation {
  toStatus: string;
}

export interface PolicyTransitionRejection {
  code: PolicyTransitionReasonCode;
  message: string;
  entityType: AllowedTransitionEntityType;
  subjectId: string;
  fromStatus: string;
  toStatus: string;
  decision: PolicyComplianceTransitionDecision;
}

const TRANSITION_REASON_ORDER = [
  'transition_not_allowed',
  'policy_compliance_not_required',
  'policy_subject_required',
  'policy_compliance_receipt_missing',
  'policy_compliance_receipt_stale',
  'policy_compliance_blocked',
  'policy_assessment_unavailable',
  'policy_compliance_ready',
  'policy_compliance_ready_with_waivers',
  'policy_compliance_not_applicable',
  'policy_compliance_advisory_only',
] as const satisfies readonly PolicyTransitionReasonCode[];

const TRANSITION_REASON_CODES =
  new Set<PolicyTransitionReasonCode>(TRANSITION_REASON_ORDER);

const DIAGNOSTIC_ORDER = [
  'policy_compliance_receipt_missing',
  'policy_compliance_receipt_stale',
  'policy_assessment_unavailable',
  'policy_assessment_inadmissible',
  'policy_metric_threshold_failed',
] as const satisfies readonly PolicyTransitionDiagnosticCode[];

const CURRENTNESS_REASON_ORDER = [
  'current_snapshot_missing',
  'subject_version_changed',
  'subject_content_changed',
  'guideline_revision_changed',
  'guideline_revision_digest_changed',
  'binding_revision_changed',
  'binding_configuration_changed',
  'policy_set_changed',
  'binding_head_changed',
  'input_digest_changed',
] as const satisfies readonly SemanticAssessmentCurrentnessReason[];

const INADMISSIBILITY_CAUSES =
  new Set<SemanticAssessmentInadmissibilityCause>([
    'confidence_below_minimum',
    'assessor_separation_required',
  ]);

const REJECTED_STATES = new Set<PolicyTransitionReasonCode>([
  'transition_not_allowed',
  'policy_subject_required',
  'policy_compliance_receipt_missing',
  'policy_compliance_receipt_stale',
  'policy_compliance_blocked',
  'policy_assessment_unavailable',
]);

const DECISION_FIELDS = [
  'state',
  'allowed',
  'policy_compliance_required',
  'reason_codes',
  'decision_digest',
  'fence_digest',
  'receipt_ids',
  'currentness',
  'currentness_reasons',
  'applicable_metric_count',
  'applicable_blocking_metric_count',
  'failed_metric_count',
  'blocking_metric_count',
  'waived_metric_count',
  'advisory_issue_count',
  'skipped_binding_count',
  'diagnostic_codes',
  'binding_decisions',
] as const;

const BINDING_DECISION_FIELDS = [
  'binding_id',
  'guideline_id',
  'enforcement',
  'applicable_metric_count',
  'allowed',
  'assessment_available',
  'receipt_id',
  'currentness',
  'currentness_reasons',
  'inadmissibility_cause',
  'failed_metric_count',
  'waived_metric_count',
  'blocking_metric_count',
  'advisory_issue_count',
  'skipped',
  'diagnostic_codes',
] as const;

const TRANSITION_FIELDS = [
  'to_status',
  'label',
  'gate',
  'blocked_reason',
  'preconditions',
  'capabilities',
  'effects',
  'reason_codes',
  'policy_compliance',
  'policy_compliance_decision',
] as const;

const ENVELOPE_FIELDS = [
  'board_id',
  'entity_type',
  'entity_id',
  'current_status',
  'allowed_transitions',
  'source',
] as const;

const REJECTION_FIELDS = [
  'outcome',
  'error',
  'code',
  'message',
  'reason_codes',
  'decision_digest',
  'fence_digest',
  'receipt_ids',
  'currentness',
  'currentness_reasons',
  'counts',
  'diagnostic_codes',
  'binding_decisions',
  'transition',
  'policy_compliance_required',
] as const;

const REJECTION_COUNT_FIELDS = [
  'applicable_metrics',
  'applicable_blocking_metrics',
  'failed_metrics',
  'blocking_metrics',
  'waived_metrics',
  'advisory_issues',
  'skipped_bindings',
] as const;

const REJECTION_TRANSITION_FIELDS = [
  'entity_type',
  'subject_id',
  'from_status',
  'to_status',
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function exactFields(
  value: Record<string, unknown>,
  fields: readonly string[],
  label: string,
): void {
  const expected = new Set(fields);
  const keys = Object.keys(value);
  if (
    keys.length !== fields.length
    || fields.some((field) => !Object.prototype.hasOwnProperty.call(value, field))
    || keys.some((field) => !expected.has(field))
  ) {
    throw new Error(
      `Policy Compliance gate preview has an unknown or missing ${label} field.`,
    );
  }
}

function requiredText(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
  }
  return value;
}

function nullableText(value: unknown, field: string): string | null {
  if (value === null) return null;
  return requiredText(value, field);
}

function nullableDigest(value: unknown, field: string): string | null {
  if (value === null) return null;
  const digest = requiredText(value, field);
  if (!/^[a-f0-9]{64}$/u.test(digest)) {
    throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
  }
  return digest;
}

function nonNegativeCount(value: unknown, field: string): number {
  if (
    typeof value !== 'number'
    || !Number.isInteger(value)
    || value < 0
  ) {
    throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
  }
  return value;
}

function nullableCount(value: unknown, field: string): number | null {
  return value === null ? null : nonNegativeCount(value, field);
}

function orderedUniqueClosedValues<T extends string>(
  value: unknown,
  order: readonly T[],
  field: string,
): T[] {
  if (!Array.isArray(value)) {
    throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
  }
  const allowed = new Set(order);
  const items: T[] = [];
  for (const item of value) {
    if (typeof item !== 'string' || !allowed.has(item as T)) {
      throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
    }
    if (items.includes(item as T)) {
      throw new Error(`Policy Compliance gate preview repeats ${field}.`);
    }
    items.push(item as T);
  }
  const canonical = order.filter((item) => items.includes(item));
  if (
    canonical.length !== items.length
    || canonical.some((item, index) => item !== items[index])
  ) {
    throw new Error(
      `Policy Compliance gate preview has non-canonical ${field}.`,
    );
  }
  return items;
}

function uniqueTextValues(value: unknown, field: string): string[] {
  if (!Array.isArray(value)) {
    throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
  }
  const items = value.map((item) => requiredText(item, field));
  if (new Set(items).size !== items.length) {
    throw new Error(`Policy Compliance gate preview repeats ${field}.`);
  }
  return items;
}

function parseCurrentness(
  currentnessValue: unknown,
  reasonsValue: unknown,
  fieldPrefix: string,
): {
  currentness: PolicyCurrentness | null;
  reasons: SemanticAssessmentCurrentnessReason[];
} {
  const currentness = currentnessValue === null
    ? null
    : currentnessValue === 'current' || currentnessValue === 'stale'
      ? currentnessValue
      : (() => {
          throw new Error(
            `Policy Compliance gate preview has invalid ${fieldPrefix} currentness.`,
          );
        })();
  const reasons = orderedUniqueClosedValues(
    reasonsValue,
    CURRENTNESS_REASON_ORDER,
    `${fieldPrefix} currentness reasons`,
  );
  if (
    (currentness === null && reasons.length !== 0)
    || (currentness === 'current' && reasons.length !== 0)
    || (currentness === 'stale' && reasons.length === 0)
  ) {
    throw new Error(
      `Policy Compliance gate preview has inconsistent ${fieldPrefix} currentness.`,
    );
  }
  return { currentness, reasons };
}

function sameItems<T>(
  actual: readonly T[],
  expected: readonly T[],
): boolean {
  return (
    actual.length === expected.length
    && actual.every((item, index) => item === expected[index])
  );
}

function orderedUnion<T extends string>(
  order: readonly T[],
  values: Iterable<Iterable<T>>,
): T[] {
  const present = new Set<T>();
  for (const collection of values) {
    for (const item of collection) present.add(item);
  }
  return order.filter((item) => present.has(item));
}

function assertBindingDecision(
  decision: PolicyComplianceBindingDecision,
): void {
  const isBlocking = decision.enforcement === 'blocking';
  if (
    decision.failed_metric_count > decision.applicable_metric_count
    || decision.waived_metric_count > decision.failed_metric_count
    || decision.blocking_metric_count > decision.failed_metric_count
    || decision.advisory_issue_count > decision.applicable_metric_count
  ) {
    throw new Error(
      'Policy Compliance gate preview has inconsistent binding metric counts.',
    );
  }
  const evidenceGapDiagnostics = new Set([
    'policy_assessment_unavailable',
    'policy_assessment_inadmissible',
    'policy_compliance_receipt_missing',
    'policy_compliance_receipt_stale',
  ]);
  const hasEvidenceGap = decision.diagnostic_codes.some((code) =>
    evidenceGapDiagnostics.has(code)
  );
  if (
    decision.enforcement === 'advisory'
    && (
      decision.blocking_metric_count !== 0
      || (!decision.allowed && !hasEvidenceGap)
    )
  ) {
    // Advisory scores never block; only absent/stale/inadmissible evidence
    // rejects an advisory binding (evidence presence is mandatory at
    // governed transitions regardless of enforcement).
    throw new Error(
      'Policy Compliance gate preview contains a blocking advisory binding.',
    );
  }

  if (decision.applicable_metric_count === 0) {
    if (
      !decision.allowed
      || decision.inadmissibility_cause !== null
      || decision.failed_metric_count !== 0
      || decision.waived_metric_count !== 0
      || decision.blocking_metric_count !== 0
      || decision.advisory_issue_count !== 0
      || decision.skipped
      || decision.diagnostic_codes.length !== 0
    ) {
      throw new Error(
        'Policy Compliance gate preview has an invalid context-only binding.',
      );
    }
    return;
  }

  if (decision.skipped) {
    const expectedDiagnostics: PolicyTransitionDiagnosticCode[] =
      decision.failed_metric_count > 0
        ? ['policy_metric_threshold_failed']
        : [];
    if (
      !decision.allowed
      || decision.waived_metric_count !== 0
      || decision.blocking_metric_count !== 0
      || decision.advisory_issue_count !== 0
      || !sameItems(decision.diagnostic_codes, expectedDiagnostics)
    ) {
      throw new Error(
        'Policy Compliance gate preview has an invalid skipped binding.',
      );
    }
    return;
  }

  const expectedAllowedWithoutEvidence = false;
  const expectedAdvisoryWithoutEvidence = isBlocking
    ? 0
    : decision.applicable_metric_count;
  if (!decision.assessment_available) {
    if (
      decision.receipt_id !== null
      || decision.currentness !== null
      || decision.currentness_reasons.length !== 0
      || decision.inadmissibility_cause !== null
      || decision.failed_metric_count !== 0
      || decision.waived_metric_count !== 0
      || decision.blocking_metric_count !== 0
      || decision.advisory_issue_count
        !== expectedAdvisoryWithoutEvidence
      || decision.allowed !== expectedAllowedWithoutEvidence
      || !sameItems(
        decision.diagnostic_codes,
        ['policy_assessment_unavailable'],
      )
    ) {
      throw new Error(
        'Policy Compliance gate preview has an invalid unavailable assessment binding.',
      );
    }
    return;
  }

  if (decision.inadmissibility_cause !== null) {
    if (
      decision.receipt_id !== null
      || decision.currentness !== null
      || decision.currentness_reasons.length !== 0
      || decision.failed_metric_count !== 0
      || decision.waived_metric_count !== 0
      || decision.blocking_metric_count !== 0
      || decision.advisory_issue_count
        !== expectedAdvisoryWithoutEvidence
      || decision.allowed !== expectedAllowedWithoutEvidence
      || !sameItems(
        decision.diagnostic_codes,
        ['policy_assessment_inadmissible'],
      )
    ) {
      throw new Error(
        'Policy Compliance gate preview has an invalid inadmissible assessment binding.',
      );
    }
    return;
  }

  if (decision.receipt_id === null) {
    if (
      decision.currentness !== null
      || decision.currentness_reasons.length !== 0
      || decision.failed_metric_count !== 0
      || decision.waived_metric_count !== 0
      || decision.blocking_metric_count !== 0
      || decision.advisory_issue_count
        !== expectedAdvisoryWithoutEvidence
      || decision.allowed !== expectedAllowedWithoutEvidence
      || !sameItems(
        decision.diagnostic_codes,
        ['policy_compliance_receipt_missing'],
      )
    ) {
      throw new Error(
        'Policy Compliance gate preview has an invalid missing-receipt binding.',
      );
    }
    return;
  }

  if (decision.currentness === 'stale') {
    if (
      decision.currentness_reasons.length === 0
      || decision.failed_metric_count !== 0
      || decision.waived_metric_count !== 0
      || decision.blocking_metric_count !== 0
      || decision.advisory_issue_count
        !== expectedAdvisoryWithoutEvidence
      || decision.allowed !== expectedAllowedWithoutEvidence
      || !sameItems(
        decision.diagnostic_codes,
        ['policy_compliance_receipt_stale'],
      )
    ) {
      throw new Error(
        'Policy Compliance gate preview has an invalid stale-receipt binding.',
      );
    }
    return;
  }

  if (
    decision.currentness !== 'current'
    || decision.currentness_reasons.length !== 0
  ) {
    throw new Error(
      'Policy Compliance gate preview has an invalid current receipt binding.',
    );
  }
  const unwaived =
    decision.failed_metric_count - decision.waived_metric_count;
  const expectedBlocking = isBlocking ? unwaived : 0;
  const expectedAdvisory = isBlocking ? 0 : unwaived;
  const expectedDiagnostics: PolicyTransitionDiagnosticCode[] =
    decision.failed_metric_count > 0
      ? ['policy_metric_threshold_failed']
      : [];
  if (
    decision.blocking_metric_count !== expectedBlocking
    || decision.advisory_issue_count !== expectedAdvisory
    || decision.allowed !== (!isBlocking || unwaived === 0)
    || !sameItems(decision.diagnostic_codes, expectedDiagnostics)
  ) {
    throw new Error(
      'Policy Compliance gate preview has an invalid current receipt binding.',
    );
  }
}

function parseBindingDecision(
  value: unknown,
): PolicyComplianceBindingDecision {
  if (!isRecord(value)) {
    throw new Error(
      'Policy Compliance gate preview has invalid binding decision.',
    );
  }
  exactFields(value, BINDING_DECISION_FIELDS, 'binding decision');
  if (value.enforcement !== 'advisory' && value.enforcement !== 'blocking') {
    throw new Error(
      'Policy Compliance gate preview has invalid binding enforcement.',
    );
  }
  if (
    typeof value.allowed !== 'boolean'
    || typeof value.assessment_available !== 'boolean'
    || typeof value.skipped !== 'boolean'
  ) {
    throw new Error(
      'Policy Compliance gate preview has invalid binding authority.',
    );
  }
  const currentness = parseCurrentness(
    value.currentness,
    value.currentness_reasons,
    'binding',
  );
  const receiptId = nullableText(value.receipt_id, 'binding receipt identity');
  const inadmissibilityCause = value.inadmissibility_cause === null
    ? null
    : (
        typeof value.inadmissibility_cause === 'string'
        && INADMISSIBILITY_CAUSES.has(
          value.inadmissibility_cause as SemanticAssessmentInadmissibilityCause,
        )
      )
      ? value.inadmissibility_cause as SemanticAssessmentInadmissibilityCause
      : (() => {
          throw new Error(
            'Policy Compliance gate preview has invalid inadmissibility cause.',
          );
        })();
  const applicable = nonNegativeCount(
    value.applicable_metric_count,
    'binding applicable metric count',
  );
  const failed = nonNegativeCount(
    value.failed_metric_count,
    'binding failed metric count',
  );
  const waived = nonNegativeCount(
    value.waived_metric_count,
    'binding waived metric count',
  );
  const blocking = nonNegativeCount(
    value.blocking_metric_count,
    'binding blocking metric count',
  );
  const advisory = nonNegativeCount(
    value.advisory_issue_count,
    'binding advisory issue count',
  );
  const diagnostics = orderedUniqueClosedValues(
    value.diagnostic_codes,
    DIAGNOSTIC_ORDER,
    'binding diagnostic codes',
  );
  const decision: PolicyComplianceBindingDecision = {
    binding_id: requiredText(value.binding_id, 'binding identity'),
    guideline_id: requiredText(value.guideline_id, 'guideline identity'),
    enforcement: value.enforcement,
    applicable_metric_count: applicable,
    allowed: value.allowed,
    assessment_available: value.assessment_available,
    receipt_id: receiptId,
    currentness: currentness.currentness,
    currentness_reasons: currentness.reasons,
    inadmissibility_cause: inadmissibilityCause,
    failed_metric_count: failed,
    waived_metric_count: waived,
    blocking_metric_count: blocking,
    advisory_issue_count: advisory,
    skipped: value.skipped,
    diagnostic_codes: diagnostics,
  };
  assertBindingDecision(decision);
  return decision;
}

function assertPlaceholderDecision(
  decision: PolicyComplianceTransitionDecision,
): void {
  if (
    decision.state !== 'policy_subject_required'
    || decision.allowed !== null
    || decision.policy_compliance_required !== true
    || !sameItems(decision.reason_codes, ['policy_subject_required'])
    || decision.decision_digest !== null
    || decision.fence_digest !== null
    || decision.receipt_ids.length !== 0
    || decision.currentness !== null
    || decision.currentness_reasons.length !== 0
    || decision.diagnostic_codes.length !== 0
    || decision.binding_decisions.length !== 0
  ) {
    throw new Error(
      'Policy Compliance gate preview has an invalid unscoped subject-required decision.',
    );
  }
}

function expectedAggregateOutcome(
  decision: PolicyComplianceTransitionDecision,
): {
  allowed: boolean;
  policyRequired: boolean;
  reasons: PolicyTransitionReasonCode[];
} {
  if (decision.state === 'transition_not_allowed') {
    return {
      allowed: false,
      policyRequired: false,
      reasons: ['transition_not_allowed'],
    };
  }
  if (decision.state === 'policy_compliance_not_required') {
    return {
      allowed: true,
      policyRequired: false,
      reasons: ['policy_compliance_not_required'],
    };
  }
  if (decision.state === 'policy_subject_required') {
    if (decision.binding_decisions.length !== 0) {
      throw new Error(
        'Policy Compliance gate preview has bindings for a missing subject.',
      );
    }
    return {
      allowed: false,
      policyRequired: true,
      reasons: ['policy_subject_required'],
    };
  }

  const blockingBindings = decision.binding_decisions.filter((item) =>
    item.applicable_metric_count > 0
    && !item.allowed
  );
  if (blockingBindings.length > 0) {
    const present = new Set<PolicyTransitionReasonCode>();
    for (const binding of blockingBindings) {
      const diagnostics = new Set(binding.diagnostic_codes);
      if (diagnostics.has('policy_compliance_receipt_missing')) {
        present.add('policy_compliance_receipt_missing');
      }
      if (diagnostics.has('policy_compliance_receipt_stale')) {
        present.add('policy_compliance_receipt_stale');
      }
      if (diagnostics.has('policy_assessment_unavailable')) {
        present.add('policy_assessment_unavailable');
      }
      if (
        diagnostics.has('policy_assessment_inadmissible')
        || diagnostics.has('policy_metric_threshold_failed')
      ) {
        present.add('policy_compliance_blocked');
      }
    }
    return {
      allowed: false,
      policyRequired: true,
      reasons: TRANSITION_REASON_ORDER.filter((reason) =>
        present.has(reason)
      ),
    };
  }

  let admitted: PolicyTransitionReasonCode;
  if (decision.applicable_metric_count === 0) {
    admitted = 'policy_compliance_not_applicable';
  } else if (
    (decision.waived_metric_count ?? 0) > 0
    || (decision.skipped_binding_count ?? 0) > 0
  ) {
    admitted = 'policy_compliance_ready_with_waivers';
  } else if (
    decision.applicable_blocking_metric_count === 0
    || (decision.advisory_issue_count ?? 0) > 0
  ) {
    admitted = 'policy_compliance_advisory_only';
  } else {
    admitted = 'policy_compliance_ready';
  }
  return {
    allowed: true,
    policyRequired: true,
    reasons: [admitted],
  };
}

function assertAggregateDecision(
  decision: PolicyComplianceTransitionDecision,
): void {
  const countValues = [
    decision.applicable_metric_count,
    decision.applicable_blocking_metric_count,
    decision.failed_metric_count,
    decision.blocking_metric_count,
    decision.waived_metric_count,
    decision.advisory_issue_count,
    decision.skipped_binding_count,
  ];
  const nullCount = countValues.filter((value) => value === null).length;
  if (nullCount !== 0 && nullCount !== countValues.length) {
    throw new Error(
      'Policy Compliance gate preview has a partial metric count envelope.',
    );
  }
  if (nullCount === countValues.length) {
    assertPlaceholderDecision(decision);
    return;
  }
  if (
    decision.allowed === null
    || decision.decision_digest === null
    || decision.fence_digest === null
  ) {
    throw new Error(
      'Policy Compliance gate preview has an incomplete governed decision envelope.',
    );
  }

  const applicable = decision.applicable_metric_count ?? 0;
  const applicableBlocking =
    decision.applicable_blocking_metric_count ?? 0;
  const failed = decision.failed_metric_count ?? 0;
  const blocking = decision.blocking_metric_count ?? 0;
  const waived = decision.waived_metric_count ?? 0;
  const advisory = decision.advisory_issue_count ?? 0;
  const skipped = decision.skipped_binding_count ?? 0;
  if (
    applicableBlocking > applicable
    || failed > applicable
    || blocking > failed
    || waived > failed
    || advisory > applicable
  ) {
    throw new Error(
      'Policy Compliance gate preview has inconsistent aggregate metric counts.',
    );
  }

  const sum = (
    selector: (item: PolicyComplianceBindingDecision) => number,
  ) => decision.binding_decisions.reduce(
    (total, item) => total + selector(item),
    0,
  );
  const expected = {
    applicable: sum((item) => item.applicable_metric_count),
    applicableBlocking: sum((item) =>
      item.enforcement === 'blocking' ? item.applicable_metric_count : 0,
    ),
    failed: sum((item) => item.failed_metric_count),
    blocking: sum((item) => item.blocking_metric_count),
    waived: sum((item) => item.waived_metric_count),
    advisory: sum((item) => item.advisory_issue_count),
    skipped: sum((item) => Number(item.skipped)),
  };
  if (
    applicable !== expected.applicable
    || applicableBlocking !== expected.applicableBlocking
    || failed !== expected.failed
    || blocking !== expected.blocking
    || waived !== expected.waived
    || advisory !== expected.advisory
    || skipped !== expected.skipped
  ) {
    throw new Error(
      'Policy Compliance gate preview metric counts do not match its binding decisions.',
    );
  }

  const expectedReceipts = decision.binding_decisions
    .map((item) => item.receipt_id)
    .filter((value): value is string => value !== null);
  const uniqueExpectedReceipts = [...new Set(expectedReceipts)]
    .sort();
  if (
    uniqueExpectedReceipts.length !== decision.receipt_ids.length
    || uniqueExpectedReceipts.some(
      (receiptId, index) => decision.receipt_ids[index] !== receiptId,
    )
  ) {
    throw new Error(
      'Policy Compliance gate preview receipts do not match its binding decisions.',
    );
  }

  const expectedDiagnostics = orderedUnion(
    DIAGNOSTIC_ORDER,
    decision.binding_decisions.map((item) => item.diagnostic_codes),
  );
  if (!sameItems(decision.diagnostic_codes, expectedDiagnostics)) {
    throw new Error(
      'Policy Compliance gate preview diagnostics do not match its binding decisions.',
    );
  }
  const expectedCurrentnessReasons = orderedUnion(
    CURRENTNESS_REASON_ORDER,
    decision.binding_decisions.map((item) => item.currentness_reasons),
  );
  const receiptBindings = decision.binding_decisions.filter(
    (item) => item.receipt_id !== null,
  );
  const expectedCurrentness: PolicyCurrentness | null =
    receiptBindings.length === 0
      ? null
      : receiptBindings.some((item) => item.currentness === 'stale')
        ? 'stale'
        : 'current';
  if (
    decision.currentness !== expectedCurrentness
    || !sameItems(
      decision.currentness_reasons,
      expectedCurrentnessReasons,
    )
  ) {
    throw new Error(
      'Policy Compliance gate preview currentness does not match its binding decisions.',
    );
  }

  const expectedOutcome = expectedAggregateOutcome(decision);
  if (
    decision.allowed !== expectedOutcome.allowed
    || decision.policy_compliance_required
      !== expectedOutcome.policyRequired
    || !sameItems(decision.reason_codes, expectedOutcome.reasons)
  ) {
    throw new Error(
      'Policy Compliance gate preview outcome does not match its binding decisions.',
    );
  }
}

export function parsePolicyComplianceTransitionDecision(
  value: unknown,
): PolicyComplianceTransitionDecision {
  if (!isRecord(value)) {
    throw new Error(
      'Policy Compliance gate preview is missing its authoritative decision.',
    );
  }
  exactFields(value, DECISION_FIELDS, 'decision');
  if (
    typeof value.state !== 'string'
    || !TRANSITION_REASON_CODES.has(value.state as PolicyTransitionReasonCode)
  ) {
    throw new Error('Policy Compliance gate preview has an unknown state.');
  }
  const state = value.state as PolicyTransitionReasonCode;
  if (
    value.allowed !== null
    && typeof value.allowed !== 'boolean'
  ) {
    throw new Error('Policy Compliance gate preview has invalid admission.');
  }
  if (typeof value.policy_compliance_required !== 'boolean') {
    throw new Error(
      'Policy Compliance gate preview has invalid enforcement authority.',
    );
  }
  const reasonCodes = orderedUniqueClosedValues(
    value.reason_codes,
    TRANSITION_REASON_ORDER,
    'reason codes',
  );
  if (reasonCodes[0] !== state) {
    throw new Error(
      'Policy Compliance gate preview state does not match its primary reason.',
    );
  }
  const currentness = parseCurrentness(
    value.currentness,
    value.currentness_reasons,
    'receipt',
  );
  const receipts = uniqueTextValues(value.receipt_ids, 'receipt identities');
  const sortedReceipts = [...receipts].sort();
  if (receipts.some((receipt, index) => receipt !== sortedReceipts[index])) {
    throw new Error(
      'Policy Compliance gate preview receipt identities are not canonical.',
    );
  }
  if (
    (currentness.currentness === null && receipts.length !== 0)
    || (currentness.currentness !== null && receipts.length === 0)
  ) {
    throw new Error(
      'Policy Compliance gate preview has inconsistent receipt currentness.',
    );
  }
  const bindingDecisions = Array.isArray(value.binding_decisions)
    ? value.binding_decisions.map(parseBindingDecision)
    : (() => {
        throw new Error(
          'Policy Compliance gate preview has invalid binding decisions.',
        );
      })();
  const bindingIds = bindingDecisions.map((item) => item.binding_id);
  const sortedBindingIds = [...bindingIds].sort();
  if (
    new Set(bindingIds).size !== bindingIds.length
    || !sameItems(bindingIds, sortedBindingIds)
  ) {
    throw new Error(
      'Policy Compliance gate preview binding decisions are not canonical.',
    );
  }
  const decision: PolicyComplianceTransitionDecision = {
    state,
    allowed: value.allowed,
    policy_compliance_required: value.policy_compliance_required,
    reason_codes: reasonCodes,
    decision_digest: nullableDigest(value.decision_digest, 'decision digest'),
    fence_digest: nullableDigest(value.fence_digest, 'fence digest'),
    receipt_ids: receipts,
    currentness: currentness.currentness,
    currentness_reasons: currentness.reasons,
    applicable_metric_count: nullableCount(
      value.applicable_metric_count,
      'applicable metric count',
    ),
    applicable_blocking_metric_count: nullableCount(
      value.applicable_blocking_metric_count,
      'applicable blocking metric count',
    ),
    failed_metric_count: nullableCount(
      value.failed_metric_count,
      'failed metric count',
    ),
    blocking_metric_count: nullableCount(
      value.blocking_metric_count,
      'blocking metric count',
    ),
    waived_metric_count: nullableCount(
      value.waived_metric_count,
      'waived metric count',
    ),
    advisory_issue_count: nullableCount(
      value.advisory_issue_count,
      'advisory issue count',
    ),
    skipped_binding_count: nullableCount(
      value.skipped_binding_count,
      'skipped binding count',
    ),
    diagnostic_codes: orderedUniqueClosedValues(
      value.diagnostic_codes,
      DIAGNOSTIC_ORDER,
      'diagnostic codes',
    ),
    binding_decisions: bindingDecisions,
  };
  assertAggregateDecision(decision);
  return decision;
}

function parseTransition(value: unknown): AllowedTransition {
  if (!isRecord(value)) {
    throw new Error('Policy Compliance gate preview has an invalid row.');
  }
  exactFields(value, TRANSITION_FIELDS, 'transition');
  if (typeof value.policy_compliance !== 'boolean') {
    throw new Error(
      'Policy Compliance gate preview metadata is unavailable from the server.',
    );
  }
  const blockedReason = value.blocked_reason === null
    ? null
    : requiredText(value.blocked_reason, 'blocked reason');
  uniqueTextValues(value.preconditions, 'preconditions');
  uniqueTextValues(value.capabilities, 'capabilities');
  uniqueTextValues(value.effects, 'effects');
  uniqueTextValues(value.reason_codes, 'transition reason codes');
  let policyDecision: PolicyComplianceTransitionDecision | null = null;
  if (value.policy_compliance) {
    policyDecision = parsePolicyComplianceTransitionDecision(
      value.policy_compliance_decision,
    );
    if (policyDecision.policy_compliance_required !== true) {
      throw new Error(
        'A governed transition lost its Policy Compliance authority.',
      );
    }
  } else if (value.policy_compliance_decision !== null) {
    throw new Error(
      'A non-enforcement transition contains a Policy Compliance decision.',
    );
  }
  return {
    to_status: requiredText(value.to_status, 'target status'),
    label: requiredText(value.label, 'target label'),
    gate: requiredText(value.gate, 'gate identity'),
    blocked_reason: blockedReason,
    preconditions: value.preconditions as string[],
    capabilities: value.capabilities as string[],
    effects: value.effects as string[],
    reason_codes: value.reason_codes as string[],
    policy_compliance: value.policy_compliance,
    policy_compliance_decision: policyDecision,
  };
}

export function projectPolicyTransitions(
  transitions: AllowedTransition[],
): PolicyTransitionProjection {
  if (!Array.isArray(transitions)) {
    throw new Error('Policy Compliance gate preview is not a transition list.');
  }
  const governed: GovernedPolicyTransition[] = [];
  const ungoverned: UngovernedPolicyTransition[] = [];
  const seen = new Set<string>();
  for (const rawTransition of transitions) {
    const transition = parseTransition(rawTransition);
    if (seen.has(transition.to_status)) {
      throw new Error(
        'Policy Compliance gate preview repeats a lifecycle target.',
      );
    }
    seen.add(transition.to_status);
    if (transition.policy_compliance) {
      governed.push({
        toStatus: transition.to_status,
        label: transition.label,
        gate: transition.gate,
        blockedReason: transition.blocked_reason,
        decision: parsePolicyComplianceTransitionDecision(
          transition.policy_compliance_decision,
        ),
      });
    } else {
      ungoverned.push({
        toStatus: transition.to_status,
        label: transition.label,
        gate: transition.gate,
      });
    }
  }
  return { governed, ungoverned };
}

export function requirePolicyTransitionEnvelope(
  response: AllowedTransitionsResponse,
  expected: PolicyTransitionEnvelopeExpectation,
): AllowedTransition[] {
  if (!isRecord(response)) {
    throw new Error(
      'Policy Compliance transition authority returned an invalid envelope.',
    );
  }
  exactFields(response, ENVELOPE_FIELDS, 'authority envelope');
  if (
    response.board_id !== expected.boardId
    || response.entity_type !== expected.entityType
    || response.entity_id !== expected.subjectId
    || response.current_status !== expected.currentStatus
    || response.source !== 'core_sdlc_registry_v1'
  ) {
    throw new Error(
      'Policy Compliance transition authority does not match the active subject.',
    );
  }
  if (!Array.isArray(response.allowed_transitions)) {
    throw new Error(
      'Policy Compliance transition authority omitted its transition list.',
    );
  }
  const projection = projectPolicyTransitions(response.allowed_transitions);
  if (projection.governed.some((item) => item.decision.allowed === null)) {
    throw new Error(
      'Policy Compliance transition authority returned an unscoped governed decision for the active subject.',
    );
  }
  return response.allowed_transitions;
}

export function parsePolicyTransitionRejection(
  error: unknown,
  expected: PolicyTransitionRejectionExpectation,
): PolicyTransitionRejection {
  if (
    !(error instanceof AuthenticatedFetchError)
    || error.status !== 409
    || !isRecord(error.details)
  ) {
    throw new Error(
      'Policy Compliance transition rejection is not a structured 409.',
    );
  }
  const details = error.details;
  exactFields(details, REJECTION_FIELDS, 'rejection');
  const code = requiredText(details.code, 'rejection code');
  if (
    details.outcome !== 'error'
    || details.error !== code
    || !REJECTED_STATES.has(code as PolicyTransitionReasonCode)
    || typeof details.policy_compliance_required !== 'boolean'
  ) {
    throw new Error(
      'Policy Compliance transition rejection has invalid authority.',
    );
  }
  const counts = details.counts;
  const transition = details.transition;
  if (
    !isRecord(counts)
    || !isRecord(transition)
    || !Array.isArray(details.binding_decisions)
  ) {
    throw new Error(
      'Policy Compliance transition rejection omitted its evidence.',
    );
  }
  exactFields(counts, REJECTION_COUNT_FIELDS, 'rejection count');
  exactFields(
    transition,
    REJECTION_TRANSITION_FIELDS,
    'rejection transition',
  );
  if (
    transition.entity_type !== expected.entityType
    || transition.subject_id !== expected.subjectId
    || transition.from_status !== expected.currentStatus
    || transition.to_status !== expected.toStatus
  ) {
    throw new Error(
      'Policy Compliance transition rejection does not match the active action.',
    );
  }
  const decision = parsePolicyComplianceTransitionDecision({
    state: code,
    allowed: false,
    policy_compliance_required: details.policy_compliance_required,
    reason_codes: details.reason_codes,
    decision_digest: details.decision_digest,
    fence_digest: details.fence_digest,
    receipt_ids: details.receipt_ids,
    currentness: details.currentness,
    currentness_reasons: details.currentness_reasons,
    applicable_metric_count: counts.applicable_metrics,
    applicable_blocking_metric_count: counts.applicable_blocking_metrics,
    failed_metric_count: counts.failed_metrics,
    blocking_metric_count: counts.blocking_metrics,
    waived_metric_count: counts.waived_metrics,
    advisory_issue_count: counts.advisory_issues,
    skipped_binding_count: counts.skipped_bindings,
    diagnostic_codes: details.diagnostic_codes,
    binding_decisions: details.binding_decisions,
  });
  return {
    code: code as PolicyTransitionReasonCode,
    message: requiredText(details.message, 'rejection message'),
    entityType: expected.entityType,
    subjectId: expected.subjectId,
    fromStatus: expected.currentStatus,
    toStatus: expected.toStatus,
    decision,
  };
}

export function readPolicyTransitionRejection(
  error: unknown,
  expected: PolicyTransitionRejectionExpectation,
): PolicyTransitionRejection | null {
  try {
    return parsePolicyTransitionRejection(error, expected);
  } catch {
    return null;
  }
}

export function policyTransitionRejectionMessage(
  rejection: PolicyTransitionRejection,
): string {
  const blocking = rejection.decision.blocking_metric_count ?? 0;
  const receipts = rejection.decision.receipt_ids.length > 0
    ? rejection.decision.receipt_ids.join(', ')
    : 'none';
  const currentness = rejection.decision.currentness ?? 'not recorded';
  return [
    `Policy Compliance rejected ${rejection.fromStatus} → ${rejection.toStatus}`,
    rejection.code,
    `${blocking} blocking metric${blocking === 1 ? '' : 's'}`,
    `receipts ${receipts} (${currentness})`,
  ].join(' · ');
}

export function isAllowedTransitionActionable(
  transition: AllowedTransition,
): boolean {
  if (!isRecord(transition)) return false;
  try {
    const parsed = parseTransition(transition);
    if (parsed.blocked_reason !== null) return false;
    if (!parsed.policy_compliance) return true;
    return parsePolicyComplianceTransitionDecision(
      parsed.policy_compliance_decision,
    ).allowed === true;
  } catch {
    return false;
  }
}
