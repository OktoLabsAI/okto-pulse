import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AllowedTransition, Spec, TestScenario } from '@/types';
import {
  TestScenarioPolicyCompliance,
  TestScenarioStatusBadge,
} from '../TestScenarioPolicyCompliance';

const mocks = vi.hoisted(() => ({
  updateStatus: vi.fn(),
  getSpec: vi.fn(),
  refresh: vi.fn(),
  handleTransitionError: vi.fn(),
  clearRejection: vi.fn(),
  useAuthority: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => ({
    updateTestScenarioStatus: mocks.updateStatus,
    getSpec: mocks.getSpec,
  }),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    success: mocks.toastSuccess,
    error: mocks.toastError,
  },
}));

vi.mock('@/components/policy-compliance', () => ({
  usePolicyTransitionAuthority: (...args: unknown[]) =>
    mocks.useAuthority(...args),
  PolicyComplianceTransitionPreview: ({
    rejection,
  }: {
    rejection: unknown;
  }) => (
    <div
      data-testid="policy-transition-preview"
      data-rejection={rejection ? 'present' : 'none'}
    />
  ),
  PolicyCompliancePanel: ({
    boardId,
    entityType,
    subjectId,
  }: {
    boardId: string;
    entityType: string;
    subjectId: string;
  }) => (
    <div
      data-testid="policy-compliance-panel"
      data-board-id={boardId}
      data-entity-type={entityType}
      data-subject-id={subjectId}
    />
  ),
}));

function transition(
  toStatus: string,
  {
    preconditions = [],
    policyCompliance = false,
  }: {
    preconditions?: string[];
    policyCompliance?: boolean;
  } = {},
): AllowedTransition {
  return {
    to_status: toStatus,
    label: toStatus[0].toUpperCase() + toStatus.slice(1),
    gate: policyCompliance ? 'test_scenario_progression' : 'none',
    blocked_reason: null,
    preconditions,
    capabilities: [],
    effects: ['status_changed', 'activity_logged'],
    reason_codes: ['transition_not_allowed'],
    policy_compliance: policyCompliance,
    policy_compliance_decision: policyCompliance
      ? {
        projection: 'full',
        state: 'policy_compliance_ready',
        allowed: true,
        policy_compliance_required: true,
        reason_codes: ['policy_compliance_ready'],
        decision_digest: 'a'.repeat(64),
        fence_digest: 'b'.repeat(64),
        receipt_ids: ['receipt-1'],
        currentness: 'current',
        currentness_reasons: [],
        applicable_metric_count: 1,
        applicable_blocking_metric_count: 1,
        failed_metric_count: 0,
        blocking_metric_count: 0,
        waived_metric_count: 0,
        advisory_issue_count: 0,
        skipped_binding_count: 0,
        diagnostic_codes: [],
        binding_decisions: [{
          binding_id: 'binding-1',
          guideline_id: 'guideline-1',
          enforcement: 'blocking',
          applicable_metric_count: 1,
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
        }],
      }
      : null,
  };
}

const scenario: TestScenario = {
  id: 'scenario-1',
  title: 'Scoped scenario',
  linked_criteria: ['ac-1'],
  scenario_type: 'e2e',
  given: 'a current subject',
  when: 'the lifecycle action is requested',
  then: 'the scoped writer is used',
  notes: null,
  status: 'ready',
  linked_task_ids: ['card-1'],
};

const refreshedSpec = {
  id: 'spec-1',
  board_id: 'board-1',
  test_scenarios: [{ ...scenario, status: 'draft' }],
} as Spec;

function authority(
  actionableTransitions: AllowedTransition[],
  {
    rejection = null,
  }: {
    rejection?: unknown;
  } = {},
) {
  return {
    preview: {
      status: 'ready',
      transitions: actionableTransitions,
      error: null,
    },
    transitions: actionableTransitions,
    actionableTransitions,
    rejection,
    refresh: mocks.refresh,
    handleTransitionError: mocks.handleTransitionError,
    clearRejection: mocks.clearRejection,
  };
}

describe('TestScenarioPolicyCompliance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getSpec.mockResolvedValue(refreshedSpec);
    mocks.updateStatus.mockResolvedValue({
      id: 'spec-1',
      scenario: { id: 'scenario-1', status: 'draft' },
      result: {
        scenario_id: 'scenario-1',
        old_status: 'ready',
        new_status: 'draft',
        evidence_provided: false,
        evidence_gate_skipped: false,
      },
    });
    mocks.handleTransitionError.mockResolvedValue(false);
  });

  it('renders the current status as a non-editable badge', () => {
    render(<TestScenarioStatusBadge status="ready" />);

    expect(screen.getByTestId('test-scenario-status-badge')).toHaveTextContent(
      'ready',
    );
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('scopes panel and preview to the expanded scenario under exact permission', () => {
    mocks.useAuthority.mockReturnValue(
      authority([], {
        rejection: { code: 'policy_compliance_blocked' },
      }),
    );

    render(
      <TestScenarioPolicyCompliance
        boardId="board-1"
        specId="spec-1"
        specArchived={false}
        scenario={scenario}
        canReadPolicyCompliance
        refreshKey={3}
        onSpecRefreshed={vi.fn()}
      />,
    );

    expect(mocks.useAuthority).toHaveBeenCalledWith({
      boardId: 'board-1',
      entityType: 'test_scenario',
      subjectId: 'scenario-1',
      currentStatus: 'ready',
      refreshKey: 3,
    });
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-entity-type',
      'test_scenario',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-subject-id',
      'scenario-1',
    );
    expect(screen.getByTestId('policy-transition-preview')).toHaveAttribute(
      'data-rejection',
      'present',
    );
  });

  it('does not expose policy evidence without the exact read permission', () => {
    mocks.useAuthority.mockReturnValue(authority([]));

    render(
      <TestScenarioPolicyCompliance
        boardId="board-1"
        specId="spec-1"
        specArchived={false}
        scenario={scenario}
        canReadPolicyCompliance={false}
        refreshKey={0}
        onSpecRefreshed={vi.fn()}
      />,
    );

    expect(
      screen.queryByTestId('policy-compliance-panel'),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('policy-transition-preview'),
    ).not.toBeInTheDocument();
  });

  it('executes only actionable edges that do not require authenticated evidence', async () => {
    const recovery = transition('draft');
    const passed = transition('passed', {
      preconditions: ['authenticated_test_evidence'],
      policyCompliance: true,
    });
    mocks.useAuthority.mockReturnValue(authority([recovery, passed]));
    const onSpecRefreshed = vi.fn();

    render(
      <TestScenarioPolicyCompliance
        boardId="board-1"
        specId="spec-1"
        specArchived={false}
        scenario={scenario}
        canReadPolicyCompliance
        refreshKey={0}
        onSpecRefreshed={onSpecRefreshed}
      />,
    );

    expect(
      screen.queryByRole('button', { name: 'Move to Passed' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId('test-scenario-evidence-required'),
    ).toHaveTextContent('Passed');

    fireEvent.click(screen.getByRole('button', { name: 'Move to Draft' }));

    await waitFor(() => {
      expect(mocks.updateStatus).toHaveBeenCalledWith(
        'spec-1',
        'scenario-1',
        { status: 'draft' },
      );
    });
    expect(mocks.getSpec).toHaveBeenCalledWith('spec-1');
    expect(onSpecRefreshed).toHaveBeenCalledWith(refreshedSpec);
  });

  it('routes a structured rejection through the shared authority and refreshes the full spec', async () => {
    const recovery = transition('draft');
    const error = new Error('structured 409');
    mocks.useAuthority.mockReturnValue(authority([recovery]));
    mocks.updateStatus.mockRejectedValue(error);
    mocks.handleTransitionError.mockResolvedValue(true);
    const onSpecRefreshed = vi.fn();

    render(
      <TestScenarioPolicyCompliance
        boardId="board-1"
        specId="spec-1"
        specArchived={false}
        scenario={scenario}
        canReadPolicyCompliance
        refreshKey={0}
        onSpecRefreshed={onSpecRefreshed}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Move to Draft' }));

    await waitFor(() => {
      expect(mocks.handleTransitionError).toHaveBeenCalledWith(error, 'draft');
    });
    expect(mocks.getSpec).toHaveBeenCalledWith('spec-1');
    expect(onSpecRefreshed).toHaveBeenCalledWith(refreshedSpec);
    expect(mocks.toastError).toHaveBeenCalledWith(
      expect.stringContaining('Policy Compliance rejected'),
    );
  });
});
