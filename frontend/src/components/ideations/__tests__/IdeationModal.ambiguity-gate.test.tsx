import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { IdeationModal } from '../IdeationModal';
import type { Ideation } from '@/types';

const apiMock = vi.hoisted(() => ({
  getIdeation: vi.fn(),
  getArchitectureDesign: vi.fn(),
  listIdeationSnapshots: vi.fn(),
  listIdeationKnowledge: vi.fn(),
  listIdeationHistory: vi.fn(),
  listIdeationQA: vi.fn(),
  moveIdeation: vi.fn(),
  deleteIdeation: vi.fn(),
  updateIdeation: vi.fn(),
  setIdeationAmbiguityGateSkip: vi.fn(),
}));

const boardState = vi.hoisted(() => ({
  currentBoard: { id: 'board-1', owner_id: 'owner-1', agents: [], settings: {} } as any,
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('@/store/dashboard', () => ({ useCurrentBoard: () => boardState.currentBoard }));
vi.mock('@/lib/exportMarkdown', () => ({
  exportIdeation: vi.fn(() => '# x'),
  downloadMarkdown: vi.fn(),
  slugify: vi.fn((s: string) => s),
}));
vi.mock('@/components/traceability', () => ({ openLineageGraph: vi.fn() }));
vi.mock('@/components/architecture', () => ({ ArchitectureTab: () => <div /> }));
vi.mock('@/components/resources/ResourceGateSummary', () => ({ ResourceGateSummary: () => <div /> }));
vi.mock('@/components/specs/MockupsTab', () => ({ MockupsTab: () => <div /> }));
vi.mock('@/components/shared/MentionInput', () => ({ MentionInput: () => <div /> }));
vi.mock('@/components/shared/MarkdownContent', () => ({ MarkdownContent: ({ content }: { content: string }) => <div>{content}</div> }));
vi.mock('@/components/shared/ContextSelector', () => ({
  ContextSelector: () => <div />,
  buildIdeationItems: vi.fn(() => []),
  compileSelectedContext: vi.fn(() => ''),
}));
vi.mock('@/components/shared/EditableField', () => ({
  EditableField: ({ value, renderView, placeholder }: any) => <div>{value ? renderView(value) : placeholder}</div>,
}));

const toastMock = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock('react-hot-toast', () => ({ default: toastMock }));

function ideationWith(overrides: Partial<Ideation>): Ideation {
  return {
    id: 'ideation-1',
    board_id: 'board-1',
    title: 'My Ideation',
    description: 'An idea',
    problem_statement: 'A problem',
    proposed_approach: 'An approach',
    scope_assessment: { domains: 1, ambiguity: 4, dependencies: 1 },
    complexity: 'medium',
    screen_mockups: [],
    architecture_designs: [],
    status: 'evaluating',
    version: 2,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-05-06T10:00:00Z',
    updated_at: '2026-05-06T10:00:00Z',
    labels: [],
    skip_ambiguity_gate: false,
    refinements: [],
    stories: [],
    specs: [],
    knowledge_bases: [],
    qa_items: [],
    ...overrides,
  };
}

describe('IdeationModal Max ambiguity gate panel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    boardState.currentBoard = {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
      settings: { require_ideation_ambiguity_gate: true, max_ideation_ambiguity: 3 },
    };
    apiMock.getIdeation.mockResolvedValue(ideationWith({}));
    apiMock.listIdeationSnapshots.mockResolvedValue([]);
    apiMock.listIdeationKnowledge.mockResolvedValue([]);
    apiMock.listIdeationHistory.mockResolvedValue([]);
    apiMock.listIdeationQA.mockResolvedValue([]);
  });

  it('shows gate status, current ambiguity, threshold and skip control when the board gate is enabled', async () => {
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    const panel = screen.getByTestId('ambiguity-gate-panel');
    expect(panel).toHaveTextContent('Board threshold:');
    expect(panel).toHaveTextContent('Current ambiguity:');
    // ambiguity 4 > threshold 3, not skipped -> blocks
    expect(screen.getByTestId('ambiguity-gate-status')).toHaveTextContent('Blocks completion');
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute('role', 'switch');
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute('aria-checked', 'false');
  });

  it('persists skip through the dedicated endpoint and refreshes state', async () => {
    apiMock.setIdeationAmbiguityGateSkip.mockResolvedValue(ideationWith({ skip_ambiguity_gate: true }));
    const onChanged = vi.fn();
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={onChanged} />);

    await screen.findByText('My Ideation');
    fireEvent.click(screen.getByTestId('toggle-skip-ambiguity-gate'));

    await waitFor(() => expect(apiMock.setIdeationAmbiguityGateSkip).toHaveBeenCalledWith('ideation-1', true));
    expect(onChanged).toHaveBeenCalled();
    // refreshed state -> status now Skipped
    await waitFor(() => expect(screen.getByTestId('ambiguity-gate-status')).toHaveTextContent('Skipped'));
    expect(apiMock.updateIdeation).not.toHaveBeenCalled();
  });

  it('surfaces the backend error through the toast path without a generic message', async () => {
    apiMock.setIdeationAmbiguityGateSkip.mockRejectedValue(new Error('Cannot update ambiguity gate skip for archived ideation.'));
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    fireEvent.click(screen.getByTestId('toggle-skip-ambiguity-gate'));

    await waitFor(() =>
      expect(toastMock.error).toHaveBeenCalledWith('Cannot update ambiguity gate skip for archived ideation.'),
    );
  });

  it('hides the gate panel when the board gate is disabled', async () => {
    boardState.currentBoard = {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
      settings: { require_ideation_ambiguity_gate: false },
    };
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    expect(screen.queryByTestId('ambiguity-gate-panel')).not.toBeInTheDocument();
  });
});
