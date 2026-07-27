import { DndContext } from '@dnd-kit/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CardStatus, CardSummary, ColumnPageResponse } from '@/types';
import { KanbanColumnPage } from '../KanbanColumnPage';

const mocks = vi.hoisted(() => ({
  getBoardColumnPage: vi.fn(),
  beginColumnPage: vi.fn(),
  applyColumnPage: vi.fn(),
  failColumnPage: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => ({
    getBoardColumnPage: mocks.getBoardColumnPage,
  }),
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: (selector: (state: Record<string, unknown>) => unknown) => selector({
    beginColumnPage: mocks.beginColumnPage,
    applyColumnPage: mocks.applyColumnPage,
    failColumnPage: mocks.failColumnPage,
    columnsGeneration: 7,
  }),
}));

vi.mock('../KanbanCard', () => ({
  KanbanCard: ({ card }: { card: CardSummary }) => (
    <article data-testid={`card-${card.id}`}>{card.title}</article>
  ),
}));

function card(id: string, title = id): CardSummary {
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
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    due_date: null,
    labels: null,
    test_scenario_ids: null,
    conclusions: null,
    validations: null,
    card_type: 'normal',
  };
}

function response(
  items: CardSummary[],
  offset: number,
  total = items.length,
): ColumnPageResponse {
  return {
    board_id: 'board-1',
    column: 'not_started',
    items,
    meta: {
      total_filtered: total,
      total_overall: total,
      has_more: offset + items.length < total,
      facets: { card_type: { normal: total } },
    },
    offset,
    limit: 25,
    next_offset: offset + items.length < total ? offset + 25 : null,
  };
}

function renderPage(overrides: {
  onCollapse?: () => void;
  onItemsChange?: (status: CardStatus, items: CardSummary[]) => void;
} = {}) {
  const onCollapse = overrides.onCollapse ?? vi.fn();
  const onItemsChange = overrides.onItemsChange ?? vi.fn();
  render(
    <DndContext>
      <KanbanColumnPage
        boardId="board-1"
        status="not_started"
        query={{ perColumnLimit: 10, search: 'needle', includeArchived: true }}
        initialCards={[card('initial', 'Initial card')]}
        totalCount={27}
        cardTypeFacets={{ normal: 27 }}
        activeCardTypes={new Set(['task', 'test', 'bug'])}
        onToggleCardType={vi.fn()}
        onCardClick={vi.fn()}
        onAddCard={vi.fn()}
        nameMap={{}}
        onItemsChange={onItemsChange}
        onCollapse={onCollapse}
      />
    </DndContext>,
  );
  return { onCollapse, onItemsChange };
}

describe('KanbanColumnPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.history.replaceState(null, '', '/');
    let request = 0;
    mocks.beginColumnPage.mockImplementation((column: CardStatus, offset: number) => ({
      generation: 7,
      offset,
      request_id: `request-${++request}`,
      column,
    }));
    mocks.applyColumnPage.mockReturnValue(true);
    mocks.failColumnPage.mockReturnValue(true);
  });

  it('renders the server page as regular cards in the same column without a dialog', async () => {
    const pageItems = [card('page-1', 'Page card one'), card('page-2', 'Page card two')];
    mocks.getBoardColumnPage.mockResolvedValue(response(pageItems, 0, 27));
    const { onCollapse, onItemsChange } = renderPage();

    expect(await screen.findByText('Page card one')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Not Started' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByTestId('kanban-column-not_started-paginator-status')).toHaveTextContent(
      'Showing 1–2 of 27 matching. 27 overall. Page 1 of 2.',
    );
    expect(onItemsChange).toHaveBeenCalledWith('not_started', pageItems);
    expect(mocks.getBoardColumnPage).toHaveBeenCalledWith(
      'board-1',
      'not_started',
      0,
      expect.objectContaining({
        perColumnLimit: 25,
        search: 'needle',
        includeArchived: true,
        signal: expect.any(AbortSignal),
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Show fewer cards from Not Started' }));
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it('requests the next window and replaces the visible column cards atomically', async () => {
    const firstPage = Array.from({ length: 25 }, (_, index) => card(`page-1-${index}`));
    const secondPage = [card('page-2-only', 'Only card on page two')];
    mocks.getBoardColumnPage
      .mockResolvedValueOnce(response(firstPage, 0, 26))
      .mockResolvedValueOnce(response(secondPage, 25, 26));
    const { onItemsChange } = renderPage();

    await screen.findByTestId('card-page-1-0');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));

    expect(await screen.findByText('Only card on page two')).toBeInTheDocument();
    expect(screen.queryByTestId('card-page-1-0')).not.toBeInTheDocument();
    expect(screen.getByTestId('kanban-column-not_started-paginator-status')).toHaveTextContent(
      'Showing 26–26 of 26 matching. 26 overall. Page 2 of 2.',
    );
    await waitFor(() => expect(mocks.getBoardColumnPage).toHaveBeenNthCalledWith(
      2,
      'board-1',
      'not_started',
      25,
      expect.objectContaining({ perColumnLimit: 25 }),
    ));
    expect(onItemsChange).toHaveBeenLastCalledWith('not_started', secondPage);
  });
});
