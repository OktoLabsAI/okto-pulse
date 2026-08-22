import { describe, expect, it } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import type {
  AllowedTransition,
  PolicyComplianceBindingDecision,
  PolicyComplianceTransitionDecision,
} from '@/types';

import {
  isAllowedTransitionActionable,
  parsePolicyComplianceTransitionDecision,
  parsePolicyTransitionRejection,
  policyTransitionRejectionMessage,
  projectPolicyTransitions,
  requirePolicyTransitionEnvelope,
} from '../policyTransitionPreviewModel';

function binding(
  overrides: Partial<PolicyComplianceBindingDecision> = {},
): PolicyComplianceBindingDecision {
  return {
    binding_id: 'binding-hexagonal',
    guideline_id: 'guideline-hexagonal',
    enforcement: 'blocking',
    applicable_metric_count: 3,
    allowed: true,
    assessment_available: true,
    receipt_id: 'receipt-1',
    currentness: 'current',
    currentness_reasons: [],
    inadmissibility_cause: null,
    failed_metric_count: 0,
    waived_metric_count: 0,
    blocking_metric_count: 0,
    advisory_issue_count: 0,
    skipped: false,
    diagnostic_codes: [],
    ...overrides,
  };
}

function decision(
  overrides: Partial<PolicyComplianceTransitionDecision> = {},
): PolicyComplianceTransitionDecision {
  return {
    state: 'policy_compliance_ready',
    allowed: true,
    policy_compliance_required: true,
    reason_codes: ['policy_compliance_ready'],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_ids: ['receipt-1'],
    currentness: 'current',
    currentness_reasons: [],
    applicable_metric_count: 3,
    applicable_blocking_metric_count: 3,
    failed_metric_count: 0,
    blocking_metric_count: 0,
    waived_metric_count: 0,
    advisory_issue_count: 0,
    skipped_binding_count: 0,
    diagnostic_codes: [],
    binding_decisions: [binding()],
    ...overrides,
    projection: overrides.projection ?? 'full',
  };
}

function blockedDecision(): PolicyComplianceTransitionDecision {
  return decision({
    state: 'policy_compliance_blocked',
    allowed: false,
    reason_codes: ['policy_compliance_blocked'],
    failed_metric_count: 1,
    blocking_metric_count: 1,
    diagnostic_codes: ['policy_metric_threshold_failed'],
    binding_decisions: [binding({
      allowed: false,
      failed_metric_count: 1,
      blocking_metric_count: 1,
      diagnostic_codes: ['policy_metric_threshold_failed'],
    })],
  });
}

function governed(
  overrides: Partial<AllowedTransition> = {},
): AllowedTransition {
  return {
    to_status: 'validated',
    label: 'Validated',
    gate: 'approved_to_validated',
    blocked_reason: null,
    blocked_facts: null,
    preconditions: [],
    capabilities: [],
    effects: [],
    reason_codes: [],
    policy_compliance: true,
    policy_compliance_decision: decision(),
    ...overrides,
  };
}

describe('policyTransitionPreviewModel', () => {
  it('projects semantic metric authority and recovery rows separately', () => {
    const projection = projectPolicyTransitions([
      governed(),
      {
        to_status: 'cancelled',
        label: 'Cancelled',
        gate: 'cancel',
        blocked_reason: null,
        blocked_facts: null,
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
        receipt_ids: ['receipt-1'],
        applicable_metric_count: 3,
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
    ])).toThrow(/unknown or missing transition field/i);
  });

  it('fails closed on unknown decision, binding, transition, and envelope fields', () => {
    expect(() => parsePolicyComplianceTransitionDecision({
      ...decision(),
      future_authority: true,
    })).toThrow(/unknown or missing decision field/i);

    expect(() => parsePolicyComplianceTransitionDecision(decision({
      binding_decisions: [{
        ...binding(),
        future_binding_authority: true,
      } as unknown as PolicyComplianceBindingDecision],
    }))).toThrow(/unknown or missing binding decision field/i);

    expect(() => projectPolicyTransitions([{
      ...governed(),
      future_transition_authority: true,
    } as unknown as AllowedTransition])).toThrow(
      /unknown or missing transition field/i,
    );

    const response = {
      board_id: 'board-1',
      entity_type: 'spec' as const,
      entity_id: 'spec-1',
      current_status: 'approved',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [governed()],
      future_envelope_authority: true,
    };
    expect(() => requirePolicyTransitionEnvelope(response as never, {
      boardId: 'board-1',
      entityType: 'spec',
      subjectId: 'spec-1',
      currentStatus: 'approved',
    })).toThrow(/unknown or missing authority envelope field/i);
  });

  it('rejects a decision whose state disagrees with its primary reason', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      reason_codes: ['policy_compliance_blocked'],
    }))).toThrow(/primary reason/i);
  });

  it('rejects metric contradictions and aggregate drift from bindings', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      applicable_metric_count: 2,
      applicable_blocking_metric_count: 2,
    }))).toThrow(/do not match its binding decisions/i);

    expect(() => parsePolicyComplianceTransitionDecision(decision({
      failed_metric_count: 1,
      blocking_metric_count: 1,
      waived_metric_count: 1,
    }))).toThrow(/metric counts do not match its binding decisions/i);

    expect(() => parsePolicyComplianceTransitionDecision(decision({
      applicable_metric_count: null,
    }))).toThrow(/partial metric count envelope/i);
  });

  it('requires stale assessment receipts to use closed currentness reasons', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      currentness: 'stale',
      currentness_reasons: [],
      binding_decisions: [binding({
        currentness: 'stale',
        currentness_reasons: [],
      })],
    }))).toThrow(/stale reasons|inconsistent receipt currentness/i);

    expect(() => parsePolicyComplianceTransitionDecision(decision({
      currentness: 'stale',
      currentness_reasons: ['unknown_reason' as never],
      binding_decisions: [binding({
        currentness: 'stale',
        currentness_reasons: ['unknown_reason' as never],
      })],
    }))).toThrow(/currentness reasons/i);
  });

  it('accepts canonical stale and unavailable semantic-assessment states', () => {
    const stale = decision({
      state: 'policy_compliance_receipt_stale',
      allowed: false,
      reason_codes: ['policy_compliance_receipt_stale'],
      currentness: 'stale',
      currentness_reasons: ['subject_version_changed'],
      diagnostic_codes: ['policy_compliance_receipt_stale'],
      binding_decisions: [binding({
        allowed: false,
        currentness: 'stale',
        currentness_reasons: ['subject_version_changed'],
        diagnostic_codes: ['policy_compliance_receipt_stale'],
      })],
    });
    expect(parsePolicyComplianceTransitionDecision(stale)).toMatchObject({
      state: 'policy_compliance_receipt_stale',
      currentness: 'stale',
      receipt_ids: ['receipt-1'],
    });

    const unavailableBinding = binding({
      allowed: false,
      assessment_available: false,
      receipt_id: null,
      currentness: null,
      diagnostic_codes: ['policy_assessment_unavailable'],
    });
    const unavailable = decision({
      state: 'policy_assessment_unavailable',
      allowed: false,
      reason_codes: ['policy_assessment_unavailable'],
      receipt_ids: [],
      currentness: null,
      diagnostic_codes: ['policy_assessment_unavailable'],
      binding_decisions: [unavailableBinding],
    });
    expect(parsePolicyComplianceTransitionDecision(unavailable)).toMatchObject({
      state: 'policy_assessment_unavailable',
      allowed: false,
      receipt_ids: [],
      diagnostic_codes: ['policy_assessment_unavailable'],
    });
  });

  it('accepts available bindings without receipts when evidence is missing or inadmissible', () => {
    const missingBinding = binding({
      allowed: false,
      receipt_id: null,
      currentness: null,
      diagnostic_codes: ['policy_compliance_receipt_missing'],
    });
    const missing = decision({
      state: 'policy_compliance_receipt_missing',
      allowed: false,
      reason_codes: ['policy_compliance_receipt_missing'],
      receipt_ids: [],
      currentness: null,
      diagnostic_codes: ['policy_compliance_receipt_missing'],
      binding_decisions: [missingBinding],
    });
    expect(parsePolicyComplianceTransitionDecision(missing)).toMatchObject({
      state: 'policy_compliance_receipt_missing',
      allowed: false,
      receipt_ids: [],
      binding_decisions: [{
        assessment_available: true,
        receipt_id: null,
      }],
    });

    const inadmissibleBinding = binding({
      allowed: false,
      receipt_id: null,
      currentness: null,
      inadmissibility_cause: 'confidence_below_minimum',
      diagnostic_codes: ['policy_assessment_inadmissible'],
    });
    const inadmissible = decision({
      state: 'policy_compliance_blocked',
      allowed: false,
      reason_codes: ['policy_compliance_blocked'],
      receipt_ids: [],
      currentness: null,
      diagnostic_codes: ['policy_assessment_inadmissible'],
      binding_decisions: [inadmissibleBinding],
    });
    expect(parsePolicyComplianceTransitionDecision(inadmissible)).toMatchObject({
      state: 'policy_compliance_blocked',
      binding_decisions: [{
        assessment_available: true,
        inadmissibility_cause: 'confidence_below_minimum',
        receipt_id: null,
      }],
    });

    // Evidence presence is mandatory regardless of enforcement: an
    // applicable advisory binding without a receipt rejects the transition.
    const advisoryMissingBinding = binding({
      enforcement: 'advisory',
      allowed: false,
      receipt_id: null,
      currentness: null,
      advisory_issue_count: 3,
      diagnostic_codes: ['policy_compliance_receipt_missing'],
    });
    expect(parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_receipt_missing',
      allowed: false,
      reason_codes: ['policy_compliance_receipt_missing'],
      receipt_ids: [],
      currentness: null,
      applicable_blocking_metric_count: 0,
      advisory_issue_count: 3,
      diagnostic_codes: ['policy_compliance_receipt_missing'],
      binding_decisions: [advisoryMissingBinding],
    }))).toMatchObject({
      state: 'policy_compliance_receipt_missing',
      allowed: false,
      advisory_issue_count: 3,
    });
  });

  it.each([
    [
      'an advisory binding that rejects',
      binding({ enforcement: 'advisory', allowed: false }),
    ],
    [
      'an invalid context-only binding',
      binding({
        applicable_metric_count: 0,
        allowed: false,
        receipt_id: null,
        currentness: null,
      }),
    ],
    [
      'a skipped binding that also claims a waiver',
      binding({
        skipped: true,
        failed_metric_count: 1,
        waived_metric_count: 1,
        diagnostic_codes: ['policy_metric_threshold_failed'],
      }),
    ],
    [
      'an unavailable assessment carrying a receipt',
      binding({
        assessment_available: false,
        diagnostic_codes: ['policy_assessment_unavailable'],
      }),
    ],
    [
      'a missing receipt without its diagnostic',
      binding({
        receipt_id: null,
        currentness: null,
      }),
    ],
    [
      'inadmissibility without its exact diagnostic',
      binding({
        receipt_id: null,
        currentness: null,
        inadmissibility_cause: 'assessor_separation_required',
      }),
    ],
    [
      'a stale receipt without its exact diagnostic',
      binding({
        currentness: 'stale',
        currentness_reasons: ['subject_version_changed'],
      }),
    ],
    [
      'a forged current-receipt outcome',
      binding({
        failed_metric_count: 1,
        diagnostic_codes: ['policy_metric_threshold_failed'],
      }),
    ],
    [
      'non-canonical diagnostic ordering',
      binding({
        receipt_id: null,
        currentness: null,
        inadmissibility_cause: 'confidence_below_minimum',
        diagnostic_codes: [
          'policy_assessment_inadmissible',
          'policy_compliance_receipt_missing',
        ],
      }),
    ],
  ])('rejects %s', (_label, invalidBinding) => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      binding_decisions: [invalidBinding],
    }))).toThrow();
  });

  it('requires canonical aggregate bindings, counts, receipts, currentness, diagnostics, and reasons', () => {
    const missingBinding = binding({
      binding_id: 'binding-a',
      guideline_id: 'guideline-a',
      allowed: false,
      receipt_id: null,
      currentness: null,
      diagnostic_codes: ['policy_compliance_receipt_missing'],
    });
    const failedBinding = binding({
      binding_id: 'binding-b',
      guideline_id: 'guideline-b',
      receipt_id: 'receipt-2',
      allowed: false,
      failed_metric_count: 1,
      blocking_metric_count: 1,
      diagnostic_codes: ['policy_metric_threshold_failed'],
    });
    const canonical = decision({
      state: 'policy_compliance_receipt_missing',
      allowed: false,
      reason_codes: [
        'policy_compliance_receipt_missing',
        'policy_compliance_blocked',
      ],
      receipt_ids: ['receipt-2'],
      applicable_metric_count: 6,
      applicable_blocking_metric_count: 6,
      failed_metric_count: 1,
      blocking_metric_count: 1,
      diagnostic_codes: [
        'policy_compliance_receipt_missing',
        'policy_metric_threshold_failed',
      ],
      binding_decisions: [missingBinding, failedBinding],
    });

    expect(parsePolicyComplianceTransitionDecision(canonical)).toMatchObject({
      state: 'policy_compliance_receipt_missing',
      reason_codes: [
        'policy_compliance_receipt_missing',
        'policy_compliance_blocked',
      ],
      receipt_ids: ['receipt-2'],
      currentness: 'current',
      applicable_metric_count: 6,
    });
    expect(() => parsePolicyComplianceTransitionDecision({
      ...canonical,
      binding_decisions: [failedBinding, missingBinding],
    })).toThrow(/binding decisions are not canonical/i);
    expect(() => parsePolicyComplianceTransitionDecision({
      ...canonical,
      diagnostic_codes: ['policy_compliance_receipt_missing'],
    })).toThrow(/diagnostics do not match/i);
    expect(() => parsePolicyComplianceTransitionDecision({
      ...canonical,
      receipt_ids: ['receipt-forged'],
    })).toThrow(/receipts do not match/i);
    expect(() => parsePolicyComplianceTransitionDecision({
      ...canonical,
      currentness: 'stale',
      currentness_reasons: ['subject_version_changed'],
    })).toThrow(/currentness does not match/i);
    expect(() => parsePolicyComplianceTransitionDecision({
      ...canonical,
      allowed: true,
    })).toThrow(/outcome does not match/i);
    expect(() => parsePolicyComplianceTransitionDecision({
      ...canonical,
      state: 'policy_compliance_blocked',
      reason_codes: [
        'policy_compliance_blocked',
        'policy_compliance_receipt_missing',
      ],
    })).toThrow(/non-canonical reason codes/i);
  });

  it('accepts canonical ready-with-waiver, skipped, and not-applicable outcomes', () => {
    const waivedBinding = binding({
      allowed: true,
      failed_metric_count: 1,
      waived_metric_count: 1,
      diagnostic_codes: ['policy_metric_threshold_failed'],
    });
    expect(parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_ready_with_waivers',
      reason_codes: ['policy_compliance_ready_with_waivers'],
      failed_metric_count: 1,
      waived_metric_count: 1,
      diagnostic_codes: ['policy_metric_threshold_failed'],
      binding_decisions: [waivedBinding],
    }))).toMatchObject({
      state: 'policy_compliance_ready_with_waivers',
      waived_metric_count: 1,
    });

    const skippedBinding = binding({ skipped: true });
    expect(parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_ready_with_waivers',
      reason_codes: ['policy_compliance_ready_with_waivers'],
      skipped_binding_count: 1,
      binding_decisions: [skippedBinding],
    }))).toMatchObject({
      state: 'policy_compliance_ready_with_waivers',
      skipped_binding_count: 1,
    });

    const contextBinding = binding({
      applicable_metric_count: 0,
    });
    expect(parsePolicyComplianceTransitionDecision(decision({
      state: 'policy_compliance_not_applicable',
      reason_codes: ['policy_compliance_not_applicable'],
      applicable_metric_count: 0,
      applicable_blocking_metric_count: 0,
      binding_decisions: [contextBinding],
    }))).toMatchObject({
      state: 'policy_compliance_not_applicable',
      applicable_metric_count: 0,
    });
  });

  it('requires complete digests and counts for every scoped decision', () => {
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      decision_digest: null,
    }))).toThrow(/incomplete governed decision envelope/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      fence_digest: null,
    }))).toThrow(/incomplete governed decision envelope/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      applicable_metric_count: null,
    }))).toThrow(/partial metric count envelope/i);
    expect(() => parsePolicyComplianceTransitionDecision(decision({
      applicable_metric_count: null,
      applicable_blocking_metric_count: null,
      failed_metric_count: null,
      blocking_metric_count: null,
      waived_metric_count: null,
      advisory_issue_count: null,
      skipped_binding_count: null,
    }))).toThrow(/invalid unscoped subject-required decision/i);
  });

  it('accepts subject-required only without fabricated metric evidence', () => {
    const subjectRequired = decision({
      state: 'policy_subject_required',
      allowed: null,
      reason_codes: ['policy_subject_required'],
      decision_digest: null,
      fence_digest: null,
      receipt_ids: [],
      currentness: null,
      applicable_metric_count: null,
      applicable_blocking_metric_count: null,
      failed_metric_count: null,
      blocking_metric_count: null,
      waived_metric_count: null,
      advisory_issue_count: null,
      skipped_binding_count: null,
      binding_decisions: [],
    });

    expect(
      parsePolicyComplianceTransitionDecision(subjectRequired),
    ).toMatchObject({
      state: 'policy_subject_required',
      allowed: null,
      receipt_ids: [],
    });

    const scopedSubjectRequired = decision({
      state: 'policy_subject_required',
      allowed: false,
      reason_codes: ['policy_subject_required'],
      receipt_ids: [],
      currentness: null,
      applicable_metric_count: 0,
      applicable_blocking_metric_count: 0,
      failed_metric_count: 0,
      blocking_metric_count: 0,
      waived_metric_count: 0,
      advisory_issue_count: 0,
      skipped_binding_count: 0,
      binding_decisions: [],
    });
    expect(
      parsePolicyComplianceTransitionDecision(scopedSubjectRequired),
    ).toMatchObject({
      state: 'policy_subject_required',
      allowed: false,
      decision_digest: 'a'.repeat(64),
      fence_digest: 'b'.repeat(64),
      applicable_metric_count: 0,
    });

    expect(() => parsePolicyComplianceTransitionDecision({
      ...subjectRequired,
      receipt_ids: ['fabricated'],
      currentness: 'current',
    })).toThrow(/invalid unscoped subject-required decision/i);
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
        blocked_facts: null,
        preconditions: [],
        capabilities: [],
        effects: [],
        reason_codes: [],
        policy_compliance: false,
        policy_compliance_decision: decision(),
      },
    ])).toThrow(/non-enforcement transition/i);
  });

  it('admits only an authoritative ready edge while preserving recovery', () => {
    expect(isAllowedTransitionActionable(governed())).toBe(true);
    expect(isAllowedTransitionActionable(governed({
      policy_compliance_decision: blockedDecision(),
    }))).toBe(false);
    expect(isAllowedTransitionActionable({
      to_status: 'cancelled',
      label: 'Cancelled',
      gate: 'cancel',
      blocked_reason: null,
      blocked_facts: null,
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
      blocked_facts: null,
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

  it('accepts the server-owned structured blocker facts without inferring admission', () => {
    const structured = governed({
      blocked_reason: 'spec_dependencies_incomplete: complete prerequisites',
      blocked_facts: {
        spec_id: 'spec-1',
        blocking_count: 1,
        archived_blocking_count: 0,
        unfinished_blocking_count: 1,
        blockers_truncated: false,
        blocking_dependencies: [{
          dependency_id: 'dependency-1',
          target_spec_id: 'spec-0',
          target_title: 'Foundation',
          target_status: 'in_progress',
          target_archived: false,
        }],
      },
      policy_compliance: false,
      policy_compliance_decision: null,
    });

    expect(projectPolicyTransitions([structured]).ungoverned).toEqual([{
      toStatus: 'validated',
      label: 'Validated',
      gate: 'approved_to_validated',
    }]);
  });

  it.each([
    ['an array', []],
    ['a string', 'spec_dependencies_incomplete'],
  ])('rejects blocked facts represented as %s', (_label, blockedFacts) => {
    expect(() => projectPolicyTransitions([governed({
      blocked_facts: blockedFacts as unknown as Record<string, unknown>,
    })])).toThrow(/invalid blocked facts/i);
  });

  it('accepts only the closed redacted projection for coarse lifecycle admission', () => {
    const redactedReady = governed({
      policy_compliance_decision: {
        projection: 'redacted',
        state: 'policy_compliance_redacted',
        allowed: true,
        policy_compliance_required: true,
      },
    });
    const redactedBlocked = governed({
      blocked_reason: 'Policy Compliance blocks this transition.',
      blocked_facts: null,
      policy_compliance_decision: {
        projection: 'redacted',
        state: 'policy_compliance_redacted',
        allowed: false,
        policy_compliance_required: true,
      },
    });

    expect(isAllowedTransitionActionable(redactedReady)).toBe(true);
    expect(isAllowedTransitionActionable(redactedBlocked)).toBe(false);
    expect(() => projectPolicyTransitions([redactedReady])).toThrow(
      /details are not available/i,
    );
    expect(isAllowedTransitionActionable({
      ...redactedReady,
      policy_compliance_decision: {
        ...redactedReady.policy_compliance_decision,
        decision_digest: 'a'.repeat(64),
      },
    } as unknown as AllowedTransition)).toBe(false);
    expect(isAllowedTransitionActionable({
      ...redactedReady,
      policy_compliance_decision: {
        ...decision(),
        projection: 'redacted',
      },
    } as unknown as AllowedTransition)).toBe(false);

    const response = {
      board_id: 'board-1',
      entity_type: 'spec' as const,
      entity_id: 'spec-1',
      current_status: 'approved',
      source: 'core_sdlc_registry_v1' as const,
      allowed_transitions: [redactedReady],
    };
    expect(requirePolicyTransitionEnvelope(response, {
      boardId: 'board-1',
      entityType: 'spec',
      subjectId: 'spec-1',
      currentStatus: 'approved',
    })).toEqual(response.allowed_transitions);
  });

  it('binds the exact transition envelope to the active subject and authority', () => {
    const response = {
      board_id: 'board-1',
      entity_type: 'spec' as const,
      entity_id: 'spec-1',
      current_status: 'approved',
      source: 'core_sdlc_registry_v1' as const,
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
    } as never, expected)).toThrow(/does not match the active subject/i);
    expect(() => requirePolicyTransitionEnvelope({
      ...response,
      allowed_transitions: [
        governed(),
        governed({ policy_compliance: false, policy_compliance_decision: null }),
      ],
    }, expected)).toThrow(/repeats a lifecycle target/i);
  });

  it('preserves and scopes the exact semantic mutation rejection', () => {
    const rejectionBinding = binding({
      allowed: false,
      failed_metric_count: 1,
      blocking_metric_count: 1,
      diagnostic_codes: ['policy_metric_threshold_failed'],
    });
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
        receipt_ids: ['receipt-1'],
        currentness: 'current',
        currentness_reasons: [],
        counts: {
          applicable_metrics: 3,
          applicable_blocking_metrics: 3,
          failed_metrics: 1,
          blocking_metrics: 1,
          waived_metrics: 0,
          advisory_issues: 0,
          skipped_bindings: 0,
        },
        diagnostic_codes: ['policy_metric_threshold_failed'],
        binding_decisions: [rejectionBinding],
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
    const errorDetails = error.details as Record<string, unknown>;
    const errorCounts = errorDetails.counts as Record<string, unknown>;

    const rejection = parsePolicyTransitionRejection(error, expected);

    expect(rejection).toMatchObject({
      code: 'policy_compliance_blocked',
      decision: {
        receipt_ids: ['receipt-1'],
        currentness: 'current',
        blocking_metric_count: 1,
        diagnostic_codes: ['policy_metric_threshold_failed'],
      },
    });
    expect(policyTransitionRejectionMessage(rejection)).toContain(
      'receipts receipt-1 (current)',
    );
    const lifecycleMessage = policyTransitionRejectionMessage(
      rejection,
      'lifecycle-edition',
    );
    expect(lifecycleMessage).toContain('current edition result');
    expect(lifecycleMessage).not.toMatch(/receipt|currentness|stale|digest/i);
    expect(lifecycleMessage).not.toContain(rejection.code);
    expect(() => parsePolicyTransitionRejection(error, {
      ...expected,
      subjectId: 'spec-other',
    })).toThrow(/does not match the active action/i);
    expect(() => parsePolicyTransitionRejection(
      new AuthenticatedFetchError({
        status: 409,
        code: 'policy_compliance_blocked',
        message: 'policy_compliance_blocked',
        details: { ...errorDetails, unexpected: true },
      }),
      expected,
    )).toThrow(/unknown or missing rejection field/i);
    expect(() => parsePolicyTransitionRejection(
      new AuthenticatedFetchError({
        status: 409,
        code: 'policy_compliance_blocked',
        message: 'policy_compliance_blocked',
        details: {
          ...errorDetails,
          counts: {
            ...errorCounts,
            unexpected: true,
          },
        },
      }),
      expected,
    )).toThrow(/unknown or missing rejection count field/i);
  });
});
