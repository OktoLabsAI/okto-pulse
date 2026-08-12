import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type {
  AllowedTransition,
  PolicyComplianceTransitionDecision,
} from '@/types';

import { PolicyComplianceTransitionPreview } from '../PolicyComplianceTransitionPreview';

function semanticDecision(
  overrides: Partial<PolicyComplianceTransitionDecision> = {},
): PolicyComplianceTransitionDecision {
  const receiptId = overrides.receipt_ids?.[0] ?? 'receipt-authoritative';
  return {
    state: 'policy_compliance_ready',
    allowed: true,
    policy_compliance_required: true,
    reason_codes: ['policy_compliance_ready'],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_ids: [receiptId],
    currentness: 'current',
    currentness_reasons: [],
    applicable_metric_count: 4,
    applicable_blocking_metric_count: 4,
    failed_metric_count: 0,
    blocking_metric_count: 0,
    waived_metric_count: 0,
    advisory_issue_count: 0,
    skipped_binding_count: 0,
    diagnostic_codes: [],
    binding_decisions: [{
      binding_id: 'binding-hexagonal',
      guideline_id: 'guideline-hexagonal',
      enforcement: 'blocking',
      applicable_metric_count: 4,
      allowed: true,
      assessment_available: true,
      receipt_id: receiptId,
      currentness: 'current',
      currentness_reasons: [],
      inadmissibility_cause: null,
      failed_metric_count: 0,
      waived_metric_count: 0,
      blocking_metric_count: 0,
      advisory_issue_count: 0,
      skipped: false,
      diagnostic_codes: [],
    }],
    ...overrides,
    projection: overrides.projection ?? 'full',
  };
}

function governed(): AllowedTransition {
  return {
    to_status: 'done',
    label: 'Done',
    gate: 'approved_to_done',
    blocked_reason: null,
    preconditions: [],
    capabilities: [],
    effects: [],
    reason_codes: [],
    policy_compliance: true,
    policy_compliance_decision: semanticDecision({
      state: 'policy_compliance_ready_with_waivers',
      allowed: true,
      reason_codes: ['policy_compliance_ready_with_waivers'],
      failed_metric_count: 1,
      waived_metric_count: 1,
      diagnostic_codes: ['policy_metric_threshold_failed'],
      binding_decisions: [{
        binding_id: 'binding-hexagonal',
        guideline_id: 'guideline-hexagonal',
        enforcement: 'blocking',
        applicable_metric_count: 4,
        allowed: true,
        assessment_available: true,
        receipt_id: 'receipt-authoritative',
        currentness: 'current',
        currentness_reasons: [],
        inadmissibility_cause: null,
        failed_metric_count: 1,
        waived_metric_count: 1,
        blocking_metric_count: 0,
        advisory_issue_count: 0,
        skipped: false,
        diagnostic_codes: ['policy_metric_threshold_failed'],
      }],
    }),
  };
}

describe('PolicyComplianceTransitionPreview', () => {
  it('renders the backend receipt and preserves non-policy recovery semantics', () => {
    render(
      <PolicyComplianceTransitionPreview
        preview={{
          status: 'ready',
          error: null,
          transitions: [
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
          ],
        }}
      />,
    );

    expect(screen.getByText('To Done')).toBeInTheDocument();
    expect(screen.getByText('receipt-authoritative')).toBeInTheDocument();
    expect(screen.getByText('Ready')).toBeInTheDocument();
    expect(screen.getByTestId('policy-transition-done'))
      .toHaveTextContent('1/4 failed · current');
    expect(
      screen.getByText(/Policy Compliance does not block.*Cancelled/s),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/own lifecycle and recovery gates still apply/i),
    ).toBeInTheDocument();
  });

  it('fails closed instead of inferring admission from malformed metadata', () => {
    render(
      <PolicyComplianceTransitionPreview
        preview={{
          status: 'ready',
          error: null,
          transitions: [{
            to_status: 'done',
            label: 'Done',
            gate: 'approved_to_done',
          } as unknown as AllowedTransition],
        }}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent(
      /unknown or missing transition field/i,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      /No admission is inferred/i,
    );
  });

  it('distinguishes loading and transport failure', () => {
    const { rerender } = render(
      <PolicyComplianceTransitionPreview
        preview={{
          status: 'loading',
          transitions: [],
          error: null,
        }}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(/Loading/i);

    rerender(
      <PolicyComplianceTransitionPreview
        preview={{
          status: 'error',
          transitions: [],
          error: 'Network boundary failed.',
        }}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      /Network boundary failed/i,
    );
  });

  it('keeps the authoritative mutation rejection visible beside the refreshed preview', () => {
    render(
      <PolicyComplianceTransitionPreview
        preview={{
          status: 'ready',
          error: null,
          transitions: [governed()],
        }}
        rejection={{
          code: 'policy_compliance_blocked',
          message: 'policy_compliance_blocked',
          entityType: 'spec',
          subjectId: 'spec-1',
          fromStatus: 'approved',
          toStatus: 'validated',
          decision: semanticDecision({
            state: 'policy_compliance_blocked',
            allowed: false,
            reason_codes: ['policy_compliance_blocked'],
            decision_digest: 'c'.repeat(64),
            fence_digest: 'd'.repeat(64),
            receipt_ids: ['receipt-race'],
            applicable_metric_count: 2,
            applicable_blocking_metric_count: 2,
            failed_metric_count: 1,
            blocking_metric_count: 1,
            diagnostic_codes: ['policy_metric_threshold_failed'],
            binding_decisions: [{
              binding_id: 'binding-hexagonal',
              guideline_id: 'guideline-hexagonal',
              enforcement: 'blocking',
              applicable_metric_count: 2,
              allowed: false,
              assessment_available: true,
              receipt_id: 'receipt-race',
              currentness: 'current',
              currentness_reasons: [],
              inadmissibility_cause: null,
              failed_metric_count: 1,
              waived_metric_count: 0,
              blocking_metric_count: 1,
              advisory_issue_count: 0,
              skipped: false,
              diagnostic_codes: ['policy_metric_threshold_failed'],
            }],
          }),
        }}
      />,
    );

    expect(screen.getByTestId('policy-transition-rejection'))
      .toHaveTextContent('policy_compliance_blocked');
    expect(screen.getByTestId('policy-transition-rejection'))
      .toHaveTextContent('receipt-race');
    expect(screen.getByTestId('policy-transition-done'))
      .toHaveTextContent('receipt-authoritative');
  });

  it('projects lifecycle-edition readiness without technical record or stale noise', () => {
    const previousEditionDecision = semanticDecision({
      state: 'policy_compliance_receipt_stale',
      allowed: false,
      reason_codes: ['policy_compliance_receipt_stale'],
      currentness: 'stale',
      currentness_reasons: ['subject_version_changed'],
      diagnostic_codes: ['policy_compliance_receipt_stale'],
      binding_decisions: [{
        ...semanticDecision().binding_decisions[0],
        allowed: false,
        currentness: 'stale',
        currentness_reasons: ['subject_version_changed'],
        diagnostic_codes: ['policy_compliance_receipt_stale'],
      }],
    });
    const { container } = render(
      <PolicyComplianceTransitionPreview
        presentationMode="lifecycle-edition"
        preview={{
          status: 'ready',
          error: null,
          transitions: [{
            ...governed(),
            policy_compliance_decision: previousEditionDecision,
          }],
        }}
        rejection={{
          code: 'policy_compliance_receipt_stale',
          message: 'policy_compliance_receipt_stale',
          entityType: 'ideation',
          subjectId: 'ideation-1',
          fromStatus: 'evaluating',
          toStatus: 'done',
          decision: previousEditionDecision,
        }}
      />,
    );

    expect(screen.getByTestId('policy-transition-rejection'))
      .toHaveTextContent('The last transition attempt needs attention.');
    expect(screen.getByTestId('policy-transition-done'))
      .toHaveTextContent('Result: Previous edition');
    expect(screen.getByTestId('policy-transition-done'))
      .toHaveTextContent('Needs attention');
    expect(container).not.toHaveTextContent(/receipt|currentness|stale|digest/i);
    expect(container).not.toHaveTextContent(/policy_compliance_/i);
  });

  it('redacts technical transport and contract details in lifecycle-edition errors', () => {
    const { rerender } = render(
      <PolicyComplianceTransitionPreview
        presentationMode="lifecycle-edition"
        preview={{
          status: 'error',
          transitions: [],
          error: 'stale receipt digest mismatch',
        }}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Lifecycle readiness could not be loaded. Try again.',
    );
    expect(screen.getByRole('alert'))
      .not.toHaveTextContent(/receipt|stale|digest/i);

    rerender(
      <PolicyComplianceTransitionPreview
        presentationMode="lifecycle-edition"
        preview={{
          status: 'ready',
          error: null,
          transitions: [{
            to_status: 'done',
            label: 'Done',
            gate: 'approved_to_done',
          } as unknown as AllowedTransition],
        }}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Lifecycle readiness could not be verified.',
    );
    expect(screen.getByRole('alert'))
      .not.toHaveTextContent(/receipt|currentness|decision field|transition field/i);
  });

  it('renders every semantic gate outcome as a distinct accessible theme-aware state', () => {
    const transition = (
      decision: PolicyComplianceTransitionDecision,
    ): AllowedTransition => ({
      ...governed(),
      policy_compliance_decision: decision,
    });
    const preview = (decision: PolicyComplianceTransitionDecision) => ({
      status: 'ready' as const,
      error: null,
      transitions: [transition(decision)],
    });

    const contextOnly = semanticDecision({
      state: 'policy_compliance_not_applicable',
      reason_codes: ['policy_compliance_not_applicable'],
      applicable_metric_count: 0,
      applicable_blocking_metric_count: 0,
      binding_decisions: [{
        ...semanticDecision().binding_decisions[0],
        applicable_metric_count: 0,
      }],
    });
    const advisoryFailure = semanticDecision({
      state: 'policy_compliance_advisory_only',
      reason_codes: ['policy_compliance_advisory_only'],
      applicable_blocking_metric_count: 0,
      failed_metric_count: 1,
      advisory_issue_count: 1,
      diagnostic_codes: ['policy_metric_threshold_failed'],
      binding_decisions: [{
        ...semanticDecision().binding_decisions[0],
        enforcement: 'advisory',
        failed_metric_count: 1,
        advisory_issue_count: 1,
        diagnostic_codes: ['policy_metric_threshold_failed'],
      }],
    });
    const blockingFailure = semanticDecision({
      state: 'policy_compliance_blocked',
      allowed: false,
      reason_codes: ['policy_compliance_blocked'],
      failed_metric_count: 1,
      blocking_metric_count: 1,
      diagnostic_codes: ['policy_metric_threshold_failed'],
      binding_decisions: [{
        ...semanticDecision().binding_decisions[0],
        allowed: false,
        failed_metric_count: 1,
        blocking_metric_count: 1,
        diagnostic_codes: ['policy_metric_threshold_failed'],
      }],
    });
    const waivedFailure = governed().policy_compliance_decision;
    if (!waivedFailure || waivedFailure.projection !== 'full') {
      throw new Error('Expected a full Policy Compliance fixture.');
    }
    const skippedBinding = semanticDecision({
      state: 'policy_compliance_ready_with_waivers',
      reason_codes: ['policy_compliance_ready_with_waivers'],
      skipped_binding_count: 1,
      binding_decisions: [{
        ...semanticDecision().binding_decisions[0],
        skipped: true,
      }],
    });
    const lowConfidence = semanticDecision({
      state: 'policy_compliance_blocked',
      allowed: false,
      reason_codes: ['policy_compliance_blocked'],
      receipt_ids: [],
      currentness: null,
      diagnostic_codes: ['policy_assessment_inadmissible'],
      binding_decisions: [{
        ...semanticDecision().binding_decisions[0],
        allowed: false,
        receipt_id: null,
        currentness: null,
        inadmissibility_cause: 'confidence_below_minimum',
        diagnostic_codes: ['policy_assessment_inadmissible'],
      }],
    });

    const { rerender } = render(
      <PolicyComplianceTransitionPreview preview={preview(contextOnly)} />,
    );
    let state = screen.getByTestId('policy-transition-done');
    expect(state).toHaveTextContent('policy_compliance_not_applicable');
    expect(state).toHaveTextContent('Ready');
    expect(state).toHaveClass('dark:border-emerald-800');

    rerender(
      <PolicyComplianceTransitionPreview preview={preview(advisoryFailure)} />,
    );
    state = screen.getByTestId('policy-transition-done');
    expect(state).toHaveTextContent('policy_compliance_advisory_only');
    expect(state).toHaveTextContent('Advisory issues: 1');

    rerender(
      <PolicyComplianceTransitionPreview preview={preview(blockingFailure)} />,
    );
    state = screen.getByTestId('policy-transition-done');
    expect(state).toHaveTextContent('policy_compliance_blocked');
    expect(state).toHaveTextContent('Blocked');
    expect(state).toHaveClass('dark:border-red-800');

    rerender(
      <PolicyComplianceTransitionPreview preview={preview(waivedFailure)} />,
    );
    state = screen.getByTestId('policy-transition-done');
    expect(state).toHaveTextContent('policy_compliance_ready_with_waivers');
    expect(state).toHaveTextContent('Waived metrics: 1');

    rerender(
      <PolicyComplianceTransitionPreview preview={preview(skippedBinding)} />,
    );
    state = screen.getByTestId('policy-transition-done');
    expect(state).toHaveTextContent('Skipped bindings: 1');
    expect(state).toHaveTextContent('human skip active');

    rerender(
      <PolicyComplianceTransitionPreview preview={preview(lowConfidence)} />,
    );
    state = screen.getByTestId('policy-transition-done');
    expect(state).toHaveTextContent('confidence_below_minimum');
    expect(state).toHaveTextContent('Receipts: none');
    expect(state).toHaveTextContent('Blocked');
  });
});
