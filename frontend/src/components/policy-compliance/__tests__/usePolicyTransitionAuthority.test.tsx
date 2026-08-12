import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import type {
  AllowedTransition,
  AllowedTransitionsResponse,
  PolicyComplianceBindingDecision,
  PolicyComplianceTransitionDecision,
} from '@/types';

import { usePolicyTransitionAuthority } from '../usePolicyTransitionAuthority';

const apiMock = {
  getAllowedTransitions: vi.fn(),
};

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

function transition(
  overrides: Partial<AllowedTransition> = {},
): AllowedTransition {
  return {
    to_status: 'done',
    label: 'Done',
    gate: 'card_to_done',
    blocked_reason: null,
    preconditions: [],
    capabilities: [],
    effects: [],
    reason_codes: [],
    policy_compliance: false,
    policy_compliance_decision: null,
    ...overrides,
  };
}

function semanticBinding(
  overrides: Partial<PolicyComplianceBindingDecision> = {},
): PolicyComplianceBindingDecision {
  return {
    binding_id: 'binding-card-quality',
    guideline_id: 'guideline-card-quality',
    enforcement: 'blocking',
    applicable_metric_count: 2,
    allowed: true,
    assessment_available: true,
    receipt_id: 'receipt-card-1',
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

function semanticDecision(
  overrides: Partial<PolicyComplianceTransitionDecision> = {},
): PolicyComplianceTransitionDecision {
  return {
    state: 'policy_compliance_ready',
    allowed: true,
    policy_compliance_required: true,
    reason_codes: ['policy_compliance_ready'],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_ids: ['receipt-card-1'],
    currentness: 'current',
    currentness_reasons: [],
    applicable_metric_count: 2,
    applicable_blocking_metric_count: 2,
    failed_metric_count: 0,
    blocking_metric_count: 0,
    waived_metric_count: 0,
    advisory_issue_count: 0,
    skipped_binding_count: 0,
    diagnostic_codes: [],
    binding_decisions: [semanticBinding()],
    ...overrides,
    projection: overrides.projection ?? 'full',
  };
}

function envelope(
  subjectId: string,
  transitions: AllowedTransition[] = [transition()],
): AllowedTransitionsResponse {
  return {
    board_id: 'board-1',
    entity_type: 'card',
    entity_id: subjectId,
    current_status: 'in_progress',
    allowed_transitions: transitions,
    source: 'core_sdlc_registry_v1',
  };
}

function blockedRejection(): AuthenticatedFetchError {
  return new AuthenticatedFetchError({
    status: 409,
    code: 'policy_compliance_blocked',
    message: 'policy_compliance_blocked',
    details: {
      outcome: 'error',
      error: 'policy_compliance_blocked',
      code: 'policy_compliance_blocked',
      message: 'Blocking guideline failed.',
      reason_codes: ['policy_compliance_blocked'],
      decision_digest: 'a'.repeat(64),
      fence_digest: 'b'.repeat(64),
      receipt_ids: ['receipt-card-1'],
      currentness: 'current',
      currentness_reasons: [],
      counts: {
        applicable_metrics: 2,
        applicable_blocking_metrics: 2,
        failed_metrics: 1,
        blocking_metrics: 1,
        waived_metrics: 0,
        advisory_issues: 0,
        skipped_bindings: 0,
      },
      diagnostic_codes: ['policy_metric_threshold_failed'],
      binding_decisions: [semanticBinding({
        allowed: false,
        failed_metric_count: 1,
        blocking_metric_count: 1,
        diagnostic_codes: ['policy_metric_threshold_failed'],
      })],
      transition: {
        entity_type: 'card',
        subject_id: 'card-1',
        from_status: 'in_progress',
        to_status: 'done',
      },
      policy_compliance_required: true,
    },
  });
}

describe('usePolicyTransitionAuthority', () => {
  beforeEach(() => {
    apiMock.getAllowedTransitions.mockReset();
  });

  it('admits only actionable transitions from a valid canonical envelope', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue(envelope('card-1', [
      transition({
        policy_compliance: true,
        policy_compliance_decision: semanticDecision(),
      }),
      transition({
        to_status: 'cancelled',
        label: 'Cancelled',
        gate: 'cancel',
        blocked_reason: 'Cancellation reason required.',
      }),
    ]));

    const { result } = renderHook(() => usePolicyTransitionAuthority({
      boardId: 'board-1',
      entityType: 'card',
      subjectId: 'card-1',
      currentStatus: 'in_progress',
    }));

    await waitFor(() => expect(result.current.preview.status).toBe('ready'));
    expect(result.current.transitions).toHaveLength(2);
    expect(result.current.actionableTransitions.map((item) => item.to_status))
      .toEqual(['done']);
  });

  it('preserves coarse lifecycle admission when policy details are redacted', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue(envelope('card-1', [
      transition({
        policy_compliance: true,
        policy_compliance_decision: {
          projection: 'redacted',
          state: 'policy_compliance_redacted',
          allowed: true,
          policy_compliance_required: true,
        },
      }),
    ]));

    const { result } = renderHook(() => usePolicyTransitionAuthority({
      boardId: 'board-1',
      entityType: 'card',
      subjectId: 'card-1',
      currentStatus: 'in_progress',
    }));

    await waitFor(() => expect(result.current.preview.status).toBe('ready'));
    expect(result.current.actionableTransitions.map((item) => item.to_status))
      .toEqual(['done']);
  });

  it('fails closed when semantic authority contains an unknown field', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue(envelope('card-1', [
      transition({
        policy_compliance: true,
        policy_compliance_decision: {
          ...semanticDecision(),
          future_authority: true,
        } as unknown as PolicyComplianceTransitionDecision,
      }),
    ]));

    const { result } = renderHook(() => usePolicyTransitionAuthority({
      boardId: 'board-1',
      entityType: 'card',
      subjectId: 'card-1',
      currentStatus: 'in_progress',
    }));

    await waitFor(() => expect(result.current.preview.status).toBe('error'));
    expect(result.current.actionableTransitions).toEqual([]);
    expect(result.current.preview.error).toMatch(/unknown or missing decision field/i);
  });

  it('fails closed when the authority envelope does not match the subject', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue(envelope('card-other'));

    const { result } = renderHook(() => usePolicyTransitionAuthority({
      boardId: 'board-1',
      entityType: 'card',
      subjectId: 'card-1',
      currentStatus: 'in_progress',
    }));

    await waitFor(() => expect(result.current.preview.status).toBe('error'));
    expect(result.current.actionableTransitions).toEqual([]);
    expect(result.current.preview).toMatchObject({
      status: 'error',
      transitions: [],
    });
  });

  it('ignores a late response from a previously selected subject', async () => {
    let resolveFirst!: (value: AllowedTransitionsResponse) => void;
    let resolveSecond!: (value: AllowedTransitionsResponse) => void;
    apiMock.getAllowedTransitions
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveFirst = resolve;
      }))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveSecond = resolve;
      }));

    const { result, rerender } = renderHook(
      ({ subjectId }) => usePolicyTransitionAuthority({
        boardId: 'board-1',
        entityType: 'card',
        subjectId,
        currentStatus: 'in_progress',
      }),
      { initialProps: { subjectId: 'card-1' } },
    );

    rerender({ subjectId: 'card-2' });
    await act(async () => {
      resolveSecond(envelope('card-2', [
        transition({ to_status: 'on_hold', label: 'On hold' }),
      ]));
    });
    await waitFor(() => expect(
      result.current.actionableTransitions[0]?.to_status,
    ).toBe('on_hold'));

    await act(async () => {
      resolveFirst(envelope('card-1'));
    });
    expect(result.current.actionableTransitions[0]?.to_status).toBe('on_hold');
  });

  it('refreshes authority and preserves a matching structured 409', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue(envelope('card-1'));

    const { result } = renderHook(() => usePolicyTransitionAuthority({
      boardId: 'board-1',
      entityType: 'card',
      subjectId: 'card-1',
      currentStatus: 'in_progress',
    }));
    await waitFor(() => expect(result.current.preview.status).toBe('ready'));

    let handled = false;
    await act(async () => {
      handled = await result.current.handleTransitionError(
        blockedRejection(),
        'done',
      );
    });

    expect(handled).toBe(true);
    expect(apiMock.getAllowedTransitions).toHaveBeenCalledTimes(2);
    expect(result.current.rejection).toMatchObject({
      code: 'policy_compliance_blocked',
      subjectId: 'card-1',
      toStatus: 'done',
    });

    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.rejection).toMatchObject({
      code: 'policy_compliance_blocked',
      subjectId: 'card-1',
    });

    act(() => result.current.clearRejection());
    expect(result.current.rejection).toBeNull();
  });
});
