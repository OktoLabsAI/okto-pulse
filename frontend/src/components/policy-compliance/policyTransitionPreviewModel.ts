import type {
  AllowedTransition,
  AllowedTransitionEntityType,
  AllowedTransitionsResponse,
  PolicyComplianceTransitionDecision,
  PolicyCurrentnessReason,
  PolicyTransitionReasonCode,
} from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';

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

const TRANSITION_REASON_CODES = new Set<PolicyTransitionReasonCode>([
  'transition_not_allowed',
  'policy_compliance_not_required',
  'policy_compliance_receipt_missing',
  'policy_compliance_receipt_stale',
  'policy_compliance_blocked',
  'policy_evaluation_unavailable',
  'policy_evaluation_degraded',
  'policy_compliance_ready',
  'policy_compliance_ready_with_waivers',
  'policy_compliance_not_applicable',
  'policy_compliance_advisory_only',
  'policy_subject_required',
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

const ADMITTED_STATES = new Set<PolicyTransitionReasonCode>([
  'policy_evaluation_degraded',
  'policy_compliance_ready',
  'policy_compliance_ready_with_waivers',
  'policy_compliance_not_applicable',
  'policy_compliance_advisory_only',
]);
const REJECTED_STATES = new Set<PolicyTransitionReasonCode>([
  'policy_compliance_receipt_missing',
  'policy_compliance_receipt_stale',
  'policy_compliance_blocked',
  'policy_evaluation_unavailable',
]);
const CURRENT_RECEIPT_STATES = new Set<PolicyTransitionReasonCode>([
  'policy_compliance_ready',
  'policy_compliance_ready_with_waivers',
  'policy_compliance_advisory_only',
  'policy_compliance_blocked',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
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

function nullableCount(value: unknown, field: string): number | null {
  if (value === null) return null;
  if (
    typeof value !== 'number'
    || !Number.isInteger(value)
    || value < 0
  ) {
    throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
  }
  return value;
}

function uniqueClosedValues<T extends string>(
  value: unknown,
  allowed: Set<T>,
  field: string,
  allowEmpty: boolean,
): T[] {
  if (!Array.isArray(value) || (!allowEmpty && value.length === 0)) {
    throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
  }
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
  return items;
}

function uniqueTextValues(value: unknown, field: string): string[] {
  if (!Array.isArray(value)) {
    throw new Error(`Policy Compliance gate preview has invalid ${field}.`);
  }
  const items: string[] = [];
  for (const item of value) {
    const text = requiredText(item, field);
    if (items.includes(text)) {
      throw new Error(`Policy Compliance gate preview repeats ${field}.`);
    }
    items.push(text);
  }
  return items;
}

export function parsePolicyComplianceTransitionDecision(
  value: unknown,
): PolicyComplianceTransitionDecision {
  if (!isRecord(value)) {
    throw new Error(
      'Policy Compliance gate preview is missing its authoritative decision.',
    );
  }
  const state = requiredText(value.state, 'decision state');
  if (!TRANSITION_REASON_CODES.has(state as PolicyTransitionReasonCode)) {
    throw new Error('Policy Compliance gate preview has an unknown state.');
  }
  if (
    value.allowed !== null
    && typeof value.allowed !== 'boolean'
  ) {
    throw new Error('Policy Compliance gate preview has invalid admission.');
  }
  if (value.policy_compliance_required !== true) {
    throw new Error(
      'Policy Compliance gate preview lost its enforcement authority.',
    );
  }
  const reasonCodes = uniqueClosedValues(
    value.reason_codes,
    TRANSITION_REASON_CODES,
    'reason codes',
    false,
  );
  if (reasonCodes[0] !== state) {
    throw new Error(
      'Policy Compliance gate preview state does not match its primary reason.',
    );
  }
  const currentnessReasons = uniqueClosedValues(
    value.currentness_reasons,
    CURRENTNESS_REASONS,
    'currentness reasons',
    true,
  );
  const receiptId = nullableText(value.receipt_id, 'receipt identity');
  const currentness = value.currentness;
  if (
    currentness !== null
    && currentness !== 'current'
    && currentness !== 'stale'
  ) {
    throw new Error(
      'Policy Compliance gate preview has invalid receipt currentness.',
    );
  }
  if ((receiptId === null) !== (currentness === null)) {
    throw new Error(
      'Policy Compliance gate preview has inconsistent receipt currentness.',
    );
  }
  if (
    (currentness === null || currentness === 'current')
    && currentnessReasons.length > 0
  ) {
    throw new Error(
      'Policy Compliance gate preview has unexpected currentness reasons.',
    );
  }
  if (currentness === 'stale' && currentnessReasons.length === 0) {
    throw new Error(
      'Policy Compliance gate preview omitted its stale reasons.',
    );
  }

  const decision = {
    state: state as PolicyTransitionReasonCode,
    allowed: value.allowed as boolean | null,
    policy_compliance_required: true,
    reason_codes: reasonCodes,
    decision_digest: nullableDigest(
      value.decision_digest,
      'decision digest',
    ),
    fence_digest: nullableDigest(value.fence_digest, 'fence digest'),
    receipt_id: receiptId,
    currentness,
    currentness_reasons: currentnessReasons,
    applicable_rule_count: nullableCount(
      value.applicable_rule_count,
      'applicable rule count',
    ),
    applicable_blocking_rule_count: nullableCount(
      value.applicable_blocking_rule_count,
      'applicable blocking rule count',
    ),
    blocking_rule_count: nullableCount(
      value.blocking_rule_count,
      'blocking rule count',
    ),
    waived_rule_count: nullableCount(
      value.waived_rule_count,
      'waived rule count',
    ),
    advisory_issue_count: nullableCount(
      value.advisory_issue_count,
      'advisory issue count',
    ),
  } satisfies PolicyComplianceTransitionDecision;

  if (decision.state === 'policy_subject_required') {
    if (
      decision.allowed !== null
      || decision.decision_digest !== null
      || decision.fence_digest !== null
      || decision.receipt_id !== null
      || decision.applicable_rule_count !== null
      || decision.applicable_blocking_rule_count !== null
      || decision.blocking_rule_count !== null
      || decision.waived_rule_count !== null
      || decision.advisory_issue_count !== null
    ) {
      throw new Error(
        'Policy Compliance subject-required preview contains fabricated evidence.',
      );
    }
  } else if (
    decision.allowed === null
    || decision.decision_digest === null
    || decision.fence_digest === null
    || decision.applicable_rule_count === null
    || decision.applicable_blocking_rule_count === null
    || decision.blocking_rule_count === null
    || decision.waived_rule_count === null
    || decision.advisory_issue_count === null
  ) {
    throw new Error(
      'Policy Compliance gate preview is missing its evaluated authority.',
    );
  }
  if (
    ADMITTED_STATES.has(decision.state)
    && decision.allowed !== true
  ) {
    throw new Error(
      'Policy Compliance gate preview contradicts its admitted state.',
    );
  }
  if (
    REJECTED_STATES.has(decision.state)
    && decision.allowed !== false
  ) {
    throw new Error(
      'Policy Compliance gate preview contradicts its rejected state.',
    );
  }
  if (
    decision.state === 'transition_not_allowed'
    || decision.state === 'policy_compliance_not_required'
  ) {
    throw new Error(
      'Policy Compliance gate preview contains an out-of-scope decision.',
    );
  }
  if (
    CURRENT_RECEIPT_STATES.has(decision.state)
    && (
      decision.receipt_id === null
      || decision.currentness !== 'current'
    )
  ) {
    throw new Error(
      'Policy Compliance gate preview state requires a current receipt.',
    );
  }
  if (
    decision.state === 'policy_compliance_receipt_stale'
    && (
      decision.receipt_id === null
      || decision.currentness !== 'stale'
    )
  ) {
    throw new Error(
      'Policy Compliance stale state requires a stale receipt.',
    );
  }
  if (
    decision.state === 'policy_compliance_receipt_missing'
    && (
      decision.receipt_id !== null
      || decision.currentness !== null
    )
  ) {
    throw new Error(
      'Policy Compliance missing-receipt state contains receipt evidence.',
    );
  }
  if (
    decision.state === 'policy_compliance_ready_with_waivers'
    && (
      decision.waived_rule_count === 0
      || decision.blocking_rule_count !== 0
    )
  ) {
    throw new Error(
      'Policy Compliance waiver-ready state omitted its waiver count.',
    );
  }
  if (
    decision.state === 'policy_compliance_advisory_only'
    && (
      decision.advisory_issue_count === 0
      || decision.blocking_rule_count !== 0
      || decision.waived_rule_count !== 0
    )
  ) {
    throw new Error(
      'Policy Compliance advisory state omitted its advisory issues.',
    );
  }
  if (
    decision.state === 'policy_compliance_blocked'
    && decision.blocking_rule_count === 0
  ) {
    throw new Error(
      'Policy Compliance blocked state omitted its blocking rules.',
    );
  }
  if (
    decision.state === 'policy_compliance_ready'
    && (
      decision.blocking_rule_count !== 0
      || decision.waived_rule_count !== 0
      || decision.advisory_issue_count !== 0
    )
  ) {
    throw new Error(
      'Policy Compliance ready state contains unresolved outcomes.',
    );
  }
  if (
    decision.state === 'policy_compliance_not_applicable'
    && decision.applicable_rule_count !== 0
  ) {
    throw new Error(
      'Policy Compliance not-applicable state contains applicable rules.',
    );
  }
  if (
    decision.state === 'policy_evaluation_degraded'
    && (
      decision.applicable_rule_count === 0
      || decision.applicable_blocking_rule_count !== 0
    )
  ) {
    throw new Error(
      'Policy Compliance degraded state has inconsistent applicability.',
    );
  }
  if (
    (
      decision.state === 'policy_compliance_receipt_missing'
      || decision.state === 'policy_compliance_receipt_stale'
      || decision.state === 'policy_evaluation_unavailable'
    )
    && decision.applicable_blocking_rule_count === 0
  ) {
    throw new Error(
      'Policy Compliance rejected state omitted applicable blocking rules.',
    );
  }
  if (
    decision.applicable_rule_count !== null
    && decision.applicable_blocking_rule_count !== null
    && decision.applicable_blocking_rule_count
      > decision.applicable_rule_count
  ) {
    throw new Error(
      'Policy Compliance gate preview has inconsistent applicable counts.',
    );
  }
  if (
    decision.currentness === 'current'
    && decision.applicable_rule_count !== null
    && decision.applicable_blocking_rule_count !== null
    && decision.blocking_rule_count !== null
    && decision.waived_rule_count !== null
    && decision.advisory_issue_count !== null
    && (
      decision.blocking_rule_count
        > decision.applicable_blocking_rule_count
      || decision.waived_rule_count
        > decision.applicable_rule_count
      || decision.advisory_issue_count
        > decision.applicable_rule_count
    )
  ) {
    throw new Error(
      'Policy Compliance gate preview has inconsistent current counts.',
    );
  }
  return decision;
}

export function projectPolicyTransitions(
  transitions: AllowedTransition[],
): PolicyTransitionProjection {
  if (!Array.isArray(transitions)) {
    throw new Error('Policy Compliance gate preview is not a transition list.');
  }
  const governed: GovernedPolicyTransition[] = [];
  const ungoverned: UngovernedPolicyTransition[] = [];
  const seenStatuses = new Set<string>();
  for (const transition of transitions) {
    if (!isRecord(transition)) {
      throw new Error('Policy Compliance gate preview has an invalid row.');
    }
    const toStatus = requiredText(transition.to_status, 'target status');
    if (seenStatuses.has(toStatus)) {
      throw new Error(
        'Policy Compliance gate preview repeats a lifecycle target.',
      );
    }
    seenStatuses.add(toStatus);
    const label = requiredText(transition.label, 'target label');
    const gate = requiredText(transition.gate, 'gate identity');
    if (typeof transition.policy_compliance !== 'boolean') {
      throw new Error(
        'Policy Compliance gate preview metadata is unavailable from the server.',
      );
    }
    const blockedReason =
      transition.blocked_reason === null
        ? null
        : requiredText(transition.blocked_reason, 'blocked reason');
    uniqueTextValues(transition.preconditions, 'preconditions');
    uniqueTextValues(transition.capabilities, 'capabilities');
    uniqueTextValues(transition.effects, 'effects');
    uniqueTextValues(transition.reason_codes, 'transition reason codes');
    if (!transition.policy_compliance) {
      if (
        transition.policy_compliance_decision !== null
        && transition.policy_compliance_decision !== undefined
      ) {
        throw new Error(
          'A non-enforcement transition contains a Policy Compliance decision.',
        );
      }
      ungoverned.push({ toStatus, label, gate });
      continue;
    }
    governed.push({
      toStatus,
      label,
      gate,
      blockedReason,
      decision: parsePolicyComplianceTransitionDecision(
        transition.policy_compliance_decision,
      ),
    });
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
  projectPolicyTransitions(response.allowed_transitions);
  return response.allowed_transitions;
}

/**
 * Parse the exact structured 409 emitted by the mutation authority.
 *
 * Preview and rejection share one decision parser so state, receipt,
 * currentness, counts, digests, and action scope cannot drift in the UI.
 */
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
  const code = requiredText(details.code, 'rejection code');
  if (
    details.outcome !== 'error'
    || details.error !== code
    || !REJECTED_STATES.has(code as PolicyTransitionReasonCode)
    || details.policy_compliance_required !== true
  ) {
    throw new Error(
      'Policy Compliance transition rejection has invalid authority.',
    );
  }
  const counts = details.counts;
  const transition = details.transition;
  if (!isRecord(counts) || !isRecord(transition)) {
    throw new Error(
      'Policy Compliance transition rejection omitted its evidence.',
    );
  }
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
    policy_compliance_required: true,
    reason_codes: details.reason_codes,
    decision_digest: details.decision_digest,
    fence_digest: details.fence_digest,
    receipt_id: details.receipt_id,
    currentness: details.currentness,
    currentness_reasons: details.currentness_reasons,
    applicable_rule_count: counts.applicable_rules,
    applicable_blocking_rule_count: counts.applicable_blocking_rules,
    blocking_rule_count: counts.blocking_rules,
    waived_rule_count: counts.waived_rules,
    advisory_issue_count: counts.advisory_issues,
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
  const blocking = rejection.decision.blocking_rule_count ?? 0;
  const receipt = rejection.decision.receipt_id ?? 'none';
  const currentness = rejection.decision.currentness ?? 'not recorded';
  return [
    `Policy Compliance rejected ${rejection.fromStatus} → ${rejection.toStatus}`,
    rejection.code,
    `${blocking} blocking rule${blocking === 1 ? '' : 's'}`,
    `receipt ${receipt} (${currentness})`,
  ].join(' · ');
}

/**
 * Decide whether the aggregate backend transition row may expose an action.
 *
 * Any generic lifecycle blocker wins. Recovery and non-enforcement rows then
 * remain available to their own gates, while governed rows additionally need
 * a structurally valid Policy decision with allowed=true.
 */
export function isAllowedTransitionActionable(
  transition: AllowedTransition,
): boolean {
  if (!isRecord(transition)) {
    return false;
  }
  if (
    transition.blocked_reason !== null
    && transition.blocked_reason !== undefined
  ) {
    return false;
  }
  if (transition.policy_compliance === false) {
    return (
      transition.policy_compliance_decision === null
      || transition.policy_compliance_decision === undefined
    );
  }
  if (transition.policy_compliance !== true) {
    return false;
  }
  try {
    return parsePolicyComplianceTransitionDecision(
      transition.policy_compliance_decision,
    ).allowed === true;
  } catch {
    return false;
  }
}
