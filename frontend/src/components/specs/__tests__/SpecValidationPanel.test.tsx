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
  PolicyCompliancePanel: () => <div data-testid="policy-detail" />,
  PolicyComplianceTransitionPreview: () => <div data-testid="policy-audit" />,
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
    { result_type: 'requirement_lint', status: 'not_started', summary: 'Not started' },
    { result_type: 'curated_checklist', status: 'not_started', summary: 'Not started' },
    { result_type: 'policy_compliance', status: 'needs_attention', summary: 'Needs attention' },
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

function rowState(testId: string): string | null {
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

  it('uses only current-edition evidence and preserves explicit failed outcomes', async () => {
    renderPanel();

    await waitFor(() => expect(
      screen.getByTestId('spec-validation-current').querySelector('[data-state]'),
    ).toHaveAttribute('data-state', 'failed'));
    expect(rowState('spec-validation-checklist-row')).toBe('not_started');
    expect(rowState('spec-validation-lint-row')).toBe('not_started');
    expect(rowState('spec-validation-policy-row')).toBe('needs_attention');
    expect(screen.queryByTestId('spec-validation-qualitative-row'))
      .not.toBeInTheDocument();
    expect(screen.getAllByTestId('spec-validation-current')).toHaveLength(1);
    expect(screen.getAllByTestId('spec-validation-previous-toggle')).toHaveLength(1);
    expect(screen.getByLabelText('Current validation checks').children)
      .toHaveLength(3);
    expect(screen.queryByText(/stale/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/receipt/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/head r/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/subject r/i)).not.toBeInTheDocument();
    expect(screen.getByText(/open to review its score and summary/i))
      .toBeInTheDocument();
    expect(screen.queryByText(/dimension details/i)).not.toBeInTheDocument();
    expect(apiMock.getValidationCycle).toHaveBeenCalledTimes(1);
    expect(apiMock.getSpecChecklistState).not.toHaveBeenCalled();
    expect(apiMock.getCurrentSpecValidation).not.toHaveBeenCalled();
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

    await screen.findByTestId('spec-validation-checklist-row');
    expect(screen.queryByTestId('spec-validation-current')).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-previous-toggle')).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-technical-audit-toggle'))
      .not.toBeInTheDocument();
    expect(apiMock.getCurrentSpecValidation).not.toHaveBeenCalled();
    expect(apiMock.listSpecValidations).not.toHaveBeenCalled();
    expect(apiMock.getValidationTechnicalAudit).not.toHaveBeenCalled();
  });

  it('does not expose Policy Compliance preview through validation audit authority', async () => {
    renderPanel({ canReadPolicyCompliance: false });
    await screen.findByTestId('spec-validation-current');

    fireEvent.click(screen.getByTestId('spec-validation-technical-audit-toggle'));

    await screen.findByText('validation-receipt-2');
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

    const checklistRow = await screen.findByTestId('spec-validation-checklist-row');
    expect(rowState('spec-validation-checklist-row')).toBe('completed');
    expect(within(checklistRow).getByText('Not required')).toBeInTheDocument();
    expect(checklistRow).toHaveTextContent('Checklist is disabled for this board.');
    expect(within(checklistRow).queryByText('Needs attention')).not.toBeInTheDocument();
  });

  it('exposes accessible lazy rows without mounting their details eagerly', async () => {
    renderPanel();
    await screen.findByTestId('spec-validation-checklist-row');

    const row = screen.getByTestId('spec-validation-checklist-row');
    const toggle = within(row).getByRole('button');
    const contentId = toggle.getAttribute('aria-controls');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(contentId).toBeTruthy();
    expect(screen.queryByTestId('checklist-detail')).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(document.getElementById(contentId!)).toBeInTheDocument();
    expect(screen.getByTestId('checklist-detail')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Cached checklist draft'), {
      target: { value: 'retained draft' },
    });

    fireEvent.keyDown(screen.getByTestId('checklist-detail'), { key: 'Escape' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveFocus();
    expect(screen.getByTestId('checklist-detail')).not.toBeVisible();

    fireEvent.click(toggle);
    expect(screen.getByTestId('checklist-detail')).toBeVisible();
    expect(screen.getByLabelText('Cached checklist draft')).toHaveValue(
      'retained draft',
    );

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
    const currentToggle = within(currentCard).getByRole('button');
    expect(apiMock.getCurrentSpecValidation).not.toHaveBeenCalled();
    fireEvent.click(currentToggle);
    await waitFor(() => expect(apiMock.getCurrentSpecValidation).toHaveBeenCalledTimes(1));
    fireEvent.click(currentToggle);
    fireEvent.click(currentToggle);
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
