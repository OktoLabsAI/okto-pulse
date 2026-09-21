import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { IdeationsPanel } from '@/components/ideations/IdeationsPanel';
import { RefinementsPanel } from '@/components/refinements/RefinementsPanel';
import { SpecsPanel } from '@/components/specs/SpecsPanel';
import { scopedPaginationKey } from '@/hooks/usePersistedPagination';

const apiMock = vi.hoisted(() => ({
  listIdeationsPage: vi.fn(),
  listBoardRefinementsPage: vi.fn(),
  listSpecsPage: vi.fn(),
  lookupIdeations: vi.fn(),
  lookupSpecs: vi.fn(),
  archiveTree: vi.fn(),
  restoreTree: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/hooks/useCognitivePendingBadges', () => ({
  useCognitivePendingBadges: () => ({ badges: {}, loading: false }),
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const ideation = {
  id: 'idea-1',
  board_id: 'board-1',
  title: 'Server ideation',
  description: 'Description',
  problem_statement: 'Problem',
  complexity: 'medium' as const,
  status: 'draft' as const,
  version: 1,
  assignee_id: null,
  created_by: 'user-1',
  created_at: '2026-07-20T00:00:00Z',
  updated_at: '2026-07-20T00:00:00Z',
  labels: [],
  archived: false,
};

const refinement = {
  id: 'ref-1',
  ideation_id: 'idea-1',
  ideation_title: 'Server ideation',
  board_id: 'board-1',
  title: 'Server refinement',
  description: 'Description',
  status: 'draft' as const,
  version: 1,
  assignee_id: null,
  created_by: 'user-1',
  created_at: '2026-07-20T00:00:00Z',
  updated_at: '2026-07-20T00:00:00Z',
  labels: [],
  archived: false,
};

const spec = {
  id: 'spec-1',
  board_id: 'board-1',
  ideation_id: null,
  refinement_id: null,
  title: 'Server spec',
  description: 'Description',
  status: 'draft' as const,
  edition: 1,
  version: 1,
  assignee_id: null,
  created_by: 'user-1',
  created_at: '2026-07-20T00:00:00Z',
  updated_at: '2026-07-20T00:00:00Z',
  labels: [],
  archived: false,
};

function envelope<T>(items: T[], offset = 0) {
  return {
    items,
    total_filtered: 50,
    total_overall: 75,
    offset,
    limit: 25,
  };
}

describe('paginated entity panels', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.history.replaceState({}, '', '/');
    apiMock.listIdeationsPage.mockResolvedValue(envelope([ideation]));
    apiMock.listBoardRefinementsPage.mockResolvedValue(envelope([refinement]));
    apiMock.listSpecsPage.mockResolvedValue(envelope([spec]));
    apiMock.lookupIdeations.mockResolvedValue({
      items: [],
      total: 0,
      offset: 0,
      limit: 50,
    });
    apiMock.lookupSpecs.mockResolvedValue({
      items: [{ id: 'spec-1', title: 'Spec one', status: 'validated' }],
      total: 1,
      offset: 0,
      limit: 50,
    });
  });

  it.each([
    ['ideation', false], ['ideation', true],
    ['refinement', false], ['refinement', true],
    ['spec', false], ['spec', true],
  ] as const)('archives/restores the %s tree with archived=%s and no Sprint count', async (kind, archived) => {
    const config = {
      ideation: { Panel: IdeationsPanel, item: ideation, list: apiMock.listIdeationsPage },
      refinement: { Panel: RefinementsPanel, item: refinement, list: apiMock.listBoardRefinementsPage },
      spec: { Panel: SpecsPanel, item: spec, list: apiMock.listSpecsPage },
    }[kind];
    config.list.mockResolvedValue(envelope([{ ...config.item, archived }]));
    const mutate = archived ? apiMock.restoreTree : apiMock.archiveTree;
    mutate.mockResolvedValue({
      [archived ? 'restored_count' : 'archived_count']: {
        ideations: 0, refinements: 0, specs: 1, cards: 2,
      },
    });
    const { Panel } = config;
    render(<Panel boardId="board-1" />);
    const button = await screen.findByTitle(archived ? 'Restore tree' : 'Archive tree');
    const readsBefore = config.list.mock.calls.length;
    fireEvent.click(button);
    await waitFor(() => expect(mutate).toHaveBeenCalledWith('board-1', kind, config.item.id));
    await waitFor(() => expect(config.list.mock.calls.length).toBeGreaterThan(readsBefore));
    expect(screen.queryByText(/sprint/i)).not.toBeInTheDocument();
  });

  it('sends ideation search and derivation filters to the server one interaction at a time', async () => {
    render(<IdeationsPanel boardId="board-1" />);
    await waitFor(() => expect(apiMock.listIdeationsPage).toHaveBeenCalledTimes(1));

    const search = screen.getByTestId('ideations-search');
    fireEvent.change(search, { target: { value: 'server' } });
    fireEvent.change(search, { target: { value: 'server ideation' } });
    await waitFor(() => expect(apiMock.listIdeationsPage).toHaveBeenCalledTimes(2));
    expect(apiMock.listIdeationsPage).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ search: 'server ideation', offset: 0, limit: 25 }),
    );

    fireEvent.click(screen.getByTestId('ideations-no-derivation-filter'));
    await waitFor(() => expect(apiMock.listIdeationsPage).toHaveBeenCalledTimes(3));
    expect(apiMock.listIdeationsPage).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ derivationPending: true, search: 'server ideation' }),
    );
  });

  it('does not carry an ideation page into another board', async () => {
    const boardOneKey = scopedPaginationKey('ideations', 'board-1');
    window.localStorage.setItem(
      `okto.pagination.${boardOneKey}`,
      JSON.stringify({ page: 2, pageSize: 25 }),
    );
    const { rerender } = render(<IdeationsPanel boardId="board-1" />);
    await waitFor(() => expect(apiMock.listIdeationsPage).toHaveBeenCalledWith(
      'board-1',
      expect.objectContaining({ offset: 25, limit: 25 }),
    ));
    apiMock.listIdeationsPage.mockClear();

    rerender(<IdeationsPanel boardId="board-2" />);

    await waitFor(() => expect(apiMock.listIdeationsPage).toHaveBeenCalledWith(
      'board-2',
      expect.objectContaining({ offset: 0, limit: 25 }),
    ));
    expect(apiMock.listIdeationsPage).not.toHaveBeenCalledWith(
      'board-2',
      expect.objectContaining({ offset: 25 }),
    );
  });

  it('never falls back to legacy scope ambiguity when Quality is omitted', async () => {
    apiMock.listIdeationsPage.mockResolvedValue(envelope([{
      ...ideation,
      scope_assessment: {
        domains: 2,
        ambiguity: 5,
        dependencies: 3,
      },
      // Deliberately omitted: this is also the permission-denied projection.
      quality_summaries: undefined,
    }]));

    render(<IdeationsPanel boardId="board-1" />);
    await screen.findByText('Server ideation');

    expect(screen.getByTitle('Domains score: 2/5')).toBeInTheDocument();
    expect(screen.getByTitle('Dependencies score: 3/5')).toBeInTheDocument();
    expect(screen.queryByTitle('Ambiguity score: 5/5')).not.toBeInTheDocument();
    expect(screen.queryByTestId('quality-summary-ambiguity')).not.toBeInTheDocument();
  });

  it('uses the board-wide refinement endpoint and debounces server search', async () => {
    render(<RefinementsPanel boardId="board-1" />);
    await waitFor(() => expect(apiMock.listBoardRefinementsPage).toHaveBeenCalledTimes(1));

    const search = screen.getByTestId('refinements-search');
    fireEvent.change(search, { target: { value: 'ref' } });
    fireEvent.change(search, { target: { value: 'server refinement' } });

    await waitFor(() => expect(apiMock.listBoardRefinementsPage).toHaveBeenCalledTimes(2));
    expect(apiMock.listBoardRefinementsPage).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ search: 'server refinement', offset: 0, limit: 25 }),
    );
  });

  it('renders open Q&A badges and omits zero counts in all non-Kanban board lists', async () => {
    apiMock.listIdeationsPage.mockResolvedValue(envelope([
      { ...ideation, id: 'idea-open', title: 'Ideation with Q&A', open_qa_count: 2 },
      { ...ideation, id: 'idea-clear', title: 'Ideation without Q&A', open_qa_count: 0 },
    ]));
    let view = render(<IdeationsPanel boardId="board-1" />);
    await screen.findByText('Ideation without Q&A');
    expect(screen.getAllByTestId('qa-open-badge')).toHaveLength(1);
    expect(screen.getByTestId('qa-open-badge')).toHaveTextContent('2 open Q&A');
    view.unmount();

    apiMock.listBoardRefinementsPage.mockResolvedValue(envelope([
      { ...refinement, id: 'ref-open', title: 'Refinement with Q&A', open_qa_count: 3 },
      { ...refinement, id: 'ref-clear', title: 'Refinement without Q&A', open_qa_count: 0 },
    ]));
    view = render(<RefinementsPanel boardId="board-1" />);
    await screen.findByText('Refinement without Q&A');
    expect(screen.getAllByTestId('qa-open-badge')).toHaveLength(1);
    expect(screen.getByTestId('qa-open-badge')).toHaveTextContent('3 open Q&A');
    view.unmount();

    apiMock.listSpecsPage.mockResolvedValue(envelope([
      { ...spec, id: 'spec-open', title: 'Spec with Q&A', open_qa_count: 4 },
      { ...spec, id: 'spec-clear', title: 'Spec without Q&A', open_qa_count: 0 },
    ]));
    view = render(<SpecsPanel boardId="board-1" />);
    await screen.findByText('Spec without Q&A');
    expect(screen.getAllByTestId('qa-open-badge')).toHaveLength(1);
    expect(screen.getByTestId('qa-open-badge')).toHaveTextContent('4 open Q&A');
    view.unmount();

  });
});
