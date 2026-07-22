import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ActivityLogList } from '../ActivityLogList';
import type { ActivityLogEntry } from '@/services/api';

const baseEntry: ActivityLogEntry = {
  id: 'act-1',
  action: 'structured_entity_updated',
  actor_type: 'agent',
  actor_id: 'agent-1',
  actor_name: 'Validator Agent',
  created_at: '2026-05-29T10:15:00Z',
  summary: 'structured_entity updated type=functional_requirement field=description',
  trigger: 'structured_entity_updated',
  details: {
    field: 'description',
    before: { text: 'old' },
    after: { text: 'new' },
    token: '[redacted]',
  },
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ActivityLogList', () => {
  it('uses the same clean metadata presentation as spec history', () => {
    const localDate = '29/05/2026, 07:15:00';
    vi.spyOn(Date.prototype, 'toLocaleString').mockReturnValue(localDate);

    render(<ActivityLogList entries={[baseEntry]} />);

    expect(screen.getByText(baseEntry.summary)).toBeInTheDocument();
    expect(screen.getByText('Validator Agent')).toBeInTheDocument();
    expect(screen.getByText(localDate)).toBeInTheDocument();
    expect(screen.getByText('structured_entity_updated')).toBeInTheDocument();
    expect(screen.queryByText('agent', { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText('Details', { exact: true })).not.toBeInTheDocument();
  });

  it('formats card movement like spec status history and keeps Before and After exclusive', () => {
    render(
      <ActivityLogList
        entries={[{
          ...baseEntry,
          id: 'act-move',
          action: 'card_moved',
          summary: 'not_started->started',
          trigger: null,
          details: {
            from_status: 'not_started',
            to_status: 'started',
            from_position: 0,
            to_position: 2,
          },
        }]}
      />
    );

    const toggle = screen.getByRole('button', { name: /Status changed.*Status: not_started → started/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('not_started->started')).not.toBeInTheDocument();

    fireEvent.click(toggle);

    const before = screen.getByRole('region', { name: 'status before value' });
    const after = screen.getByRole('region', { name: 'status after value' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(within(before).getByText('Before')).toBeInTheDocument();
    expect(within(before).getByText('not_started')).toBeInTheDocument();
    expect(within(before).queryByText('started')).not.toBeInTheDocument();
    expect(within(after).getByText('After')).toBeInTheDocument();
    expect(within(after).getByText('started')).toBeInTheDocument();
    expect(within(after).queryByText('not_started')).not.toBeInTheDocument();
  });

  it('renders multiple explicit changes in their own Before and After regions', () => {
    render(
      <ActivityLogList
        entries={[{
          ...baseEntry,
          action: 'card_updated',
          details: {
            changes: [
              { field: 'priority', old: 'low', new: 'high' },
              { field: 'labels', old: ['backend'], new: ['backend', 'urgent'] },
            ],
          },
        }]}
      />
    );

    const toggle = screen.getByRole('button', { name: /Updated.*Updated: priority, labels/i });
    fireEvent.click(toggle);

    const priorityBefore = screen.getByRole('region', { name: 'priority before value' });
    const priorityAfter = screen.getByRole('region', { name: 'priority after value' });
    const labelsBefore = screen.getByRole('region', { name: 'labels before value' });
    const labelsAfter = screen.getByRole('region', { name: 'labels after value' });

    expect(within(priorityBefore).getByText('low')).toBeInTheDocument();
    expect(within(priorityBefore).queryByText('high')).not.toBeInTheDocument();
    expect(within(priorityAfter).getByText('high')).toBeInTheDocument();
    expect(within(labelsBefore).getByText('1. backend')).toBeInTheDocument();
    expect(labelsAfter).toHaveTextContent('1. backend');
    expect(labelsAfter).toHaveTextContent('2. urgent');
  });

  it('does not expose the card-only raw JSON details panel', () => {
    render(<ActivityLogList entries={[baseEntry]} />);

    expect(screen.queryByText('Details', { exact: true })).not.toBeInTheDocument();
    expect(document.body.textContent ?? '').not.toContain('[redacted]');
    expect(document.body.textContent ?? '').not.toContain('[object Object]');
  });

  it('uses the same loading and empty states as spec history', () => {
    const { rerender } = render(<ActivityLogList entries={[]} loading />);
    expect(screen.getByText('Loading history...')).toBeInTheDocument();

    rerender(<ActivityLogList entries={[]} />);
    expect(screen.getByText('No history yet')).toBeInTheDocument();
    expect(screen.queryByText('No activity recorded')).not.toBeInTheDocument();
  });
});
