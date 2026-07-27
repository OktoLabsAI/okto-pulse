import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { IdeationsPanel } from '@/components/ideations/IdeationsPanel';
import { RefinementsPanel } from '@/components/refinements/RefinementsPanel';
import { SprintsPanel } from '@/components/sprints/SprintsPanel';

const apiMock = vi.hoisted(() => ({
  listIdeationsPage: vi.fn(),
  listBoardRefinementsPage: vi.fn(),
  listBoardSprintsPage: vi.fn(),
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

const sprint = {
  id: 'sprint-1',
  spec_id: 'spec-1',
  board_id: 'board-1',
  title: 'Server sprint',
  description: 'Description',
  status: 'draft',
  created_by: 'user-1',
  created_at: '2026-07-20T00:00:00Z',
  updated_at: '2026-07-20T00:00:00Z',
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
    apiMock.listBoardSprintsPage.mockResolvedValue(envelope([sprint]));
    apiMock.lookupSpecs.mockResolvedValue({
      items: [{ id: 'spec-1', title: 'Spec one', status: 'validated' }],
      total: 1,
      offset: 0,
      limit: 50,
    });
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

  it('requests exactly one new sprint page after advancing the paginator', async () => {
    apiMock.listBoardSprintsPage.mockImplementation(
      async (_boardId: string, options: { offset: number }) => envelope(
        options.offset === 0 ? [sprint] : [],
        options.offset,
      ),
    );

    render(<SprintsPanel boardId="board-1" />);
    await waitFor(() => expect(apiMock.listBoardSprintsPage).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));

    await waitFor(() => expect(apiMock.listBoardSprintsPage).toHaveBeenCalledTimes(2));
    expect(apiMock.listBoardSprintsPage).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ offset: 25, limit: 25 }),
    );

    fireEvent.change(screen.getByTestId('sprints-search'), { target: { value: 'server sprint' } });

    await waitFor(() => expect(apiMock.listBoardSprintsPage).toHaveBeenCalledTimes(3));
    expect(apiMock.listBoardSprintsPage).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ search: 'server sprint', offset: 0, limit: 25 }),
    );
  });
});
