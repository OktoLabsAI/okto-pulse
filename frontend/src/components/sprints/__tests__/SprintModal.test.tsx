import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SprintModal } from '../SprintModal';
import { deriveSprintDisplayCounts } from '../sprintDisplayCounts';
import type { CardSummaryForSpec, Sprint } from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type {
  PolicyCompliancePanelProps,
  PolicyComplianceTransitionPreviewProps,
} from '@/components/policy-compliance';

const apiMock = vi.hoisted(() => ({
  getSprint: vi.fn(),
  getSpec: vi.fn(),
  getAllowedTransitions: vi.fn(),
  updateSprint: vi.fn(),
  moveSprint: vi.fn(),
  listSprintHistory: vi.fn(),
  assignTasksToSprint: vi.fn(),
  unassignTasksFromSprint: vi.fn(),
}));

const permissionState = vi.hoisted(() => ({
  flags: new Set<string>(),
  denied: new Set<string>(),
}));

const policyComponentState = vi.hoisted(() => ({
  panelProps: null as PolicyCompliancePanelProps | null,
}));

const markdownMock = vi.hoisted(() => ({
  exportSprint: vi.fn(() => '# sprint export'),
  downloadMarkdown: vi.fn(),
  slugify: vi.fn((value: string) => value.toLowerCase().replace(/\s+/g, '-')),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/hooks/usePermissions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/usePermissions')>();
  return {
    ...actual,
    usePermissions: () => ({
      preset: null,
      isLoading: false,
      error: null,
      ownerReviewRequired: false,
      has: (flag: string) => (
        flag.startsWith('sprint.')
          ? !permissionState.denied.has(flag)
          : permissionState.flags.has(flag)
      ),
    }),
  };
});

vi.mock('@/components/policy-compliance', async (importOriginal) => {
  const actual = await importOriginal<
    typeof import('@/components/policy-compliance')
  >();
  return {
    ...actual,
    PolicyCompliancePanel: (props: PolicyCompliancePanelProps) => {
      policyComponentState.panelProps = props;
      return (
        <div
          data-testid="policy-compliance-panel"
          data-board-id={props.boardId}
          data-entity-type={props.entityType}
          data-subject-id={props.subjectId}
          data-refresh-key={props.refreshKey}
        />
      );
    },
    PolicyComplianceTransitionPreview: ({
      preview,
      rejection,
    }: PolicyComplianceTransitionPreviewProps) => (
      <div
        data-testid="policy-transition-preview"
        data-status={preview.status}
      >
        {rejection?.code || ''}
      </div>
    ),
  };
});

vi.mock('@/lib/exportMarkdown', () => ({
  exportSprint: markdownMock.exportSprint,
  downloadMarkdown: markdownMock.downloadMarkdown,
  slugify: markdownMock.slugify,
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('@/components/shared/ValidationGateOverride', () => ({
  ValidationGateOverride: () => <div data-testid="validation-gate-override" />,
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

let currentSprint: Sprint;

function card(overrides: Partial<CardSummaryForSpec>): CardSummaryForSpec {
  return {
    id: overrides.id || 'card-1',
    title: overrides.title || 'Card title',
    status: overrides.status || 'not_started',
    priority: overrides.priority || 'medium',
    assignee_id: overrides.assignee_id ?? null,
    sprint_id: overrides.sprint_id ?? 'sprint-1',
    ...(overrides.card_type !== undefined ? { card_type: overrides.card_type } : {}),
  };
}

function sprint(overrides: Partial<Sprint> = {}): Sprint {
  return {
    id: 'sprint-1',
    spec_id: 'spec-1',
    board_id: 'board-1',
    title: 'Sprint Details QA',
    description: null,
    objective: 'Existing objective',
    expected_outcome: 'Existing expected outcome',
    status: 'active',
    lane_type: 'normal',
    origin_sprint_id: null,
    origin_bug_id: null,
    normal_sprint_created: true,
    spec_version: 3,
    start_date: null,
    end_date: null,
    test_scenario_ids: [],
    business_rule_ids: [],
    evaluations: [],
    skip_test_coverage: false,
    skip_rules_coverage: false,
    skip_qualitative_validation: false,
    validation_threshold: null,
    version: 1,
    labels: [],
    archived: false,
    created_by: 'agent-1',
    created_at: '2026-05-28T10:00:00Z',
    updated_at: '2026-05-28T10:00:00Z',
    cards: [],
    qa_items: [],
    ...overrides,
  };
}

function transition(
  toStatus: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    to_status: toStatus,
    label: toStatus.charAt(0).toUpperCase() + toStatus.slice(1),
    gate: 'none',
    blocked_reason: null,
    blocked_facts: null,
    preconditions: [],
    capabilities: [],
    effects: [],
    reason_codes: [],
    policy_compliance: false,
    policy_compliance_decision: null,
    ...overrides,
  };
}

function policyDecision(allowed: boolean) {
  return {
    projection: 'full',
    state: allowed
      ? 'policy_compliance_ready'
      : 'policy_compliance_blocked',
    allowed,
    policy_compliance_required: true,
    reason_codes: [
      allowed
        ? 'policy_compliance_ready'
        : 'policy_compliance_blocked',
    ],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_ids: ['receipt-sprint-1'],
    currentness: 'current',
    currentness_reasons: [],
    applicable_metric_count: 2,
    applicable_blocking_metric_count: 2,
    failed_metric_count: allowed ? 0 : 1,
    blocking_metric_count: allowed ? 0 : 1,
    waived_metric_count: 0,
    advisory_issue_count: 0,
    skipped_binding_count: 0,
    diagnostic_codes: allowed ? [] : ['policy_metric_threshold_failed'],
    binding_decisions: [{
      binding_id: 'binding-sprint-1',
      guideline_id: 'guideline-sprint-1',
      enforcement: 'blocking',
      applicable_metric_count: 2,
      allowed,
      assessment_available: true,
      receipt_id: 'receipt-sprint-1',
      currentness: 'current',
      currentness_reasons: [],
      inadmissibility_cause: null,
      failed_metric_count: allowed ? 0 : 1,
      waived_metric_count: 0,
      blocking_metric_count: allowed ? 0 : 1,
      advisory_issue_count: 0,
      skipped: false,
      diagnostic_codes:
        allowed ? [] : ['policy_metric_threshold_failed'],
    }],
  };
}

function transitionResponse(
  status: string,
  allowedTransitions: ReturnType<typeof transition>[],
) {
  return {
    board_id: 'board-1',
    entity_type: 'sprint',
    entity_id: 'sprint-1',
    current_status: status,
    source: 'core_sdlc_registry_v1',
    allowed_transitions: allowedTransitions,
  };
}

function defaultTransitions(status: string) {
  if (status === 'active') {
    return [
      transition('draft', {
        gate: 'reopen',
        capabilities: ['reopen'],
      }),
      transition('review', {
        gate: 'sprint_review',
        capabilities: ['request_review'],
      }),
      transition('cancelled', { gate: 'cancel' }),
    ];
  }
  if (status === 'review') {
    return [
      transition('active', {
        gate: 'rework',
        capabilities: ['reopen'],
      }),
      transition('closed', {
        gate: 'sprint_completion',
        capabilities: ['complete'],
        policy_compliance: true,
        policy_compliance_decision: policyDecision(true),
      }),
      transition('cancelled', { gate: 'cancel' }),
    ];
  }
  return [];
}

function structuredPolicyRejection() {
  return new AuthenticatedFetchError({
    message: 'Policy Compliance blocked the transition.',
    status: 409,
    code: 'policy_compliance_blocked',
    details: {
      outcome: 'error',
      error: 'policy_compliance_blocked',
      code: 'policy_compliance_blocked',
      message: 'Policy Compliance blocked the transition.',
      policy_compliance_required: true,
      reason_codes: ['policy_compliance_blocked'],
      decision_digest: 'c'.repeat(64),
      fence_digest: 'd'.repeat(64),
      receipt_ids: ['receipt-sprint-1'],
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
      binding_decisions: [{
        binding_id: 'binding-sprint-1',
        guideline_id: 'guideline-sprint-1',
        enforcement: 'blocking',
        applicable_metric_count: 2,
        allowed: false,
        assessment_available: true,
        receipt_id: 'receipt-sprint-1',
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
      transition: {
        entity_type: 'sprint',
        subject_id: 'sprint-1',
        from_status: 'review',
        to_status: 'closed',
      },
    },
  });
}

beforeEach(() => {
  permissionState.flags = new Set();
  permissionState.denied = new Set();
  policyComponentState.panelProps = null;
  apiMock.getAllowedTransitions.mockImplementation(
    (_boardId: string, params: { current_status?: string }) => {
      const status = params.current_status || 'active';
      return Promise.resolve(
        transitionResponse(status, defaultTransitions(status)),
      );
    },
  );
});

async function renderSprint(overrides: Partial<Sprint> = {}) {
  currentSprint = sprint(overrides);
  apiMock.getSprint.mockImplementation(() => Promise.resolve(currentSprint));
  apiMock.getSpec.mockResolvedValue({
    id: 'spec-1',
    title: 'Spec title',
    test_scenarios: [],
    business_rules: [],
    technical_requirements: [],
    acceptance_criteria: [],
    api_contracts: [],
    integration_requirements: [],
    observability_requirements: [],
  });
  apiMock.updateSprint.mockImplementation((_sprintId: string, patch: Partial<Sprint>) => {
    currentSprint = { ...currentSprint, ...patch };
    return Promise.resolve(currentSprint);
  });

  render(<SprintModal sprintId="sprint-1" onClose={vi.fn()} />);
  await screen.findByText('Sprint Details QA');
}

describe('deriveSprintDisplayCounts', () => {
  it('counts tasks, tests and bugs as separate sprint categories', () => {
    const cards = [
      card({ id: 'normal-1', title: 'Normal', status: 'done', card_type: 'normal' }),
      card({ id: 'bug-1', title: 'Bug', status: 'validation', card_type: 'bug' }),
      card({ id: 'test-1', title: 'Test', status: 'done', card_type: 'test' }),
      card({ id: 'legacy-1', title: 'Legacy', status: 'done', card_type: undefined }),
    ];

    const counts = deriveSprintDisplayCounts(cards);

    expect(counts.cards).toBe(3);
    expect(counts.tasks).toBe(2);
    expect(counts.tests).toBe(1);
    expect(counts.bugs).toBe(1);
    expect(counts.workItemsTotal).toBe(4);
    expect(counts.workItemsDone).toBe(3);
    expect(counts.visibleCards.map((item) => item.id)).toEqual(['normal-1', 'bug-1', 'legacy-1']);
    expect(counts.taskCards.map((item) => item.id)).toEqual(['normal-1', 'legacy-1']);
    expect(counts.testCards.map((item) => item.id)).toEqual(['test-1']);
    expect(counts.bugCards.map((item) => item.id)).toEqual(['bug-1']);
  });

  it('normalizes backend enum-shaped card_type values before counting', () => {
    const cards = [
      card({ id: 'normal-1', title: 'Normal', status: 'done', card_type: 'CardType.NORMAL' as any }),
      card({ id: 'bug-1', title: 'Bug', status: 'done', card_type: 'CardType.BUG' as any }),
      card({ id: 'test-1', title: 'Test', status: 'done', card_type: { value: 'test' } as any }),
    ];

    const counts = deriveSprintDisplayCounts(cards);

    expect(counts.tasks).toBe(1);
    expect(counts.tests).toBe(1);
    expect(counts.bugs).toBe(1);
    expect(counts.cards).toBe(2);
    expect(counts.visibleCards.map((item) => item.id)).toEqual(['normal-1', 'bug-1']);
  });
});

describe('SprintModal display counts', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listSprintHistory.mockResolvedValue([]);
    apiMock.moveSprint.mockResolvedValue({});
    apiMock.assignTasksToSprint.mockResolvedValue({});
    apiMock.unassignTasksFromSprint.mockResolvedValue({});
  });

  it('renders an empty sprint as zero counts without crashing', async () => {
    await renderSprint({ cards: [] });

    expect(screen.getByTestId('sprint-summary-tasks')).toHaveTextContent('0');
    expect(screen.getByTestId('sprint-summary-tests')).toHaveTextContent('0');
    expect(screen.getByTestId('sprint-summary-bugs')).toHaveTextContent('0');
    expect(screen.getByTestId('sprint-summary-done')).toHaveTextContent('0');
    expect(screen.getByText('0 of 0 work items done')).toBeInTheDocument();
  });

  it('does not inflate Cards or render test rows when only tests are assigned', async () => {
    await renderSprint({
      cards: [
        card({ id: 'test-1', title: 'Regression one', status: 'done', card_type: 'test' }),
        card({ id: 'test-2', title: 'Regression two', status: 'validation', card_type: 'test' }),
      ],
    });

    expect(screen.getByTestId('sprint-summary-tasks')).toHaveTextContent('0');
    expect(screen.getByTestId('sprint-summary-tests')).toHaveTextContent('2');
    expect(screen.getByTestId('sprint-summary-bugs')).toHaveTextContent('0');
    expect(screen.getByText('1 of 2 work items done')).toBeInTheDocument();
    expect(screen.queryByText(/cards done/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^Cards/i }));

    expect(screen.queryAllByTestId('sprint-card-row')).toHaveLength(0);
    expect(screen.queryByText('Regression one')).not.toBeInTheDocument();
    expect(screen.queryByText('Regression two')).not.toBeInTheDocument();
  });

  it('counts bug cards separately from Tasks while still rendering them in Cards', async () => {
    await renderSprint({
      cards: [
        card({ id: 'bug-1', title: 'Fix broken counter', status: 'done', card_type: 'bug' }),
        card({ id: 'bug-2', title: 'Fix stale label', status: 'not_started', card_type: 'bug' }),
      ],
    });

    expect(screen.getByTestId('sprint-summary-tasks')).toHaveTextContent('0');
    expect(screen.getByTestId('sprint-summary-tests')).toHaveTextContent('0');
    expect(screen.getByTestId('sprint-summary-bugs')).toHaveTextContent('2');

    fireEvent.click(screen.getByRole('button', { name: /^Cards/i }));

    const rows = screen.getAllByTestId('sprint-card-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText('Fix broken counter')).toBeInTheDocument();
    expect(within(rows[0]).getByText('bug')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Fix stale label')).toBeInTheDocument();
    expect(within(rows[1]).getByText('bug')).toBeInTheDocument();
  });

  it('keeps Cards tab count, rows and Work items label consistent for mixed sprints', async () => {
    await renderSprint({
      cards: [
        card({ id: 'normal-1', title: 'Implement feature', status: 'done', card_type: 'normal' }),
        card({ id: 'bug-1', title: 'Fix defect', status: 'validation', card_type: 'bug' }),
        card({ id: 'test-1', title: 'Regression test', status: 'done', card_type: 'test' }),
        card({ id: 'legacy-1', title: 'Legacy card', status: 'done', card_type: undefined }),
      ],
    });

    expect(screen.getByTestId('sprint-summary-tasks')).toHaveTextContent('2');
    expect(screen.getByTestId('sprint-summary-tests')).toHaveTextContent('1');
    expect(screen.getByTestId('sprint-summary-bugs')).toHaveTextContent('1');
    expect(screen.getByText('3 of 4 work items done')).toBeInTheDocument();
    expect(screen.queryByText(/cards done/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^Cards/i }));

    expect(screen.getByTestId('sprint-tab-count-cards')).toHaveTextContent('3');
    const rows = screen.getAllByTestId('sprint-card-row');
    expect(rows).toHaveLength(3);
    expect(screen.getByText('Implement feature')).toBeInTheDocument();
    expect(screen.getByText('Fix defect')).toBeInTheDocument();
    expect(screen.getByText('Legacy card')).toBeInTheDocument();
    expect(screen.queryByText('Regression test')).not.toBeInTheDocument();
  });
});

describe('SprintModal read-first inline editing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listSprintHistory.mockResolvedValue([]);
    apiMock.moveSprint.mockResolvedValue({});
  });

  it('renders Objective as read-first and saves a field-only patch', async () => {
    await renderSprint({ objective: 'Existing objective', expected_outcome: 'Existing expected outcome' });

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Existing objective'));

    const textbox = screen.getByDisplayValue('Existing objective');
    fireEvent.change(textbox, { target: { value: 'New objective' } });
    fireEvent.blur(textbox);

    await waitFor(() =>
      expect(apiMock.updateSprint).toHaveBeenCalledWith('sprint-1', {
        objective: 'New objective',
        expected_version: 1,
      }),
    );
    expect(apiMock.updateSprint.mock.calls[0][1]).not.toHaveProperty('expected_outcome');
    await waitFor(() => expect(apiMock.getSprint).toHaveBeenCalledTimes(2));
  });

  it('renders Expected Outcome as read-first and saves a field-only patch', async () => {
    await renderSprint({ objective: 'Existing objective', expected_outcome: 'Existing expected outcome' });

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Existing expected outcome'));

    const textbox = screen.getByDisplayValue('Existing expected outcome');
    fireEvent.change(textbox, { target: { value: 'New outcome' } });
    fireEvent.blur(textbox);

    await waitFor(() =>
      expect(apiMock.updateSprint).toHaveBeenCalledWith('sprint-1', {
        expected_outcome: 'New outcome',
        expected_version: 1,
      }),
    );
    expect(apiMock.updateSprint.mock.calls[0][1]).not.toHaveProperty('objective');
    await waitFor(() => expect(apiMock.getSprint).toHaveBeenCalledTimes(2));
  });

  it('shows placeholders for empty text fields without autosaving on render', async () => {
    await renderSprint({ objective: null, expected_outcome: null });

    expect(screen.getByText('What is this sprint trying to achieve?')).toBeInTheDocument();
    expect(screen.getByText('What should be deliverable at the end of this sprint?')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(apiMock.updateSprint).not.toHaveBeenCalled();
  });

  it('keeps sprint text read-only when edit_fields is false', async () => {
    permissionState.denied = new Set(['sprint.entity.edit_fields']);
    await renderSprint({ objective: 'Existing objective' });

    fireEvent.click(screen.getByText('Existing objective'));

    expect(screen.queryByDisplayValue('Existing objective')).not.toBeInTheDocument();
    expect(apiMock.updateSprint).not.toHaveBeenCalled();
  });

  it('disables sprint mutations when interact_in for its state is false', async () => {
    permissionState.denied = new Set(['sprint.interact_in.active']);
    await renderSprint({ status: 'active', objective: 'Existing objective' });

    expect(screen.queryByRole('button', { name: 'Review' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel Sprint' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Existing objective'));
    expect(screen.queryByDisplayValue('Existing objective')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^Cards/i }));
    expect(screen.getByRole('button', { name: /Assign Cards/i })).toBeDisabled();
    expect(apiMock.updateSprint).not.toHaveBeenCalled();
    expect(apiMock.moveSprint).not.toHaveBeenCalled();
  });
});

describe('SprintModal Policy Compliance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listSprintHistory.mockResolvedValue([]);
    apiMock.moveSprint.mockResolvedValue({});
    apiMock.assignTasksToSprint.mockResolvedValue({});
    apiMock.unassignTasksFromSprint.mockResolvedValue({});
  });

  it('exposes accessible Evaluation subtabs only with the exact read permission', async () => {
    permissionState.flags = new Set(['guidelines.assessments.read']);
    await renderSprint({ status: 'review', version: 7 });

    fireEvent.click(screen.getByRole('button', { name: /^Evaluations/i }));

    expect(
      screen.getByRole('tab', { name: 'Sprint Evaluation' }),
    ).toHaveAttribute('aria-selected', 'true');
    const policyTab = screen.getByRole('tab', {
      name: 'Policy Compliance',
    });
    expect(policyTab).toHaveAttribute('aria-selected', 'false');

    fireEvent.click(policyTab);

    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-board-id',
      'board-1',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-entity-type',
      'sprint',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-subject-id',
      'sprint-1',
    );
    expect(screen.getByTestId('policy-transition-preview')).toHaveAttribute(
      'data-status',
      'ready',
    );
    expect(apiMock.getAllowedTransitions).toHaveBeenCalledWith('board-1', {
      entity_type: 'sprint',
      entity_id: 'sprint-1',
      current_status: 'review',
    });
  });

  it('does not expose Policy Compliance for adjacent or absent permissions', async () => {
    permissionState.flags = new Set(['guidelines.read']);
    await renderSprint({ status: 'review' });

    fireEvent.click(screen.getByRole('button', { name: /^Evaluations/i }));

    expect(
      screen.getByRole('tab', { name: 'Sprint Evaluation' }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('tab', { name: 'Policy Compliance' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('policy-compliance-panel'),
    ).not.toBeInTheDocument();
  });

  it('fails closed on governed review-to-closed while retaining authorized recovery and cancellation', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue(
      transitionResponse('review', [
        transition('active', {
          gate: 'rework',
          capabilities: ['reopen'],
        }),
        transition('closed', {
          gate: 'sprint_completion',
          capabilities: ['complete'],
          policy_compliance: true,
          policy_compliance_decision: policyDecision(false),
        }),
        transition('cancelled', { gate: 'cancel' }),
      ]),
    );

    await renderSprint({ status: 'review' });

    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Closed' }),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Active' }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Cancel Sprint' }),
      ).toBeInTheDocument();
    });
  });

  it('enables governed review-to-closed only when the backend decision is actionable', async () => {
    await renderSprint({ status: 'review' });

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Closed' }),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole('button', { name: 'Cancel Sprint' }),
    ).toBeInTheDocument();
  });

  it('removes every lifecycle control when the authority envelope is malformed', async () => {
    permissionState.flags = new Set(['guidelines.assessments.read']);
    apiMock.getAllowedTransitions.mockResolvedValue({
      ...transitionResponse('review', defaultTransitions('review')),
      source: 'untrusted_source',
    });

    await renderSprint({ status: 'review' });

    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Closed' }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: 'Active' }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: 'Cancel Sprint' }),
      ).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /^Evaluations/i }));
    fireEvent.click(screen.getByRole('tab', {
      name: 'Policy Compliance',
    }));
    expect(screen.getByTestId('policy-transition-preview')).toHaveAttribute(
      'data-status',
      'error',
    );
  });

  it('does not invent a cancellation action when the backend omits that edge', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue(
      transitionResponse('review', [
        transition('active', {
          gate: 'rework',
          capabilities: ['reopen'],
        }),
      ]),
    );

    await renderSprint({ status: 'review' });

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Active' }),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByRole('button', { name: 'Cancel Sprint' }),
    ).not.toBeInTheDocument();
  });

  it('persists a structured 409 rejection and refreshes transition authority', async () => {
    permissionState.flags = new Set(['guidelines.assessments.read']);
    apiMock.moveSprint.mockRejectedValue(structuredPolicyRejection());
    await renderSprint({ status: 'review' });

    const closeAction = await screen.findByRole('button', {
      name: 'Closed',
    });
    fireEvent.click(closeAction);

    await waitFor(() => {
      expect(apiMock.moveSprint).toHaveBeenCalledWith('sprint-1', {
        status: 'closed',
        expected_version: 1,
      });
    });
    await waitFor(() => {
      expect(apiMock.getAllowedTransitions).toHaveBeenCalledTimes(2);
    });

    fireEvent.click(screen.getByRole('button', { name: /^Evaluations/i }));
    fireEvent.click(screen.getByRole('tab', {
      name: 'Policy Compliance',
    }));
    expect(screen.getByTestId('policy-transition-preview')).toHaveTextContent(
      'policy_compliance_blocked',
    );
  });

  it('refreshes evidence and lifecycle authority even when sprint.version is unchanged', async () => {
    permissionState.flags = new Set(['guidelines.assessments.read']);
    await renderSprint({ status: 'review', version: 4 });

    fireEvent.click(screen.getByRole('button', { name: /^Evaluations/i }));
    fireEvent.click(screen.getByRole('tab', {
      name: 'Policy Compliance',
    }));
    const firstRefreshKey = Number(
      screen.getByTestId('policy-compliance-panel')
        .getAttribute('data-refresh-key'),
    );

    fireEvent.click(screen.getByTitle('Refresh'));

    await waitFor(() => {
      expect(apiMock.getSprint).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      const nextRefreshKey = Number(
        screen.getByTestId('policy-compliance-panel')
          .getAttribute('data-refresh-key'),
      );
      expect(nextRefreshKey).toBeGreaterThan(firstRefreshKey);
    });
    await waitFor(() => {
      expect(apiMock.getAllowedTransitions).toHaveBeenCalledTimes(2);
    });
  });
});
