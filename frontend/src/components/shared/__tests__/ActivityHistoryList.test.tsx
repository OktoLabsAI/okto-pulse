import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ActivityHistoryList,
  type ActivityHistoryEntry,
} from '../ActivityHistoryList';

const historyEntry: ActivityHistoryEntry = {
  id: 'history-1',
  action: 'updated',
  actor_type: 'agent',
  actor_name: 'Specification Agent',
  created_at: '2026-05-29T10:15:00Z',
  summary: 'Specification title changed',
  version: 17,
  changes: [
    {
      field: 'title',
      old: 'Old title',
      new: 'New title',
    },
  ],
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ActivityHistoryList', () => {
  it('preserves the spec loading state', () => {
    render(<ActivityHistoryList entries={[]} loading />);

    expect(screen.getByText('Loading history...')).toBeInTheDocument();
    expect(screen.queryByText('No history yet')).not.toBeInTheDocument();
  });

  it('preserves the spec empty state', () => {
    render(<ActivityHistoryList entries={[]} />);

    expect(screen.getByText('No history yet')).toBeInTheDocument();
  });

  it('renders actor, version and local date and expands Before and After separately', () => {
    const localDate = '29/05/2026, 07:15:00';
    const localeSpy = vi.spyOn(Date.prototype, 'toLocaleString').mockReturnValue(localDate);
    render(<ActivityHistoryList entries={[historyEntry]} />);

    expect(screen.getByText('Updated')).toBeInTheDocument();
    expect(screen.getByText('Specification Agent')).toBeInTheDocument();
    expect(screen.getByText('v17')).toBeInTheDocument();
    expect(screen.getByText(localDate)).toBeInTheDocument();
    expect(localeSpy).toHaveBeenCalledOnce();

    const toggle = screen.getByRole('button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: 'title before value' })).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const before = screen.getByRole('region', { name: 'title before value' });
    const after = screen.getByRole('region', { name: 'title after value' });
    expect(within(before).getByText('Before')).toBeInTheDocument();
    expect(within(before).getByText('Old title')).toBeInTheDocument();
    expect(within(after).getByText('After')).toBeInTheDocument();
    expect(within(after).getByText('New title')).toBeInTheDocument();
  });

  it('accepts custom action labels and colors for card adapters', () => {
    render(
      <ActivityHistoryList
        entries={[{ ...historyEntry, action: 'card_moved' }]}
        actionLabels={{ card_moved: 'Card moved' }}
        actionColors={{ card_moved: 'bg-cyan-100 text-cyan-700' }}
      />
    );

    expect(screen.getByText('Card moved')).toHaveClass('bg-cyan-100', 'text-cyan-700');
  });

  it.each([
    ['dependency_added', 'Dependency added'],
    ['dependency_removed', 'Dependency removed'],
    ['spec_dependency_added', 'Spec dependency added'],
    ['spec_dependency_removed', 'Spec dependency removed'],
  ])('humanizes the %s action', (action, label) => {
    render(<ActivityHistoryList entries={[{ ...historyEntry, action }]} />);

    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText(action)).not.toBeInTheDocument();
  });

  it('accepts a technical-revision formatter without changing the default', () => {
    render(
      <ActivityHistoryList
        entries={[historyEntry]}
        versionLabel={(version) => ({
          text: `r${version}`,
          title: `Technical revision r${version}`,
        })}
      />,
    );

    expect(screen.getByText('r17')).toHaveAttribute(
      'title',
      'Technical revision r17',
    );
    expect(screen.queryByText('v17')).not.toBeInTheDocument();
  });
});
