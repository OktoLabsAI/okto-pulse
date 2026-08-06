import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CardModal, TestEvidenceTab } from '../CardModal';
import type {
  AllowedTransition,
  AllowedTransitionsResponse,
  Card,
  CardSummary,
  CardStatus,
  PolicyComplianceTransitionDecision,
  TestScenario,
} from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';

const apiMock = vi.hoisted(() => ({
  getCard: vi.fn(),
  getSpec: vi.fn(),
  getSprint: vi.fn(),
  getSpecKnowledge: vi.fn(),
  getAllowedTransitions: vi.fn(),
  listAgentsForBoard: vi.fn(),
  getCardSeenStatus: vi.fn(),
  getCardDependencies: vi.fn(),
  getCardDependents: vi.fn(),
  getCardActivity: vi.fn(),
  getArchitectureDesign: vi.fn(),
  getBugRegressionScenarioCandidates: vi.fn(),
  listAmendmentRevisions: vi.fn().mockResolvedValue({
    board_id: 'b',
    bug_id: 'bug-1',
    revisions: [],
    path_b_resolution: { coverage_state: 'not_applicable' },
  }),
  createAmendmentRevision: vi.fn(),
  associateAmendmentRevisionArtifacts: vi.fn(),
  updateCard: vi.fn(),
  moveCard: vi.fn(),
  deleteCard: vi.fn(),
  uploadAttachment: vi.fn(),
  downloadAttachment: vi.fn(),
  deleteAttachment: vi.fn(),
  unlinkTestTaskFromBug: vi.fn(),
  submitTaskValidation: vi.fn(),
}));

const storeMock = vi.hoisted(() => ({
  selectedCardId: 'bug-1',
  isCardModalOpen: true,
  currentBoard: {
    id: 'board-1',
    settings: {
      min_confidence: 70,
      min_completeness: 80,
      max_drift: 50,
    },
  },
  columns: {} as Record<CardStatus, CardSummary[]>,
  closeCardModal: vi.fn(),
  removeCardFromColumn: vi.fn(),
  updateCardInColumn: vi.fn(),
}));

const markdownMock = vi.hoisted(() => ({
  exportCard: vi.fn(() => '# card export'),
  downloadMarkdown: vi.fn(),
  markdownFilenameForCard: vi.fn(() => 'bug_bug-traceability-is-hidden.md'),
}));

const cardKnowledgeTabMock = vi.hoisted(() => ({
  render: vi.fn(),
}));

const permissionsMock = vi.hoisted(() => ({
  has: vi.fn((_permission: string) => true),
}));

const policyComplianceMock = vi.hoisted(() => ({
  panelProps: vi.fn(),
  previewProps: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'full_control',
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: permissionsMock.has,
  }),
}));

vi.mock('@/lib/exportMarkdown', () => ({
  exportCard: markdownMock.exportCard,
  downloadMarkdown: markdownMock.downloadMarkdown,
  markdownFilenameForCard: markdownMock.markdownFilenameForCard,
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: () => ({
    closeCardModal: storeMock.closeCardModal,
    removeCardFromColumn: storeMock.removeCardFromColumn,
    updateCardInColumn: storeMock.updateCardInColumn,
  }),
  useSelectedCard: () => storeMock.selectedCardId,
  useIsCardModalOpen: () => storeMock.isCardModalOpen,
  useColumns: () => storeMock.columns,
  useCurrentBoard: () => storeMock.currentBoard,
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: ({ value, renderView, placeholder }: any) => (
    <div>{value ? renderView(value) : placeholder}</div>
  ),
}));

vi.mock('@/components/shared/MarkdownContent', () => ({
  MarkdownContent: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock('@/components/specs/MockupsTab', () => ({
  MockupsTab: () => <div />,
}));

vi.mock('@/components/resources/ResourceGateDisclosure', () => ({
  ResourceGateDisclosure: () => (
    <div data-testid="resource-gate-disclosure" />
  ),
}));

vi.mock('@/components/specs/SpecModal', () => ({
  SpecModal: () => <div />,
}));

vi.mock('@/components/policy-compliance', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@/components/policy-compliance')>();
  return {
    ...actual,
    PolicyCompliancePanel: (props: {
      boardId: string;
      entityType: string;
      subjectId: string;
      refreshKey?: number;
      onEvaluated?: () => void;
      onRefreshed?: () => void;
    }) => {
      policyComplianceMock.panelProps(props);
      return (
        <div data-testid="policy-compliance-panel">
          <button type="button" onClick={() => props.onEvaluated?.()}>
            Policy evaluated
          </button>
          <button type="button" onClick={() => props.onRefreshed?.()}>
            Policy refreshed
          </button>
        </div>
      );
    },
    PolicyComplianceTransitionPreview: (props: {
      preview: { status: string };
      rejection?: { code: string } | null;
    }) => {
      policyComplianceMock.previewProps(props);
      return (
        <div data-testid="policy-transition-preview">
          {props.preview.status}:{props.rejection?.code ?? 'none'}
        </div>
      );
    },
  };
});

vi.mock('../CardKnowledgeTab', () => ({
  CardKnowledgeTab: (props: {
    onBusyChange?: (busy: boolean) => void;
    onUpdate?: () => Promise<void>;
  }) => {
    cardKnowledgeTabMock.render(props);
    return (
      <div data-testid="card-knowledge-tab">
        {(['assign', 'drop', 'refresh'] as const).map((operation) => (
          <button
            key={operation}
            type="button"
            onClick={() => props.onBusyChange?.(true)}
          >
            Begin {operation}
          </button>
        ))}
        <button
          type="button"
          onClick={() => props.onBusyChange?.(false)}
        >
          Finish knowledge operation
        </button>
        <button
          type="button"
          onClick={() => void props.onUpdate?.()}
        >
          Complete knowledge mutation
        </button>
      </div>
    );
  },
}));

vi.mock('@/components/architecture', () => ({
  ArchitectureTab: () => <div />,
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

const emptyColumns = (): Record<CardStatus, CardSummary[]> => ({
  not_started: [],
  started: [],
  in_progress: [],
  validation: [],
  on_hold: [],
  done: [],
  cancelled: [],
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flushMicrotasks() {
  await act(async () => {
    for (let index = 0; index < 8; index += 1) {
      await Promise.resolve();
    }
  });
}

const bugCard: Card = {
  id: 'bug-1',
  board_id: 'board-1',
  spec_id: 'spec-1',
  sprint_id: null,
  title: 'Bug: traceability is hidden',
  description: 'Bug description',
  details: null,
  status: 'not_started',
  priority: 'medium',
  position: 0,
  assignee_id: null,
  created_by: 'agent-1',
  created_at: '2026-05-06T10:00:00Z',
  updated_at: '2026-05-06T10:00:00Z',
  due_date: null,
  labels: [],
  test_scenario_ids: null,
  screen_mockups: [],
  knowledge_bases: [],
  conclusions: [],
  attachments: [],
  qa_items: [],
  comments: [],
  architecture_designs: [],
  card_type: 'bug',
  origin_task_id: 'task-1',
  severity: 'major',
  expected_behavior: 'Associations should be visible',
  observed_behavior: 'Associations are hard to find',
  steps_to_reproduce: null,
  action_plan: null,
  linked_test_task_ids: ['test-1'],
  validations: [],
};

function cardForType(cardType: 'normal' | 'bug' | 'test'): Card {
  if (cardType === 'bug') return { ...bugCard };
  return {
    ...bugCard,
    id: `${cardType}-1`,
    title: cardType === 'test' ? 'Regression card' : 'Implementation card',
    card_type: cardType,
    origin_task_id: null,
    severity: null,
    expected_behavior: null,
    observed_behavior: null,
    linked_test_task_ids: null,
    test_scenario_ids: cardType === 'test' ? ['ts-1'] : [],
  };
}

function allowedTransition(
  toStatus: CardStatus,
  overrides: Partial<AllowedTransition> = {},
): AllowedTransition {
  return {
    to_status: toStatus,
    label: STATUS_LABELS_FOR_TEST[toStatus],
    gate: 'none',
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

const STATUS_LABELS_FOR_TEST: Record<CardStatus, string> = {
  not_started: 'Not Started',
  started: 'Started',
  in_progress: 'In Progress',
  validation: 'Validation',
  on_hold: 'On Hold',
  done: 'Done',
  cancelled: 'Cancelled',
};

function transitionEnvelope(
  entityId: string,
  currentStatus: CardStatus,
  allowedTransitions: AllowedTransition[],
): AllowedTransitionsResponse {
  return {
    board_id: 'board-1',
    entity_type: 'card',
    entity_id: entityId,
    current_status: currentStatus,
    allowed_transitions: allowedTransitions,
    source: 'core_sdlc_registry_v1',
  };
}

function policyDecision(
  state:
    | 'policy_compliance_blocked'
    | 'policy_compliance_ready',
): PolicyComplianceTransitionDecision {
  const blocked = state === 'policy_compliance_blocked';
  return {
    state,
    allowed: !blocked,
    policy_compliance_required: true,
    reason_codes: [state],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_ids: ['receipt-card-1'],
    currentness: 'current',
    currentness_reasons: [],
    applicable_metric_count: 2,
    applicable_blocking_metric_count: 2,
    failed_metric_count: blocked ? 1 : 0,
    blocking_metric_count: blocked ? 1 : 0,
    waived_metric_count: 0,
    advisory_issue_count: 0,
    skipped_binding_count: 0,
    diagnostic_codes: blocked ? ['policy_metric_threshold_failed'] : [],
    binding_decisions: [{
      binding_id: 'binding-card-1',
      guideline_id: 'guideline-card-1',
      enforcement: 'blocking',
      applicable_metric_count: 2,
      allowed: !blocked,
      assessment_available: true,
      receipt_id: 'receipt-card-1',
      currentness: 'current',
      currentness_reasons: [],
      inadmissibility_cause: null,
      failed_metric_count: blocked ? 1 : 0,
      waived_metric_count: 0,
      blocking_metric_count: blocked ? 1 : 0,
      advisory_issue_count: 0,
      skipped: false,
      diagnostic_codes:
        blocked ? ['policy_metric_threshold_failed'] : [],
    }],
  };
}

function policyRejection(
  cardId: string,
  fromStatus: CardStatus,
  toStatus: CardStatus,
): AuthenticatedFetchError {
  return new AuthenticatedFetchError({
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
      binding_decisions: [{
        binding_id: 'binding-card-1',
        guideline_id: 'guideline-card-1',
        enforcement: 'blocking',
        applicable_metric_count: 2,
        allowed: false,
        assessment_available: true,
        receipt_id: 'receipt-card-1',
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
        entity_type: 'card',
        subject_id: cardId,
        from_status: fromStatus,
        to_status: toStatus,
      },
      policy_compliance_required: true,
    },
  });
}

describe('CardModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionsMock.has.mockImplementation((_permission: string) => true);
    storeMock.selectedCardId = 'bug-1';
    storeMock.isCardModalOpen = true;
    storeMock.currentBoard.settings = {
      min_confidence: 70,
      min_completeness: 80,
      max_drift: 50,
    };
    storeMock.columns = emptyColumns();
    storeMock.columns.in_progress = [
      {
        id: 'task-1',
        board_id: 'board-1',
        spec_id: 'spec-1',
        title: 'Implement story lineage',
        description: null,
        status: 'in_progress',
        priority: 'high',
        position: 0,
        assignee_id: null,
        created_by: 'agent-1',
        created_at: '2026-05-06T09:00:00Z',
        updated_at: '2026-05-06T09:00:00Z',
        due_date: null,
        labels: [],
        test_scenario_ids: [],
        conclusions: [],
        card_type: 'normal',
      },
    ];
    storeMock.columns.started = [
      {
        id: 'test-1',
        board_id: 'board-1',
        spec_id: 'spec-1',
        title: 'Regression: story lineage is visible',
        description: null,
        status: 'started',
        priority: 'medium',
        position: 0,
        assignee_id: null,
        created_by: 'agent-1',
        created_at: '2026-05-06T09:30:00Z',
        updated_at: '2026-05-06T09:30:00Z',
        due_date: null,
        labels: [],
        test_scenario_ids: ['ts-1'],
        conclusions: [],
        card_type: 'test',
      },
    ];

    apiMock.getCard.mockResolvedValue(bugCard);
    apiMock.getAllowedTransitions.mockResolvedValue(
      transitionEnvelope('bug-1', 'not_started', [
        allowedTransition('started'),
        allowedTransition('cancelled'),
      ]),
    );
    apiMock.getSpec.mockResolvedValue({
      id: 'spec-1',
      title: 'Stories spec',
      test_scenarios: [],
      business_rules: [],
      api_contracts: [],
      technical_requirements: [],
      knowledge_bases: [],
    });
    apiMock.getSprint.mockResolvedValue({
      id: 'sprint-1',
      spec_id: 'spec-1',
      board_id: 'board-1',
    });
    apiMock.getSpecKnowledge.mockResolvedValue(null);
    apiMock.listAgentsForBoard.mockResolvedValue([]);
    apiMock.getCardSeenStatus.mockResolvedValue({ items: {} });
    apiMock.getCardDependencies.mockResolvedValue([]);
    apiMock.getCardDependents.mockResolvedValue([]);
    apiMock.getCardActivity.mockResolvedValue([]);
    apiMock.getBugRegressionScenarioCandidates.mockResolvedValue({
      bug_id: 'bug-1',
      spec_id: 'spec-1',
      origin_task_id: 'task-1',
      affected_task_ids: [],
      eligible_scenarios: [
        {
          scenario_id: 'ts-1',
          title: 'Regression: story lineage is visible',
          reason: 'origin_task_direct',
          source_task_id: 'task-1',
        },
      ],
      rejected_scenarios: [],
      next_action: 'create_regression_test_card',
      semantic_gap_required: false,
      spec_mutation_required: false,
      remediation: {
        reason_code: 'origin_task_direct',
        remediation_path: 'path_a_reuse_existing_scenario',
        next_action: 'create_regression_test_card',
        semantic_gap_required: false,
        eligible_scenarios_count: 1,
        hotfix_lane_status: 'not_applicable',
        message: 'Create a fresh regression test card that references one of the eligible existing scenarios.',
        detail: 'This is Path A: reuse an existing scenario linked to the bug origin task.',
        actions: [
          {
            action_id: 'create_regression_test_card',
            label: 'Create regression test card',
            description: 'Create a new test card in the bug spec using an eligible scenario id.',
            primary: true,
          },
        ],
        facts: {},
      },
    });
    apiMock.getArchitectureDesign.mockImplementation((id: string) =>
      Promise.resolve({ id, entities: [], interfaces: [], diagrams: [] }),
    );
    markdownMock.exportCard.mockReturnValue('# card export');
    markdownMock.markdownFilenameForCard.mockReturnValue('bug_bug-traceability-is-hidden.md');
  });

  it.each([
    {
      cardType: 'normal' as const,
      hasTests: false,
      hasTaskValidation: true,
    },
    {
      cardType: 'bug' as const,
      hasTests: true,
      hasTaskValidation: true,
    },
    {
      cardType: 'test' as const,
      hasTests: true,
      hasTaskValidation: false,
    },
  ])(
    'renders the canonical top-level workspace for a $cardType card',
    async ({ cardType, hasTests, hasTaskValidation }) => {
      const selectedCard = cardForType(cardType);
      storeMock.selectedCardId = selectedCard.id;
      apiMock.getCard.mockResolvedValue(selectedCard);

      render(<CardModal boardId="board-1" />);

      const tabs = await screen.findByRole('tablist', {
        name: 'Card sections',
      });
      expect(within(tabs).getByRole('tab', { name: /^Details$/ })).toBeInTheDocument();
      expect(within(tabs).getByRole('tab', { name: /^Resources/ })).toBeInTheDocument();
      expect(within(tabs).getByRole('tab', { name: /^Q&A/ })).toBeInTheDocument();
      expect(within(tabs).getByRole('tab', { name: /^Comments/ })).toBeInTheDocument();
      expect(within(tabs).getByRole('tab', { name: /^References/ })).toBeInTheDocument();
      expect(within(tabs).getByRole('tab', { name: /^Validation/ })).toBeInTheDocument();
      expect(within(tabs).getByRole('tab', { name: /^Activity/ })).toBeInTheDocument();
      expect(within(tabs).queryByRole('tab', { name: /^Tests/ }) !== null).toBe(hasTests);
      expect(within(tabs).queryByRole('tab', { name: /Cancellation/i })).not.toBeInTheDocument();

      fireEvent.click(within(tabs).getByRole('tab', { name: /^Validation/ }));
      const validationTabs = await screen.findByRole('tablist', {
        name: 'Card validation sections',
      });
      expect(
        within(validationTabs).getByRole('tab', { name: /^Execution report/ }),
      ).toBeInTheDocument();
      expect(
        within(validationTabs).queryByRole('tab', { name: /^Task validation/ }) !== null,
      ).toBe(hasTaskValidation);
      expect(
        within(validationTabs).getByRole('tab', {
          name: /^Policy Compliance$/,
        }),
      ).toBeInTheDocument();
    },
  );

  it('keeps cancellation audit context in Details instead of creating a transient tab', async () => {
    const cancelledCard: Card = {
      ...cardForType('normal'),
      id: 'cancelled-1',
      status: 'cancelled',
      cancellation_reason: 'The implementation was superseded by a safer approach.',
      cancelled_by: 'agent-1',
      cancelled_at: '2026-07-28T13:00:00Z',
    };
    storeMock.selectedCardId = cancelledCard.id;
    apiMock.getCard.mockResolvedValue(cancelledCard);
    apiMock.getAllowedTransitions.mockResolvedValue(
      transitionEnvelope(cancelledCard.id, 'cancelled', []),
    );

    render(<CardModal boardId="board-1" />);

    const tabs = await screen.findByRole('tablist', {
      name: 'Card sections',
    });
    expect(within(tabs).queryByRole('tab', { name: /Cancellation/i })).not.toBeInTheDocument();
    expect(await screen.findByTestId('cancellation-details')).toBeVisible();
    expect(
      screen.getByText('The implementation was superseded by a safer approach.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Cancelling a card can return a validated parent spec/i),
    ).toBeInTheDocument();
  });

  it('shows only the current and canonically allowed lifecycle statuses', async () => {
    render(<CardModal boardId="board-1" />);

    const status = await screen.findByRole('combobox', {
      name: 'Card status',
    });
    expect(
      within(status).getAllByRole('option').map((option) => option.textContent),
    ).toEqual(['Not Started', 'Started', 'Cancelled']);
    expect(apiMock.getAllowedTransitions).toHaveBeenCalledWith('board-1', {
      entity_type: 'card',
      entity_id: 'bug-1',
      current_status: 'not_started',
    });
  });

  it('fails closed and excludes a policy-blocked transition from the lifecycle selector', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue(
      transitionEnvelope('bug-1', 'not_started', [
        allowedTransition('started', {
          gate: 'policy_compliance',
          blocked_reason: 'Policy Compliance blocked this transition.',
          reason_codes: ['policy_compliance_blocked'],
          policy_compliance: true,
          policy_compliance_decision: policyDecision(
            'policy_compliance_blocked',
          ),
        }),
        allowedTransition('cancelled'),
      ]),
    );

    render(<CardModal boardId="board-1" />);

    const status = await screen.findByRole('combobox', {
      name: 'Card status',
    });
    await waitFor(() => expect(status).not.toBeDisabled());
    expect(
      within(status).getAllByRole('option').map((option) => option.textContent),
    ).toEqual(['Not Started', 'Cancelled']);

    fireEvent.click(screen.getByRole('tab', { name: /^Validation/ }));
    fireEvent.click(
      screen.getByRole('tab', { name: /^Policy Compliance$/ }),
    );
    expect(await screen.findByTestId('policy-transition-preview')).toHaveTextContent(
      'ready:none',
    );
  });

  it('keeps lifecycle fail-closed when the canonical transition envelope is malformed', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue({
      ...transitionEnvelope('bug-1', 'not_started', [
        allowedTransition('started'),
      ]),
      source: 'legacy_client_map',
    });

    render(<CardModal boardId="board-1" />);

    const status = await screen.findByRole('combobox', {
      name: 'Card status',
    });
    await waitFor(() => expect(status).toBeDisabled());
    expect(
      within(status).getAllByRole('option').map((option) => option.textContent),
    ).toEqual(['Not Started']);

    fireEvent.click(screen.getByRole('tab', { name: /^Validation/ }));
    fireEvent.click(
      screen.getByRole('tab', { name: /^Policy Compliance$/ }),
    );
    expect(await screen.findByTestId('policy-transition-preview')).toHaveTextContent(
      'error:none',
    );
  });

  it('keeps a structured policy 409 visible after reloading transition authority', async () => {
    apiMock.moveCard.mockRejectedValueOnce(
      policyRejection('bug-1', 'not_started', 'started'),
    );

    render(<CardModal boardId="board-1" />);

    const status = await screen.findByRole('combobox', {
      name: 'Card status',
    });
    await waitFor(() => expect(status).not.toBeDisabled());
    fireEvent.change(status, { target: { value: 'started' } });

    await waitFor(() => expect(apiMock.moveCard).toHaveBeenCalledWith(
      'bug-1',
      expect.objectContaining({ status: 'started' }),
    ));
    fireEvent.click(screen.getByRole('tab', { name: /^Validation/ }));
    fireEvent.click(
      screen.getByRole('tab', { name: /^Policy Compliance$/ }),
    );
    await waitFor(() => expect(
      screen.getByTestId('policy-transition-preview'),
    ).toHaveTextContent('ready:policy_compliance_blocked'));
    expect(apiMock.getAllowedTransitions.mock.calls.length).toBeGreaterThan(1);
  });

  it('exposes Policy Compliance as the only Validation workspace for a restricted test card', async () => {
    const testCard = cardForType('test');
    storeMock.selectedCardId = testCard.id;
    apiMock.getCard.mockResolvedValue(testCard);
    apiMock.getAllowedTransitions.mockResolvedValue(
      transitionEnvelope(testCard.id, 'not_started', [
        allowedTransition('started'),
      ]),
    );
    permissionsMock.has.mockImplementation((permission: string) =>
      permission === 'guidelines.assessments.read'
      || ![
        'card.conclusion.read',
        'card.validation.read',
      ].includes(permission)
    );

    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /^Validation/ }));
    const validationTabs = await screen.findByRole('tablist', {
      name: 'Card validation sections',
    });
    expect(
      within(validationTabs).getAllByRole('tab').map((tab) => tab.textContent),
    ).toEqual(['Policy Compliance']);
    fireEvent.click(
      within(validationTabs).getByRole('tab', {
        name: 'Policy Compliance',
      }),
    );
    expect(await screen.findByTestId('policy-compliance-panel')).toBeVisible();
    expect(policyComplianceMock.panelProps).toHaveBeenLastCalledWith(
      expect.objectContaining({
        boardId: 'board-1',
        entityType: 'card',
        subjectId: testCard.id,
      }),
    );
  });

  it('refreshes lifecycle authority after Policy Compliance evaluation and refresh', async () => {
    render(<CardModal boardId="board-1" />);
    fireEvent.click(await screen.findByRole('tab', { name: /^Validation/ }));
    fireEvent.click(
      screen.getByRole('tab', { name: /^Policy Compliance$/ }),
    );
    await screen.findByTestId('policy-compliance-panel');
    const initialCalls = apiMock.getAllowedTransitions.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: 'Policy evaluated' }));
    await waitFor(() => expect(
      apiMock.getAllowedTransitions.mock.calls.length,
    ).toBeGreaterThan(initialCalls));
    const afterEvaluation = apiMock.getAllowedTransitions.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: 'Policy refreshed' }));
    await waitFor(() => expect(
      apiMock.getAllowedTransitions.mock.calls.length,
    ).toBeGreaterThan(afterEvaluation));
  });

  it('refreshes lifecycle authority after a Knowledge resource mutation', async () => {
    render(<CardModal boardId="board-1" />);
    fireEvent.click(await screen.findByRole('tab', { name: /^Validation/ }));
    fireEvent.click(
      screen.getByRole('tab', { name: /^Policy Compliance$/ }),
    );
    await screen.findByTestId('policy-compliance-panel');
    const initialCalls = apiMock.getAllowedTransitions.mock.calls.length;

    fireEvent.click(screen.getByRole('tab', { name: /^Resources/ }));
    fireEvent.click(await screen.findByRole('tab', { name: /^Knowledge/ }));
    fireEvent.click(
      screen.getByRole('button', { name: 'Complete knowledge mutation' }),
    );

    await waitFor(() => expect(
      apiMock.getAllowedTransitions.mock.calls.length,
    ).toBeGreaterThan(initialCalls));
  });

  it('ignores a stale allowed-transitions response after switching cards', async () => {
    const firstCard = {
      ...cardForType('normal'),
      id: 'transition-card-a',
      title: 'Transition card A',
    };
    const secondCard = {
      ...cardForType('normal'),
      id: 'transition-card-b',
      title: 'Transition card B',
    };
    let resolveFirstTransitions:
      ((value: AllowedTransitionsResponse) => void) | undefined;
    const delayedFirstTransitions =
      new Promise<AllowedTransitionsResponse>((resolve) => {
      resolveFirstTransitions = resolve;
    });
    storeMock.selectedCardId = firstCard.id;
    apiMock.getCard.mockImplementation((cardId: string) =>
      Promise.resolve(cardId === firstCard.id ? firstCard : secondCard)
    );
    apiMock.getAllowedTransitions.mockImplementation(
      (_boardId: string, request: { entity_id: string }) =>
        request.entity_id === firstCard.id
          ? delayedFirstTransitions
          : Promise.resolve(
              transitionEnvelope(secondCard.id, 'not_started', [
                allowedTransition('started'),
              ]),
            ),
    );

    const view = render(<CardModal boardId="board-1" />);
    await screen.findByText(firstCard.title);
    storeMock.selectedCardId = secondCard.id;
    view.rerender(<CardModal boardId="board-1" />);
    await screen.findByText(secondCard.title);

    await waitFor(() => {
      const status = screen.getByRole('combobox', { name: 'Card status' });
      expect(
        within(status).getAllByRole('option').map((option) => option.textContent),
      ).toEqual(['Not Started', 'Started']);
    });

    resolveFirstTransitions?.(
      transitionEnvelope(firstCard.id, 'not_started', [
        allowedTransition('cancelled'),
      ]),
    );
    await Promise.resolve();

    const status = screen.getByRole('combobox', { name: 'Card status' });
    expect(
      within(status).getAllByRole('option').map((option) => option.textContent),
    ).toEqual(['Not Started', 'Started']);
  });

  it('keeps read-only collaboration and requirement workspaces non-mutating', async () => {
    const readOnlyCard: Card = {
      ...cardForType('normal'),
      id: 'read-only-1',
      qa_items: [{
        id: 'qa-1',
        card_id: 'read-only-1',
        question: 'Who validates the rollout evidence?',
        answer: null,
        asked_by: 'agent-1',
        answered_by: null,
        created_at: '2026-07-28T11:00:00Z',
        answered_at: null,
      }],
      comments: [{
        id: 'comment-1',
        card_id: 'read-only-1',
        content: 'Choose the release window',
        author_id: 'agent-1',
        comment_type: 'choice',
        choices: [
          { id: 'morning', label: 'Morning' },
          { id: 'evening', label: 'Evening' },
        ],
        responses: [],
        allow_free_text: true,
        created_at: '2026-07-28T11:30:00Z',
        updated_at: '2026-07-28T11:30:00Z',
      }],
    };
    const deniedWrites = new Set([
      'card.entity.edit_fields',
      'card.qa.ask',
      'card.qa.answer',
      'card.comments.create',
      'card.comments.create_choice',
      'card.comments.respond_choice',
    ]);
    permissionsMock.has.mockImplementation(
      (permission: string) => !deniedWrites.has(permission),
    );
    storeMock.selectedCardId = readOnlyCard.id;
    apiMock.getCard.mockResolvedValue(readOnlyCard);

    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /^Q&A/ }));
    expect(
      screen.queryByPlaceholderText('Add a question... (use @ to mention)'),
    ).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Answer...')).not.toBeInTheDocument();
    expect(
      screen.getByText('Awaiting an answer from an authorized contributor.'),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: /^Comments/ }));
    expect(
      screen.queryByPlaceholderText('Write a comment... (use @ to mention)'),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Submit Response' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Morning' })).toBeDisabled();

    fireEvent.click(screen.getByRole('tab', { name: /^References/ }));
    fireEvent.click(screen.getByRole('tab', { name: /^Requirements/ }));
    expect(
      screen.queryByRole('switch', {
        name: 'Skip task requirement link gate for this card',
      }),
    ).not.toBeInTheDocument();
    expect(screen.getByText('Disabled')).toBeInTheDocument();
    expect(apiMock.updateCard).not.toHaveBeenCalled();
  });

  it('does not leak knowledge roots from a previous card after switching to an orphan card', async () => {
    const sourceCard: Card = {
      ...cardForType('normal'),
      id: 'card-with-spec',
      title: 'Card with source spec',
      spec_id: 'spec-a',
    };
    const orphanCard: Card = {
      ...cardForType('normal'),
      id: 'orphan-card',
      title: 'Orphan card',
      spec_id: null,
    };
    let resolveOldKnowledge: ((value: {
      id: string;
      title: string;
      content: string;
    }) => void) | undefined;
    const delayedOldKnowledge = new Promise<{
      id: string;
      title: string;
      content: string;
    }>((resolve) => {
      resolveOldKnowledge = resolve;
    });
    storeMock.selectedCardId = sourceCard.id;
    apiMock.getCard.mockImplementation((cardId: string) =>
      Promise.resolve(cardId === sourceCard.id ? sourceCard : orphanCard)
    );
    apiMock.getSpec.mockResolvedValue({
      id: 'spec-a',
      title: 'Source spec',
      test_scenarios: [],
      business_rules: [],
      api_contracts: [],
      technical_requirements: [],
      knowledge_bases: [{ id: 'old-kb', title: 'Old root' }],
    });
    apiMock.getSpecKnowledge.mockReturnValue(delayedOldKnowledge);

    const view = render(<CardModal boardId="board-1" />);
    await screen.findByText('Card with source spec');
    await waitFor(() => {
      expect(apiMock.getSpecKnowledge).toHaveBeenCalledWith('spec-a', 'old-kb');
    });

    storeMock.selectedCardId = orphanCard.id;
    view.rerender(<CardModal boardId="board-1" />);
    await screen.findByText('Orphan card');
    resolveOldKnowledge?.({
      id: 'old-kb',
      title: 'Old root',
      content: 'Must not leak into the orphan card.',
    });

    fireEvent.click(await screen.findByRole('tab', { name: /^Resources/ }));
    fireEvent.click(await screen.findByRole('tab', { name: /^Knowledge/ }));
    await waitFor(() => {
      expect(cardKnowledgeTabMock.render).toHaveBeenLastCalledWith(
        expect.objectContaining({
          card: expect.objectContaining({ id: orphanCard.id }),
          specKnowledgeBases: [],
        }),
      );
    });
  });

  it('submits the explicit task-validation contract from circular score inputs', async () => {
    const validationCard: Card = {
      ...cardForType('normal'),
      id: 'validation-1',
      status: 'validation',
    };
    storeMock.selectedCardId = validationCard.id;
    apiMock.getCard.mockResolvedValue(validationCard);
    apiMock.submitTaskValidation.mockResolvedValue({
      id: 'validation-entry-1',
      confidence: 91,
      estimated_completeness: 89,
      estimated_drift: 8,
      confidence_justification: 'Confidence is supported by the full suite.',
      completeness_justification: 'Every acceptance criterion has evidence.',
      drift_justification: 'Only intentional implementation details differ.',
      general_justification: 'The implementation is ready for independent approval.',
      recommendation: 'approve',
      verdict: 'pass',
      evaluator_id: 'agent-1',
      created_at: '2026-07-28T14:00:00Z',
      resolved_thresholds: {
        min_confidence: 80,
        min_completeness: 80,
        max_drift: 20,
      },
      threshold_violations: [],
      card_status: 'done',
    });

    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /^Validation/ }));
    fireEvent.click(await screen.findByRole('tab', { name: /^Task validation/ }));

    expect(
      screen.getByRole('img', {
        name: /Confidence score 80 out of 100, higher is better/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('img', {
        name: /Drift score 20 out of 100, lower is better/i,
      }),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Confidence score'), {
      target: { value: '91' },
    });
    fireEvent.change(screen.getByLabelText('Completeness score'), {
      target: { value: '89' },
    });
    fireEvent.change(screen.getByLabelText('Drift score'), {
      target: { value: '8' },
    });
    fireEvent.change(
      screen.getByPlaceholderText('Justify the confidence score...'),
      { target: { value: 'Confidence is supported by the full suite.' } },
    );
    fireEvent.change(
      screen.getByPlaceholderText('Justify the completeness score...'),
      { target: { value: 'Every acceptance criterion has evidence.' } },
    );
    fireEvent.change(
      screen.getByPlaceholderText('Justify the drift score...'),
      { target: { value: 'Only intentional implementation details differ.' } },
    );
    fireEvent.change(
      screen.getByPlaceholderText('Overall validation summary...'),
      {
        target: {
          value: 'The implementation is ready for independent approval.',
        },
      },
    );
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Submit Validation (Approve)',
      }),
    );

    await waitFor(() => {
      expect(apiMock.submitTaskValidation).toHaveBeenCalledWith(
        validationCard.id,
        {
          confidence: 91,
          confidence_justification: 'Confidence is supported by the full suite.',
          estimated_completeness: 89,
          completeness_justification: 'Every acceptance criterion has evidence.',
          estimated_drift: 8,
          drift_justification: 'Only intentional implementation details differ.',
          general_justification: 'The implementation is ready for independent approval.',
          recommendation: 'approve',
        },
      );
    });
  });

  it('shows the active board thresholds in the task-validation form', async () => {
    const validationCard: Card = {
      ...cardForType('normal'),
      id: 'validation-thresholds-1',
      status: 'validation',
    };
    storeMock.currentBoard.settings = {
      min_confidence: 85,
      min_completeness: 90,
      max_drift: 10,
    };
    storeMock.selectedCardId = validationCard.id;
    apiMock.getCard.mockResolvedValue(validationCard);

    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /^Validation/ }));
    fireEvent.click(await screen.findByRole('tab', { name: /^Task validation/ }));

    expect(
      await screen.findByRole('img', {
        name: /Confidence score 80 out of 100.*Minimum 85.*threshold not met/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('img', {
        name: /Completeness score 80 out of 100.*Minimum 90.*threshold not met/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('img', {
        name: /Drift score 20 out of 100.*Maximum 10.*threshold not met/i,
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText('No threshold configured')).not.toBeInTheDocument();
  });

  it('resolves each task-validation threshold through sprint, spec, and board overrides', async () => {
    const validationCard: Card = {
      ...cardForType('normal'),
      id: 'validation-mixed-thresholds-1',
      status: 'validation',
      sprint_id: 'sprint-1',
    };
    storeMock.currentBoard.settings = {
      min_confidence: 70,
      min_completeness: 80,
      max_drift: 12,
    };
    storeMock.selectedCardId = validationCard.id;
    apiMock.getCard.mockResolvedValue(validationCard);
    apiMock.getSpec.mockResolvedValue({
      id: 'spec-1',
      title: 'Stories spec',
      validation_min_confidence: 88,
      validation_min_completeness: 92,
      validation_max_drift: null,
      test_scenarios: [],
      business_rules: [],
      api_contracts: [],
      technical_requirements: [],
      knowledge_bases: [],
    });
    apiMock.getSprint.mockResolvedValue({
      id: 'sprint-1',
      spec_id: 'spec-1',
      board_id: 'board-1',
      validation_min_confidence: 95,
      validation_min_completeness: null,
      validation_max_drift: null,
    });

    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /^Validation/ }));
    fireEvent.click(await screen.findByRole('tab', { name: /^Task validation/ }));

    expect(
      await screen.findByRole('img', {
        name: /Confidence score 80 out of 100.*Minimum 95.*threshold not met/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('img', {
        name: /Completeness score 80 out of 100.*Minimum 92.*threshold not met/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('img', {
        name: /Drift score 20 out of 100.*Maximum 12.*threshold not met/i,
      }),
    ).toBeInTheDocument();
    expect(screen.getByTestId('task-validation-confidence-threshold-source'))
      .toHaveTextContent('Threshold source: sprint');
    expect(screen.getByTestId('task-validation-completeness-threshold-source'))
      .toHaveTextContent('Threshold source: spec');
    expect(screen.getByTestId('task-validation-drift-threshold-source'))
      .toHaveTextContent('Threshold source: board');
    expect(apiMock.getSprint).toHaveBeenCalledWith('sprint-1');
  });

  it('fails closed when threshold authority cannot load and retries explicitly', async () => {
    const validationCard: Card = {
      ...cardForType('normal'),
      id: 'validation-threshold-retry-1',
      status: 'validation',
      sprint_id: 'sprint-threshold-retry',
    };
    storeMock.selectedCardId = validationCard.id;
    apiMock.getCard.mockResolvedValue(validationCard);
    apiMock.getSprint.mockRejectedValueOnce(new Error('sprint unavailable'));

    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /^Validation/ }));
    fireEvent.click(await screen.findByRole('tab', { name: /^Task validation/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load the authoritative Spec/Sprint validation thresholds.',
    );
    expect(
      screen.queryByRole('button', { name: /Submit Validation/ }),
    ).not.toBeInTheDocument();

    apiMock.getSprint.mockResolvedValue({
      id: 'sprint-threshold-retry',
      spec_id: 'spec-1',
      board_id: 'board-1',
      validation_min_confidence: 96,
      validation_min_completeness: 94,
      validation_max_drift: 6,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry thresholds' }));

    expect(
      await screen.findByRole('img', {
        name: /Confidence score 80 out of 100.*Minimum 96.*threshold not met/i,
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('ignores an older same-card polling response that finishes last', async () => {
    vi.useFakeTimers();
    const staleRefresh = deferred<Card>();
    const currentRefresh = deferred<Card>();
    const initialCard: Card = {
      ...cardForType('normal'),
      id: 'validation-poll-generation-1',
      status: 'validation',
      updated_at: '2026-08-06T10:00:00Z',
    };
    const staleCard = {
      ...initialCard,
      updated_at: '2026-08-06T10:01:00Z',
    };
    const currentCard = {
      ...initialCard,
      updated_at: '2026-08-06T10:02:00Z',
    };
    const initialSpec = {
      id: 'spec-1',
      title: 'Initial authority',
      validation_min_confidence: 71,
      test_scenarios: [],
      business_rules: [],
      api_contracts: [],
      technical_requirements: [],
      knowledge_bases: [],
    };
    const currentSpec = {
      ...initialSpec,
      title: 'Current authority',
      validation_min_confidence: 97,
    };
    const currentSpecResponse = deferred<typeof currentSpec>();
    storeMock.selectedCardId = initialCard.id;
    apiMock.getCard
      .mockResolvedValueOnce(initialCard)
      .mockImplementationOnce(() => staleRefresh.promise)
      .mockImplementationOnce(() => currentRefresh.promise);
    apiMock.getSpec
      .mockResolvedValueOnce(initialSpec)
      .mockImplementationOnce(() => currentSpecResponse.promise);

    const view = render(<CardModal boardId="board-1" />);
    try {
      await flushMicrotasks();
      fireEvent.click(screen.getByRole('tab', { name: /^Validation/ }));
      fireEvent.click(screen.getByRole('tab', { name: /^Task validation/ }));
      expect(
        screen.getByRole('img', {
          name: /Confidence score 80 out of 100.*Minimum 71.*threshold met/i,
        }),
      ).toBeInTheDocument();
      fireEvent.change(
        screen.getByPlaceholderText('Justify the confidence score...'),
        { target: { value: 'Current confidence evidence.' } },
      );
      fireEvent.change(
        screen.getByPlaceholderText('Justify the completeness score...'),
        { target: { value: 'Current completeness evidence.' } },
      );
      fireEvent.change(
        screen.getByPlaceholderText('Justify the drift score...'),
        { target: { value: 'Current drift evidence.' } },
      );
      fireEvent.change(
        screen.getByPlaceholderText('Overall validation summary...'),
        { target: { value: 'Current authoritative validation summary.' } },
      );
      expect(
        screen.getByRole('button', { name: 'Submit Validation (Approve)' }),
      ).toBeEnabled();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(apiMock.getCard).toHaveBeenCalledTimes(2);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(apiMock.getCard).toHaveBeenCalledTimes(3);

      currentRefresh.resolve(currentCard);
      await flushMicrotasks();
      expect(screen.getByRole('status')).toHaveTextContent(
        'Refreshing authoritative Task Validation thresholds.',
      );
      expect(
        screen.getByRole('button', { name: 'Submit Validation (Approve)' }),
      ).toBeDisabled();
      expect(
        screen.getByRole('img', {
          name: /Confidence score 80 out of 100.*Minimum 71.*threshold met/i,
        }),
      ).toBeInTheDocument();

      currentSpecResponse.resolve(currentSpec);
      await flushMicrotasks();
      expect(
        screen.getByRole('img', {
          name: /Confidence score 80 out of 100.*Minimum 97.*threshold not met/i,
        }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Submit Validation (Approve)' }),
      ).toBeEnabled();

      staleRefresh.resolve(staleCard);
      await flushMicrotasks();
      expect(
        screen.getByRole('img', {
          name: /Confidence score 80 out of 100.*Minimum 97.*threshold not met/i,
        }),
      ).toBeInTheDocument();
      expect(apiMock.getSpec).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it('renders validation history against the thresholds captured at submission time', async () => {
    const validationCard: Card = {
      ...cardForType('normal'),
      id: 'validation-history-1',
      status: 'done',
      validations: [{
        id: 'validation-entry-1',
        confidence: 92,
        estimated_completeness: 88,
        estimated_drift: 9,
        confidence_justification: 'The execution evidence is independently reproducible.',
        completeness_justification: 'All required outputs are linked and verified.',
        drift_justification: 'No unintended product behavior was introduced.',
        general_justification: 'The implementation meets all acceptance criteria.',
        recommendation: 'approve',
        verdict: 'pass',
        evaluator_id: 'agent-1',
        created_at: '2026-07-28T14:00:00Z',
        resolved_thresholds: {
          min_confidence: 85,
          min_completeness: 82,
          max_drift: 15,
          resolved_from: 'spec',
          resolved_sources: {
            required: 'spec',
            min_confidence: 'sprint',
            min_completeness: 'spec',
            max_drift: 'board',
          },
        },
        threshold_violations: [],
      }],
    };
    storeMock.selectedCardId = validationCard.id;
    apiMock.getCard.mockResolvedValue(validationCard);

    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /^Validation/ }));
    fireEvent.click(await screen.findByRole('tab', { name: /^Task validation/ }));
    fireEvent.click(
      await screen.findByText('The implementation meets all acceptance criteria.'),
    );

    expect(
      screen.getByTestId('task-validation-validation-entry-1-confidence-score'),
    ).toHaveAttribute('data-status', 'met');
    expect(
      screen.getByRole('img', {
        name: /Confidence score 92 out of 100.*Minimum 85.*threshold met/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('img', {
        name: /Drift score 9 out of 100.*Maximum 15.*threshold met/i,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText('Threshold source: sprint')).toBeInTheDocument();
    expect(screen.getByText('Threshold source: board')).toBeInTheDocument();
  });

  it('shows the bug origin task and linked regression tests in lineage references', async () => {
    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /References/i }));
    const panel = await screen.findByTestId('bug-traceability-panel');
    await waitFor(() => expect(apiMock.getCard).toHaveBeenCalledWith('bug-1'));

    expect(within(panel).getByText('Origin Task')).toBeInTheDocument();
    expect(within(panel).getByText('Implement story lineage')).toBeInTheDocument();
    expect(within(panel).getByText('In Progress')).toBeInTheDocument();
    expect(within(panel).getByText('Linked Regression Tests')).toBeInTheDocument();
    expect(within(panel).getByText('Regression: story lineage is visible')).toBeInTheDocument();
    expect(within(panel).getByText('Started')).toBeInTheDocument();
  });

  it('toggles the human task requirement link skip from requirement references', async () => {
    const taskCard: Card = {
      ...bugCard,
      id: 'task-skip-1',
      title: 'Task: implement requirement gate',
      card_type: 'normal',
      origin_task_id: null,
      severity: null,
      expected_behavior: null,
      observed_behavior: null,
      linked_test_task_ids: null,
      skip_task_requirement_link_gate: false,
    };
    storeMock.selectedCardId = 'task-skip-1';
    apiMock.getCard.mockResolvedValue(taskCard);
    apiMock.updateCard.mockResolvedValue({
      ...taskCard,
      skip_task_requirement_link_gate: true,
    });

    render(<CardModal boardId="board-1" />);

    fireEvent.click(await screen.findByRole('tab', { name: /References/i }));
    fireEvent.click(await screen.findByRole('tab', { name: /Requirements/i }));
    const toggle = await screen.findByRole('switch', {
      name: 'Skip task requirement link gate for this card',
    });
    fireEvent.click(toggle);

    await waitFor(() => expect(apiMock.updateCard).toHaveBeenCalledWith('task-skip-1', {
      skip_task_requirement_link_gate: true,
    }));
    expect(storeMock.updateCardInColumn).toHaveBeenCalledWith(expect.objectContaining({
      id: 'task-skip-1',
      skip_task_requirement_link_gate: true,
    }));
  });

  it('shows a dedicated evidence tab for test cards', async () => {
    storeMock.selectedCardId = 'test-1';
    const testCard: Card = {
      ...bugCard,
      id: 'test-1',
      title: 'Regression: story lineage is visible',
      card_type: 'test',
      origin_task_id: null,
      severity: undefined,
      expected_behavior: null,
      observed_behavior: null,
      linked_test_task_ids: null,
      test_scenario_ids: ['ts-1', 'ts-2'],
    };
    apiMock.getCard.mockResolvedValue(testCard);
    apiMock.getSpec.mockResolvedValue({
      id: 'spec-1',
      title: 'Stories spec',
      test_scenarios: [
        {
          id: 'ts-1',
          title: 'Scenario with execution evidence',
          linked_criteria: [],
          scenario_type: 'e2e',
          given: 'a linked story',
          when: 'the lineage graph opens',
          then: 'the scenario is visible',
          notes: null,
          status: 'passed',
          linked_task_ids: ['test-1'],
          created_at: '2026-05-06T09:30:00Z',
          evidence: null,
          latest_evidence: {
            test_file_path: 'tests/test_flow.py',
            test_function: 'test_flow_happy_path',
            last_run_at: '2026-05-07T12:00:00Z',
            output_snippet: '1 passed',
          },
        },
        {
          id: 'ts-2',
          title: 'Scenario missing execution evidence',
          linked_criteria: [],
          scenario_type: 'manual',
          given: 'a linked story',
          when: 'the test is reviewed',
          then: 'missing evidence is visible',
          notes: null,
          status: 'failed',
          linked_task_ids: ['test-1'],
          created_at: '2026-05-06T09:40:00Z',
          evidence: null,
        },
      ],
      business_rules: [],
      api_contracts: [],
      technical_requirements: [],
      knowledge_bases: [],
    });

    render(<CardModal boardId="board-1" />);
    fireEvent.click(await screen.findByRole('tab', { name: /Tests/i }));
    fireEvent.click(await screen.findByRole('tab', { name: /Evidence/i }));

    const tab = await screen.findByTestId('test-evidence-tab');
    expect(within(tab).getByText('Scenario with execution evidence')).toBeInTheDocument();
    expect(within(tab).getByText('tests/test_flow.py')).toBeInTheDocument();
    expect(within(tab).getByText('test_flow_happy_path')).toBeInTheDocument();
    expect(within(tab).getByText('1 passed')).toBeInTheDocument();
    expect(within(tab).getByText('Scenario missing execution evidence')).toBeInTheDocument();
    expect(within(tab).getByText('No evidence recorded')).toBeInTheDocument();
  });

  it('downloads card Markdown with sanitized type-aware filename and no mutation calls', async () => {
    render(<CardModal boardId="board-1" />);

    await screen.findByText('Bug: traceability is hidden');
    fireEvent.click(screen.getByTitle('Download Markdown'));

    await waitFor(() =>
      expect(markdownMock.exportCard).toHaveBeenCalledWith(
        expect.objectContaining({ id: 'bug-1', card_type: 'bug' }),
        expect.objectContaining({ id: 'spec-1', title: 'Stories spec' }),
      ),
    );
    expect(markdownMock.markdownFilenameForCard).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'bug-1', card_type: 'bug' }),
    );
    expect(markdownMock.downloadMarkdown).toHaveBeenCalledWith(
      '# card export',
      'bug_bug-traceability-is-hidden.md',
    );
    expect(apiMock.updateCard).not.toHaveBeenCalled();
    expect(apiMock.moveCard).not.toHaveBeenCalled();
    expect(apiMock.deleteCard).not.toHaveBeenCalled();
    expect(apiMock.uploadAttachment).not.toHaveBeenCalled();
    expect(apiMock.unlinkTestTaskFromBug).not.toHaveBeenCalled();
  });

  it('hydrates full architecture designs (card-owned and inherited spec) before export', async () => {
    apiMock.getCard.mockResolvedValue({
      ...bugCard,
      architecture_designs: [{ id: 'arch-card', title: 'Card arch', diagrams_count: 1 }] as any,
    });
    apiMock.getSpec.mockResolvedValue({
      id: 'spec-1',
      title: 'Stories spec',
      test_scenarios: [],
      business_rules: [],
      api_contracts: [],
      technical_requirements: [],
      knowledge_bases: [],
      architecture_designs: [{ id: 'arch-spec', title: 'Spec arch', diagrams_count: 1 }],
    });
    apiMock.getArchitectureDesign.mockImplementation((id: string) =>
      Promise.resolve({ id, title: `${id} full`, entities: [{ id: `${id}-e`, name: 'E' }], interfaces: [], diagrams: [] }),
    );

    render(<CardModal boardId="board-1" />);
    await screen.findByText('Bug: traceability is hidden');
    fireEvent.click(screen.getByTitle('Download Markdown'));

    // Both card-owned and inherited spec architecture summaries are hydrated with payloads.
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledWith('arch-card', true));
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledWith('arch-spec', true));

    // exportCard receives the hydrated full designs (with entities), not the summaries.
    const lastCall = (markdownMock.exportCard.mock.calls.at(-1) ?? []) as any[];
    const cardArg = lastCall[0];
    const specArg = lastCall[1];
    expect(cardArg.architecture_designs[0]).toMatchObject({ id: 'arch-card', entities: [{ id: 'arch-card-e', name: 'E' }] });
    expect(specArg.architecture_designs[0]).toMatchObject({ id: 'arch-spec', entities: [{ id: 'arch-spec-e', name: 'E' }] });
    expect(apiMock.updateCard).not.toHaveBeenCalled();
    expect(apiMock.moveCard).not.toHaveBeenCalled();
  });

  it('renders canonical bug workflow remediation in the tests tab', async () => {
    apiMock.getCard.mockResolvedValue({ ...bugCard, linked_test_task_ids: [] });

    render(<CardModal boardId="board-1" />);
    fireEvent.click(await screen.findByRole('tab', { name: /Tests/i }));

    const panel = await screen.findByTestId('bug-workflow-remediation-panel');
    expect(within(panel).getByText('Path A · Reuse eligible scenario')).toBeInTheDocument();
    expect(within(panel).getByText('create_regression_test_card')).toBeInTheDocument();
    expect(within(panel).getByText('Create regression test card')).toBeInTheDocument();
    expect(within(panel).getByText('Regression: story lineage is visible')).toBeInTheDocument();
    expect(within(panel).queryByText(/Create a new test scenario/i)).not.toBeInTheDocument();
    expect(apiMock.getBugRegressionScenarioCandidates).toHaveBeenCalledWith('bug-1', 'board-1');
  });

  it('uses the shared activity renderer in the activity tab', async () => {
    apiMock.getCardActivity.mockResolvedValue([
      {
        id: 'act-1',
        action: 'structured_entity_updated',
        actor_type: 'agent',
        actor_id: 'agent-1',
        actor_name: 'Validator Agent',
        created_at: '2026-05-29T10:15:00Z',
        summary: 'structured_entity updated type=functional_requirement field=description',
        trigger: 'structured_entity_updated',
        details: {
          after: { text: 'new value' },
          token: '[redacted]',
        },
      },
    ]);

    render(<CardModal boardId="board-1" />);
    fireEvent.click(await screen.findByRole('tab', { name: /Activity/i }));

    expect(await screen.findByTestId('activity-log-list')).toBeInTheDocument();
    expect(
      await screen.findByText('structured_entity updated type=functional_requirement field=description'),
    ).toBeInTheDocument();
    expect(await screen.findByText('Validator Agent')).toBeInTheDocument();
    expect(document.body.textContent ?? '').not.toContain('[object Object]');
    expect(document.body.textContent ?? '').not.toContain('[object: object]');
  });

  it('shows task status activity as a Before to After transition', async () => {
    apiMock.getCardActivity.mockResolvedValue([{
      id: 'act-move',
      action: 'card_moved',
      actor_type: 'user',
      actor_id: 'user-1',
      actor_name: 'Task Owner',
      created_at: '2026-07-22T10:15:00Z',
      summary: 'not_started->started',
      trigger: null,
      details: {
        from_status: 'not_started',
        to_status: 'started',
        from_position: 0,
        to_position: 1,
      },
    }]);

    render(<CardModal boardId="board-1" />);
    fireEvent.click(await screen.findByRole('tab', { name: /Activity/i }));
    fireEvent.click(await screen.findByRole('button', {
      name: /Status changed.*Status: not_started → started/i,
    }));

    const before = await screen.findByRole('region', { name: 'status before value' });
    const after = await screen.findByRole('region', { name: 'status after value' });
    expect(within(before).getByText('Before')).toBeInTheDocument();
    expect(within(before).getByText('not_started')).toBeInTheDocument();
    expect(within(before).queryByText('started')).not.toBeInTheDocument();
    expect(within(after).getByText('After')).toBeInTheDocument();
    expect(within(after).getByText('started')).toBeInTheDocument();
    expect(within(after).queryByText('not_started')).not.toBeInTheDocument();
  });

  it('preserves the no-activity empty state through the shared renderer', async () => {
    render(<CardModal boardId="board-1" />);
    fireEvent.click(await screen.findByRole('tab', { name: /Activity/i }));

    expect(await screen.findByText('No history yet')).toBeInTheDocument();
  });

  it.each(['assign', 'drop', 'refresh'] as const)(
    'keeps the Knowledge tab mounted and ignores Escape, backdrop, and tab changes during %s',
    async (operation) => {
      render(<CardModal boardId="board-1" />);
      fireEvent.click(await screen.findByRole('tab', { name: /Resources/i }));
      fireEvent.click(await screen.findByRole('tab', { name: /^Knowledge/ }));
      expect(await screen.findByTestId('card-knowledge-tab')).toBeVisible();
      expect(cardKnowledgeTabMock.render).toHaveBeenLastCalledWith(
        expect.objectContaining({
          onBusyChange: expect.any(Function),
        }),
      );

      fireEvent.click(
        screen.getByRole('button', { name: `Begin ${operation}` }),
      );

      fireEvent.keyDown(document, { key: 'Escape' });
      expect(storeMock.closeCardModal).not.toHaveBeenCalled();

      const backdrop = document.querySelector('.modal-overlay');
      expect(backdrop).not.toBeNull();
      fireEvent.click(backdrop!);
      expect(storeMock.closeCardModal).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('tab', { name: /^Details$/ }));
      expect(screen.getByTestId('card-knowledge-tab')).toBeVisible();

      fireEvent.click(
        screen.getByRole('button', { name: 'Finish knowledge operation' }),
      );
      fireEvent.click(screen.getByRole('tab', { name: /^Details$/ }));
      expect(screen.getByTestId('card-knowledge-tab')).not.toBeVisible();
    },
  );
});

describe('TestEvidenceTab — re-executable evidence visibility (spec 9e0bf979)', () => {
  function scenario(overrides: Partial<TestScenario>): TestScenario {
    return {
      id: 's1',
      title: 'Scenario',
      linked_criteria: null,
      scenario_type: 'integration',
      given: 'g',
      when: 'w',
      then: 't',
      notes: null,
      status: 'passed',
      linked_task_ids: null,
      evidence: null,
      latest_evidence: null,
      ...overrides,
    } as TestScenario;
  }

  it('renders the new re-executable evidence fields for a replay_command scenario', () => {
    render(
      <TestEvidenceTab
        scenarios={[
          scenario({
            id: 'replay',
            evidence: {
              evidence_class: 'replay_command',
              replay_command: 'pytest tests/test_x.py::test_y',
              expected_output_snapshot: '1 passed',
            },
          }),
        ]}
      />,
    );
    expect(screen.getByText('Evidence class')).toBeInTheDocument();
    expect(screen.getByText('Replay command')).toBeInTheDocument();
    expect(screen.getByText('pytest tests/test_x.py::test_y')).toBeInTheDocument();
    expect(screen.getByText('Expected output')).toBeInTheDocument();
    expect(screen.getByText('1 passed')).toBeInTheDocument();
    // The badge reflects the real class/artifact, not a decorative flag.
    expect(screen.getByTestId('evidence-badge-class')).toHaveAttribute(
      'data-evidence-class',
      'replay_command',
    );
  });

  it('renders the non_replayable_justification block for a run_log scenario', () => {
    render(
      <TestEvidenceTab
        scenarios={[
          scenario({
            id: 'runlog',
            evidence: {
              evidence_class: 'run_log',
              last_run_at: '2026-06-19T00:00:00',
              output_snippet: 'ok',
              non_replayable_justification: 'dogfood MCP flow, no harness yet',
              expected_output_snapshot: 'spec done',
            },
          }),
        ]}
      />,
    );
    expect(screen.getByText('Non-replayable justification')).toBeInTheDocument();
    expect(screen.getByText('dogfood MCP flow, no harness yet')).toBeInTheDocument();
  });

  it('renders legacy evidence (no evidence_class) without breaking', () => {
    render(
      <TestEvidenceTab
        scenarios={[
          scenario({
            id: 'legacy',
            evidence: {
              test_file_path: 'tests/foo.py',
              test_function: 'test_bar',
              last_run_at: '2026-04-27T20:00:00',
              output_snippet: '1 passed',
            },
          }),
        ]}
      />,
    );
    expect(screen.getByText('Test file')).toBeInTheDocument();
    expect(screen.getByText('tests/foo.py')).toBeInTheDocument();
    // legacy → binary present badge, no class badge, no new-field labels.
    expect(screen.getByTestId('evidence-badge-present')).toBeInTheDocument();
    expect(screen.queryByText('Replay command')).not.toBeInTheDocument();
    expect(screen.queryByText('Non-replayable justification')).not.toBeInTheDocument();
  });

  it('renders negative as supported and a historical unknown type explicitly', () => {
    render(
      <TestEvidenceTab
        scenarios={[
          scenario({
            id: 'negative',
            scenario_type: 'negative',
          }),
          scenario({
            id: 'legacy',
            scenario_type: 'regression',
          }),
        ]}
      />,
    );

    const badges = screen.getAllByTestId('scenario-type-badge');
    expect(badges[0]).toHaveTextContent('negative');
    expect(badges[0]).not.toHaveAttribute('data-unsupported');
    expect(badges[0]).toHaveClass(
      'bg-rose-50',
      'text-rose-600',
      'dark:bg-rose-900/30',
      'dark:text-rose-300',
    );
    expect(badges[1]).toHaveTextContent('regression (unsupported)');
    expect(badges[1]).toHaveAttribute('data-unsupported', 'true');
  });
});

// SK-B2-S1 — TS-11: ExecutionReportsPanel renders the declared impact block
// read-only (no edit controls) and skips it entirely when absent.
describe('ExecutionReportsPanel impact evidence (TS-11)', () => {
  const baseCard = {
    id: 'card-ie',
    board_id: 'board-1',
    spec_id: 'spec-1',
    title: 'IE card',
    description: null,
    details: null,
    status: 'validation',
    priority: 'high',
    position: 0,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-08-02T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
    due_date: null,
    labels: [],
    card_type: 'normal',
    archived: false,
  } as unknown as import('@/types').Card;

  it('renders every declared section read-only', async () => {
    const { ExecutionReportsPanel } = await import('../CardModal');
    const card = {
      ...baseCard,
      conclusions: [
        {
          text: 'done',
          author_id: 'agent-1',
          created_at: '2026-08-02T00:00:00Z',
          completeness: 100,
          completeness_justification: 'ok',
          drift: 0,
          drift_justification: 'ok',
          source: 'move_to_validation',
          impact_evidence: {
            schema_version: 1,
            files: [
              {
                repo: 'core',
                path: 'src/okto_pulse/core/models/schemas.py',
                change_kind: 'modified',
              },
            ],
            symbols: [
              {
                name: 'ImpactEvidence',
                kind: 'class',
                action: 'created',
                repo: 'core',
                file: 'src/okto_pulse/core/models/schemas.py',
              },
            ],
            surfaces: [
              { kind: 'mcp_tool', identifier: 'okto_pulse_move_card' },
            ],
            tests: [
              {
                action: 'added',
                repo: 'core',
                test_file_path: 'tests/test_impact_evidence_shape.py',
                scenario_id: 'ts_8138a59f',
              },
            ],
            evidence_refs: ['ts_8138a59f'],
          },
        },
      ],
    } as unknown as import('@/types').Card;
    render(<ExecutionReportsPanel card={card} />);
    const block = screen.getByTestId('impact-evidence-readonly');
    expect(block).toHaveTextContent('[core] modified: src/okto_pulse/core/models/schemas.py');
    expect(block).toHaveTextContent('class/created: ImpactEvidence');
    expect(block).toHaveTextContent('mcp_tool: okto_pulse_move_card');
    expect(block).toHaveTextContent('ts_8138a59f');
    expect(within(block).queryByRole('button')).toBeNull();
    expect(within(block).queryByRole('textbox')).toBeNull();
  });

  it('omits the section entirely when the conclusion has no block', async () => {
    const { ExecutionReportsPanel } = await import('../CardModal');
    const card = {
      ...baseCard,
      conclusions: [
        {
          text: 'legacy',
          author_id: 'agent-1',
          created_at: '2026-08-02T00:00:00Z',
          completeness: 100,
          completeness_justification: 'ok',
          drift: 0,
          drift_justification: 'ok',
        },
      ],
    } as unknown as import('@/types').Card;
    render(<ExecutionReportsPanel card={card} />);
    expect(screen.queryByTestId('impact-evidence-readonly')).toBeNull();
  });
});

// SK-B2-S1 — TS-16: under 'require', the 409 impact_evidence_required
// remediation renders IN-PLACE and the prompt keeps its state; the same
// submit succeeds after the gate clears.
describe('conclusion prompt keeps state on impact_evidence_required (TS-16)', () => {
  it('shows the remediation without closing and retries successfully', async () => {
    const normalCard = {
      ...cardForType('normal'),
      status: 'in_progress',
    } as Card;
    storeMock.selectedCardId = normalCard.id;
    apiMock.getCard.mockResolvedValue(normalCard);
    apiMock.getAllowedTransitions.mockResolvedValue(
      transitionEnvelope(normalCard.id, 'in_progress', [
        allowedTransition('validation'),
        allowedTransition('on_hold'),
      ]),
    );
    apiMock.moveCard
      .mockRejectedValueOnce(
        new Error(
          'impact_evidence_required: This board requires declared impact evidence on the execution report',
        ),
      )
      .mockResolvedValueOnce({ ...normalCard, status: 'validation' });

    render(<CardModal boardId="board-1" />);

    const status = await screen.findByRole('combobox', {
      name: 'Card status',
    });
    await waitFor(() => expect(status).not.toBeDisabled());
    fireEvent.change(status, { target: { value: 'validation' } });

    // The executor-report prompt opens instead of calling the API.
    expect(
      await screen.findByText('Execution Report Required'),
    ).toBeInTheDocument();
    fireEvent.change(
      screen.getByPlaceholderText(/## Implementation Summary/),
      { target: { value: 'Executor claim' } },
    );
    fireEvent.change(
      screen.getByPlaceholderText('Justify the completeness score...'),
      { target: { value: 'complete' } },
    );
    fireEvent.change(
      screen.getByPlaceholderText('Justify the drift score...'),
      { target: { value: 'no drift' } },
    );

    const submit = screen.getByRole('button', {
      name: /Complete & Move to/,
    });
    fireEvent.click(submit);

    // Gate rejection: remediation in-place, prompt still open, state intact.
    expect(
      await screen.findByTestId('impact-evidence-gate-error'),
    ).toHaveTextContent('impact_evidence_required');
    expect(screen.getByText('Execution Report Required')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(/## Implementation Summary/),
    ).toHaveValue('Executor claim');

    // Same submit succeeds once the block/gate situation is resolved.
    fireEvent.click(submit);
    await waitFor(() =>
      expect(
        screen.queryByText('Execution Report Required'),
      ).not.toBeInTheDocument(),
    );
    expect(apiMock.moveCard).toHaveBeenCalledTimes(2);
  });
});
