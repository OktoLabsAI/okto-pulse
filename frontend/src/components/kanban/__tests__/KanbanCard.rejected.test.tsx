import { DndContext } from '@dnd-kit/core';
import { SortableContext } from '@dnd-kit/sortable';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { CardSummary } from '@/types';
import { KanbanCard } from '../KanbanCard';

function card(overrides: Partial<CardSummary> = {}): CardSummary {
  return {
    id: 'card-1',
    board_id: 'board-1',
    spec_id: 'spec-1',
    title: 'Implement governed rework',
    description: null,
    status: 'rejected',
    priority: 'high',
    position: 0,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-08-14T00:00:00Z',
    updated_at: '2026-08-14T00:00:00Z',
    due_date: null,
    labels: [],
    test_scenario_ids: null,
    conclusions: [],
    card_type: 'normal',
    validations: [{
      id: 'validation-1',
      confidence: 60,
      verdict: 'fail',
      recommendation: 'reject',
      created_at: '2026-08-14T00:01:00Z',
    }],
    ...overrides,
  };
}

function renderCard(value: CardSummary) {
  return render(
    <DndContext>
      <SortableContext items={[value.id]}>
        <KanbanCard
          card={value}
          onClick={vi.fn()}
          nameMap={{ 'agent-1': 'Executor' }}
        />
      </SortableContext>
    </DndContext>,
  );
}

describe('KanbanCard Rejected projection', () => {
  it('labels unfinished rework and reports the failed attempt without color alone', () => {
    renderCard(card());

    expect(screen.getByLabelText('Rejected: rework required')).toBeInTheDocument();
    expect(screen.getByText('Attempt 1 rejected · rework required')).toBeInTheDocument();
    expect(screen.queryByText(/2nd attempt/i)).not.toBeInTheDocument();
  });

  it('counts failed completion attempts rather than unrelated validation entries', () => {
    renderCard(card({
      validations: [
        {
          id: 'validation-0',
          confidence: 95,
          verdict: 'pass',
          recommendation: 'approve',
          created_at: '2026-08-13T23:59:00Z',
        },
        {
          id: 'validation-1',
          confidence: 60,
          verdict: 'fail',
          recommendation: 'reject',
          created_at: '2026-08-14T00:01:00Z',
        },
      ],
    }));

    expect(screen.getByText('Attempt 1 rejected · rework required')).toBeInTheDocument();
  });

  it('does not project the Rejected affordance for Test Cards or legacy Not Started failures', () => {
    const { rerender } = renderCard(card({ card_type: 'test' }));
    expect(screen.queryByLabelText('Rejected: rework required')).not.toBeInTheDocument();

    const legacy = card({ status: 'not_started', card_type: 'normal' });
    rerender(
      <DndContext>
        <SortableContext items={[legacy.id]}>
          <KanbanCard card={legacy} onClick={vi.fn()} nameMap={{}} />
        </SortableContext>
      </DndContext>,
    );
    expect(screen.queryByLabelText('Rejected: rework required')).not.toBeInTheDocument();
  });
});
