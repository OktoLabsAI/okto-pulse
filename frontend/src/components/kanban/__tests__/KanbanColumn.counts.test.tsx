import { DndContext } from '@dnd-kit/core';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { CardSummary } from '@/types';
import { deriveKanbanCardTypeCounts, KanbanColumn } from '../KanbanColumn';

vi.mock('../KanbanCard', () => ({
  KanbanCard: ({ card }: { card: CardSummary }) => <div>{card.title}</div>,
}));

function card(overrides: Partial<CardSummary>): CardSummary {
  return {
    id: overrides.id ?? crypto.randomUUID(),
    board_id: 'board-1',
    spec_id: null,
    title: overrides.title ?? 'Card',
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
    ...overrides,
  };
}

describe('KanbanColumn card-type counters', () => {
  it('counts task, test, bug and total cards defensively', () => {
    const counts = deriveKanbanCardTypeCounts([
      card({ id: 'task-1', card_type: 'normal' }),
      card({ id: 'legacy-task', card_type: undefined }),
      card({ id: 'test-1', card_type: 'test' }),
      card({ id: 'bug-1', card_type: 'CardType.BUG' as any }),
      card({ id: 'object-test', card_type: { value: 'test' } as any }),
    ]);

    expect(counts).toEqual({
      total: 5,
      task: 2,
      test: 2,
      bug: 1,
    });
  });

  it('renders total and per-type counters in the column header', () => {
    render(
      <DndContext>
        <KanbanColumn
          status="not_started"
          cards={[
            card({ id: 'task-1', title: 'Task one', card_type: 'normal' }),
            card({ id: 'test-1', title: 'Test one', card_type: 'test' }),
            card({ id: 'bug-1', title: 'Bug one', card_type: 'bug' }),
            card({ id: 'task-2', title: 'Task two' }),
          ]}
          onCardClick={vi.fn()}
          onAddCard={vi.fn()}
          nameMap={{}}
        />
      </DndContext>,
    );

    expect(screen.getByLabelText('4 total cards')).toBeInTheDocument();
    expect(screen.getByLabelText('2 task cards')).toBeInTheDocument();
    expect(screen.getByLabelText('1 test cards')).toBeInTheDocument();
    expect(screen.getByLabelText('1 bug cards')).toBeInTheDocument();
  });
});
