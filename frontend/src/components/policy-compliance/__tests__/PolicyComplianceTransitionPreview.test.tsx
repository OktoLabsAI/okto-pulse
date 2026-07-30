import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AllowedTransition } from '@/types';

import { PolicyComplianceTransitionPreview } from '../PolicyComplianceTransitionPreview';

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
    policy_compliance_decision: {
      state: 'policy_compliance_ready_with_waivers',
      allowed: true,
      policy_compliance_required: true,
      reason_codes: ['policy_compliance_ready_with_waivers'],
      decision_digest: 'a'.repeat(64),
      fence_digest: 'b'.repeat(64),
      receipt_id: 'receipt-authoritative',
      currentness: 'current',
      currentness_reasons: [],
      applicable_rule_count: 4,
      applicable_blocking_rule_count: 2,
      blocking_rule_count: 0,
      waived_rule_count: 1,
      advisory_issue_count: 1,
    },
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
      /metadata is unavailable/i,
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
          decision: {
            state: 'policy_compliance_blocked',
            allowed: false,
            policy_compliance_required: true,
            reason_codes: ['policy_compliance_blocked'],
            decision_digest: 'c'.repeat(64),
            fence_digest: 'd'.repeat(64),
            receipt_id: 'receipt-race',
            currentness: 'current',
            currentness_reasons: [],
            applicable_rule_count: 2,
            applicable_blocking_rule_count: 1,
            blocking_rule_count: 1,
            waived_rule_count: 0,
            advisory_issue_count: 0,
          },
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
});
