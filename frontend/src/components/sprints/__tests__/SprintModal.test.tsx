import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SprintModal } from '../SprintModal';
import { deriveSprintDisplayCounts } from '../sprintDisplayCounts';
import type { CardSummaryForSpec, Sprint } from '@/types';

const apiMock = vi.hoisted(() => ({
  getSprint: vi.fn(),
  getSpec: vi.fn(),
  updateSprint: vi.fn(),
  moveSprint: vi.fn(),
  listSprintHistory: vi.fn(),
  assignTasksToSprint: vi.fn(),
  unassignTasksFromSprint: vi.fn(),
}));

const markdownMock = vi.hoisted(() => ({
  exportSprint: vi.fn(() => '# sprint export'),
  downloadMarkdown: vi.fn(),
  slugify: vi.fn((value: string) => value.toLowerCase().replace(/\s+/g, '-')),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

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
  it('counts tests separately while bugs and legacy cards remain Cards', () => {
    const cards = [
      card({ id: 'normal-1', title: 'Normal', status: 'done', card_type: 'normal' }),
      card({ id: 'bug-1', title: 'Bug', status: 'validation', card_type: 'bug' }),
      card({ id: 'test-1', title: 'Test', status: 'done', card_type: 'test' }),
      card({ id: 'legacy-1', title: 'Legacy', status: 'done', card_type: undefined }),
    ];

    const counts = deriveSprintDisplayCounts(cards);

    expect(counts.cards).toBe(3);
    expect(counts.tests).toBe(1);
    expect(counts.workItemsTotal).toBe(4);
    expect(counts.workItemsDone).toBe(3);
    expect(counts.visibleCards.map((item) => item.id)).toEqual(['normal-1', 'bug-1', 'legacy-1']);
    expect(counts.testCards.map((item) => item.id)).toEqual(['test-1']);
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

    expect(screen.getByTestId('sprint-summary-cards')).toHaveTextContent('0');
    expect(screen.getByTestId('sprint-summary-tests')).toHaveTextContent('0');
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

    expect(screen.getByTestId('sprint-summary-cards')).toHaveTextContent('0');
    expect(screen.getByTestId('sprint-summary-tests')).toHaveTextContent('2');
    expect(screen.getByText('1 of 2 work items done')).toBeInTheDocument();
    expect(screen.queryByText(/cards done/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^Cards/i }));

    expect(screen.queryAllByTestId('sprint-card-row')).toHaveLength(0);
    expect(screen.queryByText('Regression one')).not.toBeInTheDocument();
    expect(screen.queryByText('Regression two')).not.toBeInTheDocument();
  });

  it('counts and renders bug cards as Cards', async () => {
    await renderSprint({
      cards: [
        card({ id: 'bug-1', title: 'Fix broken counter', status: 'done', card_type: 'bug' }),
        card({ id: 'bug-2', title: 'Fix stale label', status: 'not_started', card_type: 'bug' }),
      ],
    });

    expect(screen.getByTestId('sprint-summary-cards')).toHaveTextContent('2');
    expect(screen.getByTestId('sprint-summary-tests')).toHaveTextContent('0');

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

    expect(screen.getByTestId('sprint-summary-cards')).toHaveTextContent('3');
    expect(screen.getByTestId('sprint-summary-tests')).toHaveTextContent('1');
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
      expect(apiMock.updateSprint).toHaveBeenCalledWith('sprint-1', { objective: 'New objective' }),
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
      expect(apiMock.updateSprint).toHaveBeenCalledWith('sprint-1', { expected_outcome: 'New outcome' }),
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
});
