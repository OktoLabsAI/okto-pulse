import { beforeEach, describe, expect, it } from 'vitest';
import { CARD_STATUSES, type Board, type CardStatus, type CardSummary } from '@/types';
import { useDashboardStore } from '../dashboard';

function card(id: string, status: CardStatus, position = 0): CardSummary {
  return {
    id,
    board_id: 'board-a',
    spec_id: null,
    title: id,
    description: null,
    status,
    priority: 'medium',
    position,
    assignee_id: null,
    created_by: 'owner',
    created_at: '2026-07-20T00:00:00Z',
    updated_at: '2026-07-20T00:00:00Z',
    due_date: null,
    labels: [],
    test_scenario_ids: null,
    conclusions: null,
    card_type: 'normal',
  };
}

function emptyColumns(): Record<CardStatus, CardSummary[]> {
  return CARD_STATUSES.reduce<Record<CardStatus, CardSummary[]>>((result, status) => {
    result[status] = [];
    return result;
  }, {} as Record<CardStatus, CardSummary[]>);
}

function loadState() {
  return CARD_STATUSES.reduce<Record<CardStatus, { requestId: string | null; error: string | null }>>(
    (result, status) => {
      result[status] = { requestId: null, error: null };
      return result;
    },
    {} as Record<CardStatus, { requestId: string | null; error: string | null }>,
  );
}

const pageMeta = {
  total_filtered: 2,
  total_overall: 3,
  has_more: false,
  facets: { card_type: { normal: 2 } },
} as const;

describe('dashboard paginated column store', () => {
  beforeEach(() => {
    useDashboardStore.setState({
      currentBoard: { id: 'board-a' } as Board,
      columns: emptyColumns(),
      columnsMeta: {},
      columnsGeneration: 1,
      columnPageState: loadState(),
    });
  });

  it('issues one immutable in-flight token per column', () => {
    const token = useDashboardStore.getState().beginColumnPage('done', 0);

    expect(token).not.toBeNull();
    expect(Object.isFrozen(token)).toBe(true);
    expect(token).toMatchObject({ generation: 1, offset: 0, column: 'done' });
    expect(token?.request_id).toMatch(/^column-1-/);
    expect(useDashboardStore.getState().beginColumnPage('done', 0)).toBeNull();
    expect(useDashboardStore.getState().beginColumnPage('started', -1)).toBeNull();
  });

  it('replaces a batch only for the current generation and board', () => {
    const generation = useDashboardStore.getState().beginColumnsGeneration();
    const response = {
      board_id: 'board-a',
      columns: { ...emptyColumns(), done: [card('batch', 'done')] },
      columns_meta: {
        columns: CARD_STATUSES.reduce((result, status) => {
          result[status] = pageMeta;
          return result;
        }, {} as Record<CardStatus, typeof pageMeta>),
        facets: { assignee: [] },
      },
    };

    expect(useDashboardStore.getState().applyColumnsBatch(generation - 1, response)).toBe(false);
    expect(useDashboardStore.getState().applyColumnsBatch(generation, { ...response, board_id: 'board-b' })).toBe(false);
    expect(useDashboardStore.getState().applyColumnsBatch(generation, response)).toBe(true);
    expect(useDashboardStore.getState().columns.done.map(({ id }) => id)).toEqual(['batch']);
  });

  it('rejects a stale generation without mutating loaded data', () => {
    const original = card('original', 'done');
    useDashboardStore.setState({
      columns: { ...emptyColumns(), done: [original] },
    });
    const token = useDashboardStore.getState().beginColumnPage('done', 1)!;
    useDashboardStore.getState().beginColumnsGeneration();

    const applied = useDashboardStore.getState().applyColumnPage(token, {
      board_id: 'board-a',
      column: 'done',
      items: [card('late', 'done', 1)],
      meta: pageMeta,
      offset: 1,
      limit: 25,
      next_offset: null,
    });

    expect(applied).toBe(false);
    expect(useDashboardStore.getState().columns.done).toEqual([original]);
  });

  it('appends and de-duplicates only the token target column', () => {
    const existing = card('existing', 'done');
    const untouched = card('untouched', 'started');
    useDashboardStore.setState({
      columns: { ...emptyColumns(), done: [existing], started: [untouched] },
    });
    const token = useDashboardStore.getState().beginColumnPage('done', 1)!;

    const applied = useDashboardStore.getState().applyColumnPage(token, {
      board_id: 'board-a',
      column: 'done',
      items: [existing, card('next', 'done', 1)],
      meta: pageMeta,
      offset: 1,
      limit: 25,
      next_offset: null,
    });

    const state = useDashboardStore.getState();
    expect(applied).toBe(true);
    expect(state.columns.done.map(({ id }) => id)).toEqual(['existing', 'next']);
    expect(state.columns.started).toEqual([untouched]);
    expect(state.columnsMeta.done).toEqual(pageMeta);
    expect(state.columnPageState.done.requestId).toBeNull();
  });

  it('rejects mismatched board, column, and offset closures', () => {
    const token = useDashboardStore.getState().beginColumnPage('done', 0)!;
    const base = {
      board_id: 'board-a',
      column: 'done' as const,
      items: [card('unexpected', 'done')],
      meta: pageMeta,
      offset: 0,
      limit: 25,
      next_offset: null,
    };

    expect(useDashboardStore.getState().applyColumnPage(token, { ...base, board_id: 'board-b' })).toBe(false);
    expect(useDashboardStore.getState().applyColumnPage(token, { ...base, column: 'started' })).toBe(false);
    expect(useDashboardStore.getState().applyColumnPage(token, { ...base, offset: 25 })).toBe(false);
    expect(useDashboardStore.getState().columns.done).toEqual([]);
  });

  it('records a current failure without changing cards and ignores stale failures', () => {
    const existing = card('existing', 'done');
    useDashboardStore.setState({ columns: { ...emptyColumns(), done: [existing] } });
    const token = useDashboardStore.getState().beginColumnPage('done', 1)!;

    expect(useDashboardStore.getState().failColumnPage(token, 'network down')).toBe(true);
    expect(useDashboardStore.getState().columns.done).toEqual([existing]);
    expect(useDashboardStore.getState().columnPageState.done).toEqual({
      requestId: null,
      error: 'network down',
    });
    expect(useDashboardStore.getState().failColumnPage(token, 'late')).toBe(false);
  });

  it('never applies negative optimistic positions or duplicates same-column cards', () => {
    useDashboardStore.setState({
      columns: { ...emptyColumns(), in_progress: [card('a', 'in_progress'), card('b', 'in_progress', 1)] },
    });

    expect(useDashboardStore.getState().optimisticMoveCard('a', 'done', -1)).toBeNull();
    expect(useDashboardStore.getState().columns.in_progress.map(({ id }) => id)).toEqual(['a', 'b']);

    useDashboardStore.getState().optimisticMoveCard('a', 'in_progress', 1);
    expect(useDashboardStore.getState().columns.in_progress.map(({ id }) => id)).toEqual(['b', 'a']);
  });
});
