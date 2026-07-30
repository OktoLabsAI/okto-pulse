import { describe, expect, it } from 'vitest';

import type { AllowedTransition } from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';

import {
  isAllowedTransitionActionable,
  parsePolicyComplianceTransitionDecision,
  parsePolicyTransitionRejection,
  policyTransitionRejectionMessage,
  projectPolicyTransitions,
  requirePolicyTransitionEnvelope,
} from '../policyTransitionPreviewModel';

function decision(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    state: 'policy_compliance_ready',
    allowed: true,
    policy_compliance_required: true,
    reason_codes: ['policy_compliance_ready'],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_id: 'receipt-1',
    currentness: 'current',
    currentness_reasons: [],
    applicable_rule_count: 3,
    applicable_blocking_rule_count: 2,
    blocking_rule_count: 0,
    waived_rule_count: 0,
    advisory_issue_count: 0,
    ...overrides,
  };
}

function governed(
  overrides: Partial<AllowedTransition> = {},
): AllowedTransition {
  return {
    to_status: 'validated',
    label: 'Validated',
    gate: 'approved_to_validated',
    blocked_reason: null,
    preconditions: [],
    capabilities: [],
    effects: [],
    reason_codes: [],
    policy_compliance: true,
    policy_compliance_decision: decision() as never,
    ...overrides,
  } as AllowedTransition;
}

describe('policyTransitionPreviewModel', () => {
  it('projects authoritative enforcement and recovery rows separately', () => {
    const projection = projectPolicyTransitions([
      governed(),
      {
        to_status: 'cancelled',
        label: 'Cancelled',
        gate: 'cancel',
        blocked_reason: null,
        preconditions: [],
        capabilities: [],
        effects: [],
        reason_codes: [],
        policy_compliance: false,
        policy_compliance_decision: null,
      },
    ]);

    expect(projection.governed).toHaveLength(1);
    expect(projection.governed[0]).toMatchObject({
      toStatus: 'validated',
      decision: {
        receipt_id: 'receipt-1',
        allowed: true,
      },
    });
    expect(projection.ungoverned).toEqual([
      {
        toStatus: 'cancelled',
        label: 'Cancelled',
        gate: 'cancel',
      },
    ]);
  });

  it('fails closed when transition metadata is absent', () => {
    expect(() => projectPolicyTransitions([
      {
        to_status: 'validated',
        label: 'Validated',
        gate: 'approved_to_validated',
      } as unknown as AllowedTransition,
    ])).toThrow(/metadata is unavailable/i);
  });

  it('rejects a decision whose state disagrees with its primary reason', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      reason_codes: ['policy_compliance_blocked'],
    }))).toThrow(/primary reason/i);
  });

  it('rejects admission and count contradictions', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      allowed: false,
    }))).toThrow(/contradicts its admitted state/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_blocked',
      reason_codes: ['policy_compliance_blocked'],
      allowed: true,
    }))).toThrow(/contradicts its rejected state/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      applicable_rule_count: 1,
      applicable_blocking_rule_count: 2,
    }))).toThrow(/inconsistent applicable counts/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      applicable_rule_count: 2,
      applicable_blocking_rule_count: 1,
      blocking_rule_count: 2,
    }))).toThrow(/unresolved outcomes|inconsistent current counts/i);
  });

  it('requires a stale receipt to include closed currentness reasons', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      currentness: 'stale',
      currentness_reasons: [],
    }))).toThrow(/stale reasons/i);

    expect(() => parsePolicyComplianceTransitionDecision(decision({
      currentness: 'stale',
      currentness_reasons: ['unknown_reason'],
    }))).toThrow(/currentness reasons/i);
  });

  it('binds admission states to canonical receipt currentness', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      receipt_id: null,
      currentness: null,
    }))).toThrow(/requires a current receipt/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      currentness: 'stale',
      currentness_reasons: ['subject_version_changed'],
    }))).toThrow(/requires a current receipt/i);

    expect(parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_receipt_stale',
      allowed: false,
      reason_codes: ['policy_compliance_receipt_stale'],
      currentness: 'stale',
      currentness_reasons: ['subject_version_changed'],
    }))).toMatchObject({
      state: 'policy_compliance_receipt_stale',
      currentness: 'stale',
    });
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_receipt_stale',
      allowed: false,
      reason_codes: ['policy_compliance_receipt_stale'],
    }))).toThrow(/requires a stale receipt/i);

    expect(parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_receipt_missing',
      allowed: false,
      reason_codes: ['policy_compliance_receipt_missing'],
      receipt_id: null,
      currentness: null,
    }))).toMatchObject({
      state: 'policy_compliance_receipt_missing',
      receipt_id: null,
    });
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_receipt_missing',
      allowed: false,
      reason_codes: ['policy_compliance_receipt_missing'],
    }))).toThrow(/contains receipt evidence/i);
  });

  it('rejects impossible canonical state and outcome correlations', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_ready_with_waivers',
      reason_codes: ['policy_compliance_ready_with_waivers'],
    }))).toThrow(/waiver count/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_advisory_only',
      reason_codes: ['policy_compliance_advisory_only'],
    }))).toThrow(/advisory issues/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_blocked',
      allowed: false,
      reason_codes: ['policy_compliance_blocked'],
    }))).toThrow(/blocking rules/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      advisory_issue_count: 1,
    }))).toThrow(/unresolved outcomes/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_evaluation_degraded',
      reason_codes: ['policy_evaluation_degraded'],
      receipt_id: null,
      currentness: null,
      applicable_blocking_rule_count: 1,
    }))).toThrow(/inconsistent applicability/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_not_applicable',
      reason_codes: ['policy_compliance_not_applicable'],
    }))).toThrow(/contains applicable rules/i);
  });

  it('accepts subject-required only without fabricated evidence', () => {
    const subjectRequired = decision({
      state: 'policy_subject_required',
      allowed: null,
      reason_codes: ['policy_subject_required'],
      decision_digest: null,
      fence_digest: null,
      receipt_id: null,
      currentness: null,
      applicable_rule_count: null,
      applicable_blocking_rule_count: null,
      blocking_rule_count: null,
      waived_rule_count: null,
      advisory_issue_count: null,
    });

    expect(
      parsePolicyComplianceTransitionDecision(subjectRequired),
    ).toMatchObject({
      state: 'policy_subject_required',
      allowed: null,
      receipt_id: null,
    });
    expect(() => parsePolicyComplianceTransitionDecision({
      ...subjectRequired,
      receipt_id: 'fabricated',
      currentness: 'current',
    })).toThrow(/fabricated evidence/i);
  });

  it('rejects duplicate lifecycle targets and decisions on non-policy rows', () => {
    expect(() => projectPolicyTransitions([
      governed(),
      governed(),
    ])).toThrow(/repeats a lifecycle target/i);

    expect(() => projectPolicyTransitions([
      {
        to_status: 'cancelled',
        label: 'Cancelled',
        gate: 'cancel',
        blocked_reason: null,
        preconditions: [],
        capabilities: [],
        effects: [],
        reason_codes: [],
        policy_compliance: false,
        policy_compliance_decision: decision() as never,
      },
    ])).toThrow(/non-enforcement transition/i);
  });

  it('admits only an authoritative ready edge while preserving recovery', () => {
    expect(isAllowedTransitionActionable(governed())).toBe(true);
    expect(isAllowedTransitionActionable(governed({
      policy_compliance_decision: decision({
        state: 'policy_compliance_blocked',
        allowed: false,
        reason_codes: ['policy_compliance_blocked'],
      }) as never,
    }))).toBe(false);
    expect(isAllowedTransitionActionable({
      to_status: 'cancelled',
      label: 'Cancelled',
      gate: 'cancel',
      blocked_reason: null,
      preconditions: [],
      capabilities: [],
      effects: [],
      reason_codes: [],
      policy_compliance: false,
      policy_compliance_decision: null,
    })).toBe(true);
    expect(isAllowedTransitionActionable({
      to_status: 'cancelled',
      label: 'Cancelled',
      gate: 'cancel',
      blocked_reason: 'Cancellation justification is required.',
      preconditions: [],
      capabilities: [],
      effects: [],
      reason_codes: [],
      policy_compliance: false,
      policy_compliance_decision: null,
    })).toBe(false);
    expect(isAllowedTransitionActionable({
      to_status: 'done',
      label: 'Done',
      gate: 'forward',
    } as unknown as AllowedTransition)).toBe(false);
  });

  it('binds the transition envelope to the active subject and authority', () => {
    const response = {
      board_id: 'board-1',
      entity_type: 'spec' as const,
      entity_id: 'spec-1',
      current_status: 'approved',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [governed()],
    };
    const expected = {
      boardId: 'board-1',
      entityType: 'spec' as const,
      subjectId: 'spec-1',
      currentStatus: 'approved',
    };

    expect(
      requirePolicyTransitionEnvelope(response, expected),
    ).toEqual(response.allowed_transitions);
    expect(() => requirePolicyTransitionEnvelope({
      ...response,
      entity_id: 'spec-other',
    }, expected)).toThrow(/does not match the active subject/i);
    expect(() => requirePolicyTransitionEnvelope({
      ...response,
      source: 'legacy_client_map',
    }, expected)).toThrow(/does not match the active subject/i);
    expect(() => requirePolicyTransitionEnvelope({
      ...response,
      allowed_transitions: [
        governed(),
        governed({ policy_compliance: false, policy_compliance_decision: null }),
      ],
    }, expected)).toThrow(/repeats a lifecycle target/i);
  });

  it('preserves and scopes the exact structured mutation rejection', () => {
    const error = new AuthenticatedFetchError({
      status: 409,
      code: 'policy_compliance_blocked',
      message: 'policy_compliance_blocked',
      details: {
        outcome: 'error',
        error: 'policy_compliance_blocked',
        code: 'policy_compliance_blocked',
        message: 'policy_compliance_blocked',
        reason_codes: ['policy_compliance_blocked'],
        decision_digest: 'a'.repeat(64),
        fence_digest: 'b'.repeat(64),
        receipt_id: 'receipt-locked',
        currentness: 'current',
        currentness_reasons: [],
        counts: {
          applicable_rules: 3,
          applicable_blocking_rules: 2,
          blocking_rules: 1,
          waived_rules: 0,
          advisory_issues: 0,
        },
        transition: {
          entity_type: 'spec',
          subject_id: 'spec-1',
          from_status: 'approved',
          to_status: 'validated',
        },
        policy_compliance_required: true,
      },
    });
    const expected = {
      boardId: 'board-1',
      entityType: 'spec' as const,
      subjectId: 'spec-1',
      currentStatus: 'approved',
      toStatus: 'validated',
    };

    const rejection = parsePolicyTransitionRejection(error, expected);

    expect(rejection).toMatchObject({
      code: 'policy_compliance_blocked',
      decision: {
        receipt_id: 'receipt-locked',
        currentness: 'current',
        blocking_rule_count: 1,
      },
    });
    expect(policyTransitionRejectionMessage(rejection)).toContain(
      'receipt receipt-locked (current)',
    );
    expect(() => parsePolicyTransitionRejection(error, {
      ...expected,
      subjectId: 'spec-other',
    })).toThrow(/does not match the active action/i);
  });
});
