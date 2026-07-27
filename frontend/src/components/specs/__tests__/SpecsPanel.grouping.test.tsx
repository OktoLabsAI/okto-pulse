import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SpecsPanel } from '../SpecsPanel';
import type { IdeationSummary, SpecSummary } from '@/types';

const apiMock = vi.hoisted(() => ({
  listSpecs: vi.fn(),
  listSpecsPage: vi.fn(),
  listIdeations: vi.fn(),
  lookupIdeations: vi.fn(),
  listBoardRefinementsPage: vi.fn(),
  getIdeation: vi.fn(),
  archiveTree: vi.fn(),
  restoreTree: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const specBase: Omit<SpecSummary, 'id' | 'title' | 'ideation_id' | 'refinement_id'> = {
  board_id: 'board-1',
  description: null,
  status: 'approved',
  version: 1,
  assignee_id: null,
  created_by: 'user-1',
  created_at: '2026-05-04T00:00:00Z',
  updated_at: '2026-05-04T00:00:00Z',
  labels: null,
  architecture_designs: [],
  archived: false,
};

const specs: SpecSummary[] = [
  {
    ...specBase,
    id: 'spec-with-refinement',
    title: 'Spec with refinement',
    ideation_id: 'idea-1',
    refinement_id: 'ref-1',
  },
  {
    ...specBase,
    id: 'spec-with-ideation',
    title: 'Spec with ideation only',
    ideation_id: 'idea-2',
    refinement_id: null,
  },
  {
    ...specBase,
    id: 'spec-without-parent',
    title: 'Spec without parent',
    ideation_id: null,
    refinement_id: null,
  },
];

const ideations: IdeationSummary[] = [
  {
    id: 'idea-1',
    board_id: 'board-1',
    title: 'Ideation Alpha',
    description: null,
    problem_statement: null,
    complexity: 'medium',
    status: 'done',
    version: 1,
    assignee_id: null,
    created_by: 'user-1',
    created_at: '2026-05-04T00:00:00Z',
    updated_at: '2026-05-04T00:00:00Z',
    labels: null,
    architecture_designs: [],
    archived: false,
  },
  {
    id: 'idea-2',
    board_id: 'board-1',
    title: 'Ideation Beta',
    description: null,
    problem_statement: null,
    complexity: 'small',
    status: 'done',
    version: 1,
    assignee_id: null,
    created_by: 'user-1',
    created_at: '2026-05-04T00:00:00Z',
    updated_at: '2026-05-04T00:00:00Z',
    labels: null,
    architecture_designs: [],
    archived: false,
  },
];

const groupModeKey = (boardId: string) => `okto-pulse:specs:group-mode:${boardId}`;

describe('SpecsPanel grouping modes', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    window.history.replaceState({}, '', '/');
    apiMock.listSpecs.mockResolvedValue(specs);
    apiMock.listSpecsPage.mockResolvedValue({
      items: specs,
      total_filtered: specs.length,
      total_overall: specs.length,
      offset: 0,
      limit: 25,
    });
    apiMock.listIdeations.mockResolvedValue(ideations);
    apiMock.lookupIdeations.mockResolvedValue({
      items: ideations.map(({ id, title, status }) => ({ id, title, status })),
      total: ideations.length,
      offset: 0,
      limit: 50,
    });
    apiMock.listBoardRefinementsPage.mockResolvedValue({
      items: [{
        id: 'ref-1',
        ideation_id: 'idea-1',
        ideation_title: 'Ideation Alpha',
        board_id: 'board-1',
        title: 'Refinement Alpha',
        description: null,
        status: 'approved',
        version: 1,
        assignee_id: null,
        created_by: 'user-1',
        created_at: '2026-05-04T00:00:00Z',
        updated_at: '2026-05-04T00:00:00Z',
        labels: null,
        archived: false,
      }],
      total_filtered: 1,
      total_overall: 1,
      offset: 0,
      limit: 100,
    });
    apiMock.getIdeation.mockImplementation((id: string) => Promise.resolve({
      id,
      title: id === 'idea-1' ? 'Ideation Alpha' : 'Ideation Beta',
      version: 1,
      refinements: id === 'idea-1'
        ? [{ id: 'ref-1', title: 'Refinement Alpha' }]
        : [],
    }));
  });

  it('defaults to parents: refinement first, ideation fallback, no standalone bucket', async () => {
    render(<SpecsPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Spec with refinement')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText('Refinement: Refinement Alpha')).toBeInTheDocument());

    expect(screen.getByTestId('specs-list-group-refinement:ref-1')).toBeInTheDocument();
    expect(screen.getByTestId('specs-list-group-ideation:idea-2')).toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-ideation:idea-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-__ungrouped__')).not.toBeInTheDocument();
    expect(screen.getByText('Spec without parent')).toBeInTheDocument();
  });

  it('groups by ideation only and leaves specs without ideation flat', async () => {
    render(<SpecsPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Spec with refinement')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('specs-group-mode'), { target: { value: 'ideation' } });

    expect(screen.getByTestId('specs-list-group-ideation:idea-1')).toBeInTheDocument();
    expect(screen.getByTestId('specs-list-group-ideation:idea-2')).toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-refinement:ref-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-__ungrouped__')).not.toBeInTheDocument();
    expect(screen.getByText('Spec without parent')).toBeInTheDocument();
  });

  it('groups by refinement only and leaves specs without refinement flat', async () => {
    render(<SpecsPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Spec with refinement')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('specs-group-mode'), { target: { value: 'refinement' } });

    expect(screen.getByTestId('specs-list-group-refinement:ref-1')).toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-ideation:idea-2')).not.toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-__ungrouped__')).not.toBeInTheDocument();
    expect(screen.getByText('Spec with ideation only')).toBeInTheDocument();
    expect(screen.getByText('Spec without parent')).toBeInTheDocument();
  });

  it('renders a flat list when grouping is none', async () => {
    render(<SpecsPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Spec with refinement')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('specs-group-mode'), { target: { value: 'none' } });

    expect(screen.queryByTestId('specs-list-group-refinement:ref-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-ideation:idea-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-ideation:idea-2')).not.toBeInTheDocument();
    expect(screen.queryByTestId('specs-list-group-__ungrouped__')).not.toBeInTheDocument();
    expect(screen.getByText('Spec with refinement')).toBeInTheDocument();
    expect(screen.getByText('Spec with ideation only')).toBeInTheDocument();
    expect(screen.getByText('Spec without parent')).toBeInTheDocument();
  });

  it('persists the grouping mode for the board and restores it after remount', async () => {
    const first = render(<SpecsPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Spec with refinement')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('specs-group-mode'), { target: { value: 'ideation' } });

    expect(localStorage.getItem(groupModeKey('board-1'))).toBe('ideation');

    first.unmount();
    render(<SpecsPanel boardId="board-1" />);

    expect(screen.getByTestId('specs-group-mode')).toHaveValue('ideation');
    await waitFor(() => expect(screen.getByTestId('specs-list-group-ideation:idea-1')).toBeInTheDocument());
  });

  it('scopes the saved grouping mode by board', async () => {
    const first = render(<SpecsPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Spec with refinement')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('specs-group-mode'), { target: { value: 'refinement' } });
    expect(localStorage.getItem(groupModeKey('board-1'))).toBe('refinement');

    first.unmount();
    render(<SpecsPanel boardId="board-2" />);

    expect(screen.getByTestId('specs-group-mode')).toHaveValue('parents');
    expect(localStorage.getItem(groupModeKey('board-2'))).toBeNull();
  });

  it('falls back to parents and clears an invalid saved grouping mode', async () => {
    localStorage.setItem(groupModeKey('board-1'), 'invalid-mode');

    render(<SpecsPanel boardId="board-1" />);

    expect(screen.getByTestId('specs-group-mode')).toHaveValue('parents');
    expect(localStorage.getItem(groupModeKey('board-1'))).toBeNull();
  });

  it('requests one new server page per paginator interaction', async () => {
    apiMock.listSpecsPage.mockImplementation(async (_boardId: string, options: { offset: number; limit: number }) => ({
      items: options.offset === 0 ? specs : [],
      total_filtered: 51,
      total_overall: 51,
      offset: options.offset,
      limit: options.limit,
    }));

    render(<SpecsPanel boardId="board-1" />);
    await waitFor(() => expect(apiMock.listSpecsPage).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));

    await waitFor(() => expect(apiMock.listSpecsPage).toHaveBeenCalledTimes(2));
    expect(apiMock.listSpecsPage).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ offset: 25, limit: 25 }),
    );

    fireEvent.change(screen.getByTestId('specs-search'), { target: { value: 'server spec' } });

    await waitFor(() => expect(apiMock.listSpecsPage).toHaveBeenCalledTimes(3));
    expect(apiMock.listSpecsPage).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ search: 'server spec', offset: 0, limit: 25 }),
    );
  });
});
