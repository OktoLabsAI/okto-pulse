import { render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CardModal } from '../CardModal';
import type { Card, CardSummary, CardStatus } from '@/types';

const apiMock = vi.hoisted(() => ({
  getCard: vi.fn(),
  getSpec: vi.fn(),
  getSpecKnowledge: vi.fn(),
  listAgentsForBoard: vi.fn(),
  getCardSeenStatus: vi.fn(),
  getCardDependencies: vi.fn(),
  getCardDependents: vi.fn(),
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

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
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
});
