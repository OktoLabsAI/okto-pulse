import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SpecsPanel } from '../SpecsPanel';
import type { IdeationSummary, SpecSummary } from '@/types';

const apiMock = vi.hoisted(() => ({
  listSpecs: vi.fn(),
  listIdeations: vi.fn(),
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
    apiMock.listSpecs.mockResolvedValue(specs);
    apiMock.listIdeations.mockResolvedValue(ideations);
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
});
