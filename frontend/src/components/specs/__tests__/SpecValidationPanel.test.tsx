import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SpecValidationPanel } from '../SpecValidationPanel';

const apiMock = vi.hoisted(() => ({
  getValidationCycle: vi.fn(),
  getValidationTechnicalAudit: vi.fn(),
  getSpecChecklistState: vi.fn(),
  getCurrentSpecValidation: vi.fn(),
  listSpecValidations: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/components/quality', () => ({
  QualityPanel: () => <div data-testid="quality-detail" />,
}));

vi.mock('../SpecChecklistPanel', () => ({
  SpecChecklistPanel: () => (
    <div data-testid="checklist-detail">
      <input aria-label="Cached checklist draft" defaultValue="draft" />
    </div>
  ),
}));

vi.mock('../SpecValidationHistoryPanel', async () => {
  const React = await import('react');
  return {
    SpecValidationHistoryPanel: ({ view }: { view: 'current' | 'previous' }) => {
      React.useEffect(() => {
        if (view === 'current') {
          void apiMock.getCurrentSpecValidation('spec-1');
        } else {
          void apiMock.listSpecValidations('spec-1');
        }
      }, [view]);
      return <div data-testid={`validation-detail-${view}`} />;
    },
  };
});

vi.mock('@/components/policy-compliance', () => ({
  PolicyCompliancePanel: ({
    lifecycleSnapshot,
  }: {
    lifecycleSnapshot?: { applicable_bindings: unknown[] } | null;
  }) => (
    <div
      data-testid="policy-detail"
      data-applicable-bindings={lifecycleSnapshot?.applicable_bindings.length ?? 0}
    />
  ),
  projectPolicyTransitions: (transitions: Array<{
    policy_compliance?: boolean;
    policy_compliance_decision?: { allowed: boolean | null } | null;
  }>) => ({
    governed: transitions
      .filter((transition) => transition.policy_compliance)
      .map((transition) => ({ decision: transition.policy_compliance_decision })),
    ungoverned: transitions.filter((transition) => !transition.policy_compliance),
  }),
}));

const checklistFromEditionOne = {
  status: 'current',
  subject: { spec_edition: 1 },
  binding: { mode: 'blocking' },
  current_receipt: { spec_edition: 1 },
  gate: { allowed: true },
};

const failedValidation = {
  id: 'validation-2',
  spec_id: 'spec-1',
  board_id: 'board-1',
  reviewer_id: 'reviewer-1',
  reviewer_name: 'Reviewer',
  completeness: 70,
  completeness_justification: 'Missing required detail.',
  assertiveness: 70,
  assertiveness_justification: 'Statements remain tentative.',
  ambiguity: 40,
  ambiguity_justification: 'Important behavior is unclear.',
  general_justification: 'The current edition needs another pass.',
  recommendation: 'reject',
  outcome: 'failed',
  threshold_violations: ['Completeness must be at least 80.'],
  created_at: '2026-08-11T12:00:00Z',
  edition: 2,
  lifecycle_state: 'current',
};

const failedCycle = {
  subject_type: 'spec',
  subject_id: 'spec-1',
  edition: 2,
  subject_status: 'validated',
  visible_sections: [
    'spec_validation',
    'requirement_lint',
    'curated_checklist',
    'policy_compliance',
  ],
  cycle_state: 'completed',
  current_result: {
    result_id: 'validation-2',
    result_type: 'spec_validation',
    subject_edition: 2,
    status: 'failed',
    summary: { recommendation: 'reject' },
  },
  previous_result_count: 3,
  previous_results: [],
  submission_fence: {
    expected_validation_edition: 2,
    expected_subject_version: 8,
    expected_head_revision: 1,
  },
  checks: [
    { result_type: 'requirement_lint', status: 'not_started', summary: 'Not started', details: {} },
    { result_type: 'curated_checklist', status: 'not_started', summary: 'Not started', details: {} },
    {
      result_type: 'policy_compliance',
      status: 'needs_attention',
      summary: 'Needs attention',
      details: {
        counts: {
          applicable: 1,
          completed: 1,
          passed: 0,
          failed: 1,
          waived: 0,
          skipped: 0,
          pending: 0,
          context_only: 0,
          inconsistent: 0,
          scope_inconsistent: 0,
          blocking: 1,
          advisory: 0,
          blocking_failed: 1,
          blocking_pending: 0,
          advisory_failed: 0,
          advisory_pending: 0,
          failed_metrics: 1,
          waived_metrics: 0,
          unwaived_failed_metrics: 1,
        },
        applicable_bindings: [{
          binding_id: 'binding-blocking',
          guideline_id: 'guideline-blocking',
          revision_id: 'revision-blocking',
          title: 'Blocking policy',
          enforcement: 'blocking',
          minimum_confidence: 80,
          status: 'failed',
          failed_metric_count: 1,
          waived_metric_count: 0,
          unwaived_failed_metric_count: 1,
          metrics: [{
            metric_id: 'metric-blocking',
            code: 'quality.blocking',
            title: 'Blocking metric',
            description: 'Frozen description.',
            description_truncated: false,
            evaluation_rubric: 'Frozen rubric.',
            evaluation_rubric_truncated: false,
            assessment_outcome: 'failed',
            direction: 'minimum',
            default_threshold: 80,
            effective_threshold: 80,
            threshold_source: 'default',
          }],
        }],
      },
    },
  ],
  remaining_actions: ['record_requirement_lint'],
};

const blockedPolicyPreview = {
  status: 'ready',
  transitions: [{
    policy_compliance: true,
    policy_compliance_decision: { allowed: false },
  }],
  error: null,
} as any;

const lastTransitionPolicyRejection = {
  code: 'policy_compliance_blocked',
  message: 'The last transition attempt was blocked.',
  entityType: 'spec',
  subjectId: 'spec-1',
  fromStatus: 'approved',
  toStatus: 'validated',
  decision: {
    blocking_metric_count: 1,
    currentness: 'current',
  },
} as unknown as NonNullable<
  ComponentProps<typeof SpecValidationPanel>['policyTransitionRejection']
>;

function renderPanel(
  overrides: Partial<ComponentProps<typeof SpecValidationPanel>> = {},
) {
  return render(
    <SpecValidationPanel
      boardId="board-1"
      specId="spec-1"
      specVersion={8}
      specEdition={2}
      specStatus="validated"
      canReadChecklist
      canExecuteChecklist
      canReadValidation
      canReadQuality
      canReadPolicyCompliance
      policyTransitionPreview={blockedPolicyPreview}
      specArchived={false}
      {...overrides}
    />,
  );
}

function summaryState(testId: string): string | null {
  return screen.getByTestId(testId).querySelector('[data-state]')
    ?.getAttribute('data-state') ?? null;
}

describe('SpecValidationPanel lifecycle edition projection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getValidationCycle.mockResolvedValue(failedCycle);
    apiMock.getValidationTechnicalAudit.mockResolvedValue({
      subject_type: 'spec',
      subject_id: 'spec-1',
      result_id: 'validation-2',
      result_type: 'spec_validation',
      subject_edition: 2,
      technical_audit: {
        receipt_id: 'validation-receipt-2',
        subject_version: 8,
        head_revision: 1,
        digests: {},
        visible_exception_types: [],
        exceptions: [],
      },
    });
    apiMock.getSpecChecklistState.mockResolvedValue(checklistFromEditionOne);
    apiMock.getCurrentSpecValidation.mockResolvedValue({
      spec_id: 'spec-1',
      edition: 2,
      lifecycle_state: 'current',
      current_validation: failedValidation,
      previous_count: 3,
    });
    apiMock.listSpecValidations.mockResolvedValue({
      spec_id: 'spec-1',
      current_validation_id: 'validation-2',
      validations: [],
    });
  });

  it('renders the four accessible subtabs in product order and keeps current-edition states', async () => {
    renderPanel();

    const tabs = within(screen.getByRole('tablist', {
      name: 'Spec validation sections',
    })).getAllByRole('tab');
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      'Spec Validation',
      'Checklist',
      'Requirement lint',
      'Policy Compliance',
    ]);
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(
      screen.getByTestId('spec-validation-current').querySelector('[data-state]'),
    ).toHaveAttribute('data-state', 'failed'));

    fireEvent.click(tabs[1]);
    expect(summaryState('spec-validation-checklist-summary')).toBe('not_started');
    fireEvent.click(tabs[2]);
    expect(summaryState('spec-validation-lint-summary')).toBe('not_started');
    fireEvent.click(tabs[3]);
    expect(summaryState('spec-validation-policy-summary')).toBe('needs_attention');
    expect(screen.queryByTestId('policy-audit')).not.toBeInTheDocument();
    expect(screen.queryByText(/stale/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/receipt/i)).not.toBeInTheDocument();
    expect(apiMock.getValidationCycle).toHaveBeenCalledTimes(1);
    expect(apiMock.getSpecChecklistState).not.toHaveBeenCalled();
    expect(apiMock.getCurrentSpecValidation).toHaveBeenCalledTimes(1);
    expect(apiMock.getValidationTechnicalAudit).not.toHaveBeenCalled();
  });

  it('does not infer Failed when a post-validation Spec has no current result', async () => {
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      subject_status: 'validated',
      edition: 2,
      cycle_state: 'completed',
      current_result: null,
      previous_result_count: 2,
    });

    renderPanel({
      canReadChecklist: false,
      canReadQuality: false,
      canReadPolicyCompliance: false,
    });

    await waitFor(() => expect(
      screen.getByTestId('spec-validation-current').querySelector('[data-state]'),
    ).toHaveAttribute('data-state', 'needs_attention'));
  });

  it('describes a legacy completed state as recorded instead of needing attention', async () => {
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      current_result: {
        ...failedCycle.current_result,
        status: 'completed',
      },
    });

    renderPanel({
      canReadChecklist: false,
      canReadQuality: false,
      canReadPolicyCompliance: false,
    });

    expect(await screen.findByText('Edition 2 validation is recorded'))
      .toBeInTheDocument();
    expect(screen.queryByText('Edition 2 needs attention'))
      .not.toBeInTheDocument();
    expect(
      screen.getByTestId('spec-validation-current').querySelector('[data-state]'),
    ).toHaveAttribute('data-state', 'completed');
  });

  it('keeps validation results and technical audit hidden from a checklist-only reader', async () => {
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      current_result: null,
      previous_result_count: 0,
      previous_results: [],
      checks: [failedCycle.checks[1]],
      remaining_actions: ['run_curated_checklist'],
    });

    renderPanel({
      canReadValidation: false,
      canReadQuality: false,
      canReadPolicyCompliance: false,
    });

    const checklistTab = await screen.findByRole('tab', { name: 'Checklist' });
    expect(checklistTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('checklist-detail')).toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-current')).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-previous-toggle')).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-technical-audit-toggle'))
      .not.toBeInTheDocument();
    expect(apiMock.getCurrentSpecValidation).not.toHaveBeenCalled();
    expect(apiMock.listSpecValidations).not.toHaveBeenCalled();
    expect(apiMock.getValidationTechnicalAudit).not.toHaveBeenCalled();
  });

  it('does not expose Policy Compliance when its read authority is absent', async () => {
    renderPanel({ canReadPolicyCompliance: false });
    await screen.findByTestId('spec-validation-current');
    expect(screen.queryByRole('tab', { name: 'Policy Compliance' }))
      .not.toBeInTheDocument();
    expect(screen.queryByTestId('policy-audit')).not.toBeInTheDocument();
  });

  it('presents a disabled checklist as Not required instead of Needs attention', async () => {
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      checks: failedCycle.checks.map((check) => check.result_type === 'curated_checklist'
        ? { ...check, status: 'off', summary: 'Not required' }
        : check),
    });

    renderPanel();

    fireEvent.click(await screen.findByRole('tab', { name: 'Checklist' }));
    const summary = screen.getByTestId('spec-validation-checklist-summary');
    expect(summaryState('spec-validation-checklist-summary')).toBe('completed');
    expect(within(summary).getByText('Not required')).toBeInTheDocument();
    expect(summary).toHaveTextContent('Checklist is disabled for this board.');
    expect(within(summary).queryByText('Needs attention')).not.toBeInTheDocument();
  });

  it('uses only three applicable frozen policies when five bindings exist and explains skips', async () => {
    const passedBinding = (index: number) => ({
      binding_id: `binding-${index}`,
      guideline_id: `guideline-${index}`,
      revision_id: `revision-${index}`,
      title: `Policy ${index}`,
      enforcement: 'blocking',
      minimum_confidence: 80,
      status: index === 3 ? 'skipped' : 'passed',
      failed_metric_count: 0,
      waived_metric_count: 0,
      unwaived_failed_metric_count: 0,
      metrics: [{
        metric_id: `metric-${index}`,
        code: `quality.metric_${index}`,
        title: `Metric ${index}`,
        description: 'Frozen description.',
        description_truncated: false,
        evaluation_rubric: 'Frozen rubric.',
        evaluation_rubric_truncated: false,
        assessment_outcome: index === 3 ? 'pending' : 'passed',
        direction: 'minimum',
        default_threshold: 80,
        effective_threshold: 80,
        threshold_source: 'default',
      }],
    });
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      checks: failedCycle.checks.map((check) => check.result_type === 'policy_compliance'
        ? {
            ...check,
            status: 'passed',
            // Deliberately stale transport copy: the UI must use details.
            summary: '3 of 5 completed',
            details: {
              counts: {
                applicable: 3,
                completed: 3,
                passed: 2,
                failed: 0,
                waived: 0,
                skipped: 1,
                pending: 0,
                context_only: 2,
                inconsistent: 0,
                scope_inconsistent: 0,
                blocking: 3,
                advisory: 0,
                blocking_failed: 0,
                blocking_pending: 0,
                advisory_failed: 0,
                advisory_pending: 0,
                failed_metrics: 0,
                waived_metrics: 0,
                unwaived_failed_metrics: 0,
              },
              applicable_bindings: [
                passedBinding(1),
                passedBinding(2),
                passedBinding(3),
              ],
            },
          }
        : check),
    });

    renderPanel({
      policyTransitionPreview: {
        status: 'ready',
        transitions: [],
        error: null,
      },
      policyTransitionRejection: lastTransitionPolicyRejection,
    });
    fireEvent.click(await screen.findByRole('tab', { name: 'Policy Compliance' }));

    const summary = screen.getByTestId('spec-validation-policy-summary');
    expect(summary).toHaveTextContent(
      'All 3 applicable policies are resolved: 2 passed and 1 skipped.',
    );
    expect(summary).toHaveTextContent(
      '2 adopted guidelines have no Spec metrics and are excluded from the applicable total.',
    );
    expect(summary).not.toHaveTextContent('3 of 5 completed');
    expect(summaryState('spec-validation-policy-summary')).toBe('passed');
    expect(within(summary).getByText('Passed with skips')).toBeInTheDocument();
    expect(screen.getByTestId('policy-detail'))
      .toHaveAttribute('data-applicable-bindings', '3');
    expect(screen.getByTestId('spec-validation-policy-transition-attempt'))
      .toHaveTextContent('Last transition attempt:');
    expect(screen.getByTestId('spec-validation-policy-transition-attempt'))
      .toHaveTextContent('The frozen edition summary above is unchanged.');
  });

  it('presents fully waived policy findings as resolved for the frozen edition', async () => {
    const policyCheck = failedCycle.checks.find(
      (check) => check.result_type === 'policy_compliance',
    )!;
    const baseBinding = (policyCheck.details as {
      applicable_bindings: Array<Record<string, unknown>>;
    }).applicable_bindings[0];
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      checks: failedCycle.checks.map((check) => check.result_type === 'policy_compliance'
        ? {
            ...policyCheck,
            status: 'passed',
            summary: '1 blocking policy failed',
            details: {
              counts: {
                applicable: 1,
                completed: 1,
                passed: 0,
                failed: 0,
                waived: 1,
                skipped: 0,
                pending: 0,
                context_only: 0,
                inconsistent: 0,
                scope_inconsistent: 0,
                blocking: 1,
                advisory: 0,
                blocking_failed: 0,
                blocking_pending: 0,
                advisory_failed: 0,
                advisory_pending: 0,
                failed_metrics: 1,
                waived_metrics: 1,
                unwaived_failed_metrics: 0,
              },
              applicable_bindings: [{
                ...baseBinding,
                status: 'waived',
                failed_metric_count: 1,
                waived_metric_count: 1,
                unwaived_failed_metric_count: 0,
                metrics: (baseBinding.metrics as Array<Record<string, unknown>>).map(
                  (metric) => ({ ...metric, assessment_outcome: 'waived' }),
                ),
              }],
            },
          }
        : check),
    });

    renderPanel();
    fireEvent.click(await screen.findByRole('tab', { name: 'Policy Compliance' }));

    const summary = screen.getByTestId('spec-validation-policy-summary');
    expect(summaryState('spec-validation-policy-summary')).toBe('passed');
    expect(within(summary).getByText('Waived')).toBeInTheDocument();
    expect(summary).toHaveTextContent(
      'All 1 applicable policy is resolved: 1 waived.',
    );
    expect(summary).toHaveTextContent(
      '1 failed metric finding is covered by an approved waiver.',
    );
    expect(summary).not.toHaveTextContent('1 blocking policy failed');
  });

  it('keeps a partially waived blocking policy in needs-attention with residual copy', async () => {
    const policyCheck = failedCycle.checks.find(
      (check) => check.result_type === 'policy_compliance',
    )!;
    const baseBinding = (policyCheck.details as {
      applicable_bindings: Array<Record<string, unknown>>;
    }).applicable_bindings[0];
    const baseMetric = (baseBinding.metrics as Array<Record<string, unknown>>)[0];
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      checks: failedCycle.checks.map((check) => check.result_type === 'policy_compliance'
        ? {
            ...policyCheck,
            details: {
              counts: {
                applicable: 1,
                completed: 1,
                passed: 0,
                failed: 1,
                waived: 0,
                skipped: 0,
                pending: 0,
                context_only: 0,
                inconsistent: 0,
                scope_inconsistent: 0,
                blocking: 1,
                advisory: 0,
                blocking_failed: 1,
                blocking_pending: 0,
                advisory_failed: 0,
                advisory_pending: 0,
                failed_metrics: 2,
                waived_metrics: 1,
                unwaived_failed_metrics: 1,
              },
              applicable_bindings: [{
                ...baseBinding,
                failed_metric_count: 2,
                waived_metric_count: 1,
                unwaived_failed_metric_count: 1,
                metrics: [
                  { ...baseMetric, assessment_outcome: 'waived' },
                  {
                    ...baseMetric,
                    metric_id: 'metric-residual',
                    code: 'quality.residual',
                    title: 'Residual metric',
                    assessment_outcome: 'failed',
                  },
                ],
              }],
            },
          }
        : check),
    });

    renderPanel();
    fireEvent.click(await screen.findByRole('tab', { name: 'Policy Compliance' }));

    const summary = screen.getByTestId('spec-validation-policy-summary');
    expect(summaryState('spec-validation-policy-summary')).toBe('needs_attention');
    expect(summary).toHaveTextContent('1 blocking policy failed.');
    expect(summary).toHaveTextContent(
      '1 failed metric finding is covered by an approved waiver; 1 failed metric finding remains unresolved.',
    );
  });

  it('presents advisory-only findings as completed and explicitly non-blocking', async () => {
    const policyCheck = failedCycle.checks.find(
      (check) => check.result_type === 'policy_compliance',
    )!;
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      checks: failedCycle.checks.map((check) => check.result_type === 'policy_compliance'
        ? {
            ...policyCheck,
            status: 'advisory',
            details: {
              counts: {
                applicable: 1,
                completed: 1,
                passed: 0,
                failed: 1,
                waived: 0,
                skipped: 0,
                pending: 0,
                context_only: 0,
                inconsistent: 0,
                scope_inconsistent: 0,
                blocking: 0,
                advisory: 1,
                blocking_failed: 0,
                blocking_pending: 0,
                advisory_failed: 1,
                advisory_pending: 0,
                failed_metrics: 1,
                waived_metrics: 0,
                unwaived_failed_metrics: 1,
              },
              applicable_bindings: [{
                ...(policyCheck.details as {
                  applicable_bindings: Array<Record<string, unknown>>;
                }).applicable_bindings[0],
                enforcement: 'advisory',
              }],
            },
          }
        : check),
    });

    renderPanel({
      policyTransitionPreview: {
        status: 'ready',
        transitions: [],
        error: null,
      },
    });
    fireEvent.click(await screen.findByRole('tab', { name: 'Policy Compliance' }));

    const summary = screen.getByTestId('spec-validation-policy-summary');
    expect(summaryState('spec-validation-policy-summary')).toBe('completed');
    expect(within(summary).getByText('Advisory')).toBeInTheDocument();
    expect(summary).toHaveTextContent('1 advisory policy has findings');
    expect(summary).toHaveTextContent('do not block validation');
    expect(summary).not.toHaveTextContent('In progress');
    expect(summary).not.toHaveTextContent('Needs attention');
  });

  it.each([
    { completed: 0, passed: 0, includeBlockingPass: false },
    { completed: 1, passed: 1, includeBlockingPass: true },
  ])(
    'keeps advisory-only pending work non-blocking with $completed completed',
    async ({ completed, passed, includeBlockingPass }) => {
      const policyCheck = failedCycle.checks.find(
        (check) => check.result_type === 'policy_compliance',
      )!;
      const baseBinding = (policyCheck.details as {
        applicable_bindings: Array<Record<string, unknown>>;
      }).applicable_bindings[0];
      const advisoryPending = {
        ...baseBinding,
        binding_id: 'binding-advisory-pending',
        guideline_id: 'guideline-advisory-pending',
        revision_id: 'revision-advisory-pending',
        title: 'Advisory pending policy',
        enforcement: 'advisory',
        status: 'pending',
        failed_metric_count: 0,
        waived_metric_count: 0,
        unwaived_failed_metric_count: 0,
        metrics: (baseBinding.metrics as Array<Record<string, unknown>>).map(
          (metric) => ({ ...metric, assessment_outcome: 'pending' }),
        ),
      };
      const blockingPass = {
        ...baseBinding,
        binding_id: 'binding-blocking-pass',
        guideline_id: 'guideline-blocking-pass',
        revision_id: 'revision-blocking-pass',
        title: 'Blocking passed policy',
        enforcement: 'blocking',
        status: 'passed',
        failed_metric_count: 0,
        waived_metric_count: 0,
        unwaived_failed_metric_count: 0,
        metrics: (baseBinding.metrics as Array<Record<string, unknown>>).map(
          (metric) => ({ ...metric, assessment_outcome: 'passed' }),
        ),
      };
      apiMock.getValidationCycle.mockResolvedValue({
        ...failedCycle,
        checks: failedCycle.checks.map((check) => check.result_type === 'policy_compliance'
          ? {
              ...policyCheck,
              status: 'advisory',
              details: {
                counts: {
                  applicable: includeBlockingPass ? 2 : 1,
                  completed,
                  passed,
                  failed: 0,
                  waived: 0,
                  skipped: 0,
                  pending: 1,
                  context_only: 0,
                  inconsistent: 0,
                  scope_inconsistent: 0,
                  blocking: includeBlockingPass ? 1 : 0,
                  advisory: 1,
                  blocking_failed: 0,
                  blocking_pending: 0,
                  advisory_failed: 0,
                  advisory_pending: 1,
                  failed_metrics: 0,
                  waived_metrics: 0,
                  unwaived_failed_metrics: 0,
                },
                applicable_bindings: includeBlockingPass
                  ? [blockingPass, advisoryPending]
                  : [advisoryPending],
              },
            }
          : check),
      });

      renderPanel({
        policyTransitionPreview: {
          status: 'ready',
          transitions: [],
          error: null,
        },
      });
      fireEvent.click(await screen.findByRole('tab', { name: 'Policy Compliance' }));

      const summary = screen.getByTestId('spec-validation-policy-summary');
      expect(summaryState('spec-validation-policy-summary')).toBe('completed');
      expect(within(summary).getByText('Advisory')).toBeInTheDocument();
      expect(summary).toHaveTextContent(
        `${completed} of ${includeBlockingPass ? 2 : 1} applicable policies assessed`,
      );
      expect(summary).toHaveTextContent('1 advisory policy awaits assessment');
      expect(summary).toHaveTextContent('do not block validation');
      expect(summary).not.toHaveTextContent('In progress');
      expect(summary).not.toHaveTextContent('Not started');
    },
  );

  it('fails closed for a mixed inconsistent scope while preserving verified binding details', async () => {
    const policyCheck = failedCycle.checks.find(
      (check) => check.result_type === 'policy_compliance',
    )!;
    const baseBinding = (policyCheck.details as {
      applicable_bindings: Array<Record<string, unknown>>;
    }).applicable_bindings[0];
    apiMock.getValidationCycle.mockResolvedValue({
      ...failedCycle,
      checks: failedCycle.checks.map((check) => check.result_type === 'policy_compliance'
        ? {
            ...policyCheck,
            status: 'needs_attention',
            details: {
              counts: {
                applicable: 1,
                completed: 1,
                passed: 1,
                failed: 0,
                waived: 0,
                skipped: 0,
                pending: 0,
                context_only: 0,
                inconsistent: 0,
                scope_inconsistent: 1,
                blocking: 1,
                advisory: 0,
                blocking_failed: 0,
                blocking_pending: 0,
                advisory_failed: 0,
                advisory_pending: 0,
                failed_metrics: 0,
                waived_metrics: 0,
                unwaived_failed_metrics: 0,
              },
              applicable_bindings: [{
                ...baseBinding,
                status: 'passed',
                failed_metric_count: 0,
                waived_metric_count: 0,
                unwaived_failed_metric_count: 0,
                metrics: (baseBinding.metrics as Array<Record<string, unknown>>).map(
                  (metric) => ({ ...metric, assessment_outcome: 'passed' }),
                ),
              }],
            },
          }
        : check),
    });

    renderPanel({
      policyTransitionPreview: {
        status: 'ready',
        transitions: [],
        error: null,
      },
    });
    fireEvent.click(await screen.findByRole('tab', { name: 'Policy Compliance' }));

    const summary = screen.getByTestId('spec-validation-policy-summary');
    expect(summaryState('spec-validation-policy-summary')).toBe('needs_attention');
    expect(within(summary).getByText('Unavailable')).toBeInTheDocument();
    expect(summary).toHaveTextContent(
      '1 frozen scope item could not be reconciled safely',
    );
    expect(summary).toHaveTextContent(
      'Verified applicable policies remain visible below',
    );
    expect(screen.getByTestId('policy-detail'))
      .toHaveAttribute('data-applicable-bindings', '1');
  });

  it('lazy-mounts subtabs, preserves a checklist draft and keeps validation audit bounded', async () => {
    renderPanel();
    await screen.findByTestId('spec-validation-current');
    expect(screen.queryByTestId('checklist-detail')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Checklist' }));
    expect(screen.getByTestId('checklist-detail')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Cached checklist draft'), {
      target: { value: 'retained draft' },
    });

    fireEvent.click(screen.getByRole('tab', { name: 'Spec Validation' }));
    expect(screen.getByTestId('checklist-detail')).not.toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'Checklist' }));
    expect(screen.getByTestId('checklist-detail')).toBeVisible();
    expect(screen.getByLabelText('Cached checklist draft')).toHaveValue(
      'retained draft',
    );

    fireEvent.click(screen.getByRole('tab', { name: 'Spec Validation' }));
    const auditToggle = screen.getByTestId(
      'spec-validation-technical-audit-toggle',
    );
    expect(screen.queryByText('validation-receipt-2')).not.toBeInTheDocument();
    expect(screen.queryByText('subject r8 · head r1')).not.toBeInTheDocument();
    fireEvent.click(auditToggle);
    await waitFor(() => expect(
      apiMock.getValidationTechnicalAudit,
    ).toHaveBeenCalledTimes(1));
    expect(screen.getByText('validation-receipt-2')).toBeInTheDocument();
    expect(screen.getByText('subject r8 · head r1')).toBeInTheDocument();
    fireEvent.click(auditToggle);
    fireEvent.click(auditToggle);
    expect(apiMock.getValidationTechnicalAudit).toHaveBeenCalledTimes(1);

    const currentCard = screen.getByTestId('spec-validation-current');
    expect(within(currentCard).queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByTestId('validation-detail-current')).toBeVisible();
    expect(apiMock.getCurrentSpecValidation).toHaveBeenCalledTimes(1);

    const previousToggle = screen.getByTestId('spec-validation-previous-toggle');
    expect(apiMock.listSpecValidations).not.toHaveBeenCalled();
    fireEvent.click(previousToggle);
    await waitFor(() => expect(apiMock.listSpecValidations).toHaveBeenCalledTimes(1));
    fireEvent.click(previousToggle);
    fireEvent.click(previousToggle);
    expect(apiMock.listSpecValidations).toHaveBeenCalledTimes(1);
  });
});
