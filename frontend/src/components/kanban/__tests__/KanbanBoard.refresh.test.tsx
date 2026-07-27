import type { ReactNode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CardStatus, CardSummary, ColumnPageResponse } from '@/types';
import { KanbanBoard } from '../KanbanBoard';

const mocks = vi.hoisted(() => {
  const getBoardColumns = vi.fn();
  const getBoardColumnPage = vi.fn();
  const lookupSpecs = vi.fn();
  const beginColumnsGeneration = vi.fn();
  const applyColumnsBatch = vi.fn();
  const beginColumnPage = vi.fn();
  const applyColumnPage = vi.fn();
  const failColumnPage = vi.fn();
  const openCardModal = vi.fn();
  const optimisticMoveCard = vi.fn();
  const emptyColumns = {
    not_started: [],
    started: [],
    in_progress: [],
    validation: [],
    on_hold: [],
    done: [],
    cancelled: [],
  };
  const dashboardState = {
    columns: emptyColumns,
    columnsMeta: {},
    columnsGeneration: 1,
    currentBoard: {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
    },
    openCardModal,
    optimisticMoveCard,
    beginColumnsGeneration,
    applyColumnsBatch,
    beginColumnPage,
    applyColumnPage,
    failColumnPage,
  };
  const dashboardHook = Object.assign(
    (selector?: (state: typeof dashboardState) => unknown) => (
      selector ? selector(dashboardState) : dashboardState
    ),
    { getState: () => dashboardState },
  );
  return {
    getBoardColumns,
    getBoardColumnPage,
    lookupSpecs,
    beginColumnsGeneration,
    applyColumnsBatch,
    beginColumnPage,
    applyColumnPage,
    failColumnPage,
    openCardModal,
    optimisticMoveCard,
    emptyColumns,
    dashboardState,
    dashboardHook,
  };
});

vi.mock('@/services/api', () => ({
  useDashboardApi: () => ({
    getBoardColumns: mocks.getBoardColumns,
    getBoardColumnPage: mocks.getBoardColumnPage,
    lookupSpecs: mocks.lookupSpecs,
  }),
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: mocks.dashboardHook,
  useColumns: () => mocks.dashboardState.columns,
  useColumnsMeta: () => mocks.dashboardState.columnsMeta,
  useCurrentBoard: () => mocks.dashboardState.currentBoard,
}));

vi.mock('@/hooks/useCognitivePendingBadges', () => ({
  useCognitivePendingBadges: () => ({ badges: new Map() }),
}));

vi.mock('../KanbanColumn', () => ({
  KanbanColumn: ({
    status,
    cards,
    onViewAll,
    footer,
  }: {
    status: CardStatus;
    cards: CardSummary[];
    onViewAll?: () => void;
    footer?: ReactNode;
  }) => (
    <div data-testid={footer ? `expanded-${status}` : `column-${status}`}>
      {cards.map((card) => <span key={card.id}>{card.title}</span>)}
      {onViewAll && (
        <button type="button" onClick={onViewAll}>View all {status}</button>
      )}
      {footer}
    </div>
  ),
}));

vi.mock('../CardModal', () => ({
  CardModal: () => null,
}));

vi.mock('../CreateCardModal', () => ({
  CreateCardModal: () => null,
}));

vi.mock('@/components/shared/CancellationReasonDialog', () => ({
  CancellationReasonDialog: () => null,
}));

describe('KanbanBoard filtered refresh', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.history.replaceState(null, '', '/');
    mocks.dashboardState.columnsGeneration = 0;
    mocks.beginColumnsGeneration.mockImplementation(() => {
      mocks.dashboardState.columnsGeneration += 1;
      return mocks.dashboardState.columnsGeneration;
    });
    mocks.applyColumnsBatch.mockReturnValue(true);
    let pageRequest = 0;
    mocks.beginColumnPage.mockImplementation((column: CardStatus, offset: number) => ({
      generation: mocks.dashboardState.columnsGeneration,
      offset,
      request_id: `page-${++pageRequest}`,
      column,
    }));
    mocks.applyColumnPage.mockReturnValue(true);
    mocks.failColumnPage.mockReturnValue(true);
    mocks.getBoardColumns.mockResolvedValue({
      board_id: 'board-1',
      columns: mocks.emptyColumns,
      columns_meta: { columns: {} },
    });
    mocks.lookupSpecs.mockResolvedValue({
      items: [{ id: 'spec-1', title: 'Spec one', status: 'in_progress' }],
      total_filtered: 1,
      total_overall: 1,
      offset: 0,
      limit: 50,
    });
  });

  async function selectSpec() {
    fireEvent.click(screen.getByRole('button', { name: /filter specs/i }));
    fireEvent.click((await screen.findByText('Spec one')).closest('button')!);
    await waitFor(() => expect(mocks.getBoardColumns).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ specIds: ['spec-1'] }),
    ));
  }

  it('reuses the active spec filter when the parent requests a refresh', async () => {
    const { rerender } = render(<KanbanBoard boardId="board-1" refreshKey={0} />);

    await waitFor(() => expect(mocks.getBoardColumns).toHaveBeenCalledTimes(1));
    expect(mocks.getBoardColumns).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ specIds: [] }),
    );

    await selectSpec();

    await waitFor(() => expect(mocks.getBoardColumns).toHaveBeenCalledTimes(2));
    expect(mocks.getBoardColumns).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({
        specIds: ['spec-1'],
        includeUnlinked: false,
        signal: expect.any(AbortSignal),
      }),
    );

    rerender(<KanbanBoard boardId="board-1" refreshKey={1} />);

    await waitFor(() => expect(mocks.getBoardColumns).toHaveBeenCalledTimes(3));
    expect(mocks.getBoardColumns).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({
        specIds: ['spec-1'],
        includeUnlinked: false,
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it('collapses an expanded column before refresh and rejects its late page response', async () => {
    let resolveOldPage!: (response: ColumnPageResponse) => void;
    mocks.getBoardColumnPage.mockReturnValueOnce(new Promise<ColumnPageResponse>((resolve) => {
      resolveOldPage = resolve;
    }));
    const { rerender } = render(<KanbanBoard boardId="board-1" refreshKey={0} />);

    await waitFor(() => expect(mocks.getBoardColumns).toHaveBeenCalledTimes(1));
    await selectSpec();
    fireEvent.click(screen.getByRole('button', { name: 'View all not_started' }));

    expect(await screen.findByTestId('expanded-not_started')).toBeInTheDocument();
    await waitFor(() => expect(mocks.getBoardColumnPage).toHaveBeenCalledWith(
      'board-1',
      'not_started',
      0,
      expect.objectContaining({ specIds: ['spec-1'] }),
    ));

    rerender(<KanbanBoard boardId="board-1" refreshKey={1} />);

    expect(screen.queryByTestId('expanded-not_started')).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.getBoardColumns).toHaveBeenCalledTimes(3));
    expect(mocks.getBoardColumns).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({ specIds: ['spec-1'] }),
    );

    await act(async () => {
      resolveOldPage({
        board_id: 'board-1',
        column: 'not_started',
        items: [card('stale-card', 'Stale unfiltered card')],
        meta: {
          total_filtered: 71,
          total_overall: 71,
          has_more: true,
          facets: { card_type: { normal: 71 } },
        },
        offset: 0,
        limit: 25,
        next_offset: 25,
      });
      await Promise.resolve();
    });

    expect(mocks.applyColumnPage).not.toHaveBeenCalled();
    expect(screen.queryByText('Stale unfiltered card')).not.toBeInTheDocument();
  });

  it('invalidates an expanded unfiltered page as soon as the spec query changes', async () => {
    let resolveOldPage!: (response: ColumnPageResponse) => void;
    mocks.getBoardColumnPage.mockReturnValueOnce(new Promise<ColumnPageResponse>((resolve) => {
      resolveOldPage = resolve;
    }));
    render(<KanbanBoard boardId="board-1" refreshKey={0} />);

    await waitFor(() => expect(mocks.getBoardColumns).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'View all not_started' }));
    expect(await screen.findByTestId('expanded-not_started')).toBeInTheDocument();
    await waitFor(() => expect(mocks.getBoardColumnPage).toHaveBeenCalledWith(
      'board-1',
      'not_started',
      0,
      expect.objectContaining({ specIds: [] }),
    ));

    await selectSpec();

    expect(screen.queryByTestId('expanded-not_started')).not.toBeInTheDocument();
    await act(async () => {
      resolveOldPage({
        board_id: 'board-1',
        column: 'not_started',
        items: [card('old-unfiltered-card', 'Old unfiltered card')],
        meta: {
          total_filtered: 71,
          total_overall: 71,
          has_more: true,
          facets: { card_type: { normal: 71 } },
        },
        offset: 0,
        limit: 25,
        next_offset: 25,
      });
      await Promise.resolve();
    });

    expect(mocks.applyColumnPage).not.toHaveBeenCalled();
    expect(screen.queryByText('Old unfiltered card')).not.toBeInTheDocument();
  });
});

function card(id: string, title: string): CardSummary {
  return {
    id,
    board_id: 'board-1',
    spec_id: null,
    title,
    description: null,
    status: 'not_started',
    priority: 'none',
    position: 0,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-07-22T00:00:00Z',
    updated_at: '2026-07-22T00:00:00Z',
    due_date: null,
    labels: null,
    test_scenario_ids: null,
    conclusions: null,
    validations: null,
    card_type: 'normal',
  };
}
