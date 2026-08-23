import { describe, expect, it } from 'vitest';
import { CARD_STATUSES, type CardStatus, type CardSummary } from '@/types';
import { resolveKanbanDropDestination } from '../kanbanDnd';

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

function columns(
  values: Partial<Record<CardStatus, CardSummary[]>> = {},
): Record<CardStatus, CardSummary[]> {
  return CARD_STATUSES.reduce<Record<CardStatus, CardSummary[]>>((result, status) => {
    result[status] = values[status] ?? [];
    return result;
  }, {} as Record<CardStatus, CardSummary[]>);
}

describe('paginated Kanban DnD invariant', () => {
  it('uses placement=end with index zero for an empty destination column', () => {
    const state = columns({ started: [card('active', 'started')] });

    const destination = resolveKanbanDropDestination(state, 'active', 'done');

    expect(destination).toEqual({
      targetStatus: 'done',
      targetIndex: 0,
      request: { status: 'done', placement: 'end' },
    });
    expect(destination?.request).not.toHaveProperty('position');
  });

  it('uses placement=end for a partial visible page even when the server has more', () => {
    const state = columns({
      started: [card('active', 'started')],
      done: [card('loaded-1', 'done'), card('loaded-2', 'done', 1)],
    });
    const serverMeta = { has_more: true };

    const destination = resolveKanbanDropDestination(state, 'active', 'done');

    expect(serverMeta.has_more).toBe(true);
    expect(destination).toEqual({
      targetStatus: 'done',
      targetIndex: 2,
      request: { status: 'done', placement: 'end' },
    });
    expect(destination?.request).not.toHaveProperty('position');
  });

  it('uses before_id for a card anchor and computes a non-negative local index', () => {
    const state = columns({
      started: [card('active', 'started')],
      validation: [card('anchor', 'validation'), card('later', 'validation', 1)],
    });

    const destination = resolveKanbanDropDestination(state, 'active', 'anchor');

    expect(destination?.targetIndex).toBe(0);
    expect(destination?.request).toEqual({ status: 'validation', before_id: 'anchor' });
    expect(destination?.request).not.toHaveProperty('position');
  });

  it('uses only the filtered visible anchors and never invents an index', () => {
    const filteredVisibleState = columns({
      started: [card('active', 'started')],
      validation: [card('only-filter-match', 'validation')],
    });

    const destination = resolveKanbanDropDestination(
      filteredVisibleState,
      'active',
      'only-filter-match',
    );

    expect(destination).toEqual({
      targetStatus: 'validation',
      targetIndex: 0,
      request: { status: 'validation', before_id: 'only-filter-match' },
    });
    expect(destination?.request).not.toHaveProperty('position');
  });

  it('removes the active card before deriving a same-column optimistic index', () => {
    const state = columns({
      in_progress: [
        card('active', 'in_progress'),
        card('anchor', 'in_progress', 1),
        card('later', 'in_progress', 2),
      ],
    });

    const destination = resolveKanbanDropDestination(state, 'active', 'anchor');

    expect(destination?.targetIndex).toBe(0);
    expect(destination?.targetIndex).toBeGreaterThanOrEqual(0);
    expect(destination?.request).not.toHaveProperty('position');
  });

  it('fails closed for a missing source, self-drop, or unknown target', () => {
    const state = columns({ started: [card('active', 'started')] });

    expect(resolveKanbanDropDestination(state, 'missing', 'done')).toBeNull();
    expect(resolveKanbanDropDestination(state, 'active', 'active')).toBeNull();
    expect(resolveKanbanDropDestination(state, 'active', 'not-rendered')).toBeNull();
  });

  it('never emits position across every column destination', () => {
    for (const status of CARD_STATUSES.filter((candidate) => candidate !== 'rejected')) {
      const state = columns({
        started: [card('active', 'started')],
        [status]: status === 'started'
          ? [card('active', 'started')]
          : [card(`visible-${status}`, status)],
      });
      const destination = resolveKanbanDropDestination(state, 'active', status);
      expect(destination?.targetIndex).toBeGreaterThanOrEqual(0);
      expect(destination?.request).not.toHaveProperty('position');
      expect(destination?.request.placement).toBe('end');
    }
  });

  it('refuses every manual inbound drop into the consequence-only Rejected column', () => {
    const state = columns({
      validation: [card('active', 'validation')],
      rejected: [card('rework-anchor', 'rejected')],
    });

    expect(resolveKanbanDropDestination(state, 'active', 'rejected')).toBeNull();
    expect(resolveKanbanDropDestination(state, 'active', 'rework-anchor')).toBeNull();
  });

  it('allows Rejected reorder and its sole public exit to In Progress', () => {
    const state = columns({
      rejected: [
        card('active', 'rejected'),
        card('rework-anchor', 'rejected', 1),
      ],
      in_progress: [card('implementation-anchor', 'in_progress')],
    });

    expect(resolveKanbanDropDestination(state, 'active', 'rework-anchor'))
      .toMatchObject({
        targetStatus: 'rejected',
        request: { status: 'rejected', before_id: 'rework-anchor' },
      });
    expect(resolveKanbanDropDestination(state, 'active', 'in_progress'))
      .toMatchObject({
        targetStatus: 'in_progress',
        request: { status: 'in_progress', placement: 'end' },
      });
    for (const target of ['not_started', 'started', 'validation', 'on_hold', 'done', 'cancelled'] as CardStatus[]) {
      expect(resolveKanbanDropDestination(state, 'active', target)).toBeNull();
    }
  });
});
