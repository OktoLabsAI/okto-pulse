import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CardModal, TestEvidenceTab } from '../CardModal';
import type { Card, CardSummary, CardStatus, TestScenario } from '@/types';

const apiMock = vi.hoisted(() => ({
  getCard: vi.fn(),
  getSpec: vi.fn(),
  getSpecKnowledge: vi.fn(),
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
  unlinkTestTaskFromBug: vi.fn(),
}));

const storeMock = vi.hoisted(() => ({
  selectedCardId: 'bug-1',
  isCardModalOpen: true,
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

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
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

vi.mock('@/components/specs/SpecModal', () => ({
  SpecModal: () => <div />,
}));

vi.mock('../CardKnowledgeTab', () => ({
  CardKnowledgeTab: () => <div />,
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

describe('CardModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    storeMock.selectedCardId = 'bug-1';
    storeMock.isCardModalOpen = true;
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
    apiMock.getSpec.mockResolvedValue({
      id: 'spec-1',
      title: 'Stories spec',
      test_scenarios: [],
      business_rules: [],
      api_contracts: [],
      technical_requirements: [],
      knowledge_bases: [],
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

  it('shows the bug origin task and linked regression tests in details', async () => {
    render(<CardModal boardId="board-1" />);

    const panel = await screen.findByTestId('bug-traceability-panel');
    await waitFor(() => expect(apiMock.getCard).toHaveBeenCalledWith('bug-1'));

    expect(within(panel).getByText('Origin Task')).toBeInTheDocument();
    expect(within(panel).getByText('Implement story lineage')).toBeInTheDocument();
    expect(within(panel).getByText('In Progress')).toBeInTheDocument();
    expect(within(panel).getByText('Linked Regression Tests')).toBeInTheDocument();
    expect(within(panel).getByText('Regression: story lineage is visible')).toBeInTheDocument();
    expect(within(panel).getByText('Started')).toBeInTheDocument();
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
    fireEvent.click(await screen.findByRole('button', { name: /Evidence/i }));

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
    fireEvent.click(await screen.findByRole('button', { name: /Tests/i }));

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
    fireEvent.click(await screen.findByRole('button', { name: /Activity/i }));

    expect(await screen.findByTestId('activity-log-list')).toBeInTheDocument();
    expect(
      screen.getByText('structured_entity updated type=functional_requirement field=description'),
    ).toBeInTheDocument();
    expect(screen.getByText('Validator Agent')).toBeInTheDocument();
    expect(document.body.textContent ?? '').not.toContain('[object Object]');
    expect(document.body.textContent ?? '').not.toContain('[object: object]');
  });

  it('preserves the no-activity empty state through the shared renderer', async () => {
    render(<CardModal boardId="board-1" />);
    fireEvent.click(await screen.findByRole('button', { name: /Activity/i }));

    expect(await screen.findByText('No activity recorded')).toBeInTheDocument();
  });
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
});
