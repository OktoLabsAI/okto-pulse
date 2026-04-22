import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { KGSyncIndicator } from '../KGSyncIndicator';

describe('KGSyncIndicator', () => {
  it('renders disconnected state', () => {
    render(
      <KGSyncIndicator
        connectionState="disconnected"
        unseenCommits={0}
        onApply={() => {}}
      />,
    );
    const el = screen.getByTestId('kg-sync-indicator');
    expect(el).toHaveAttribute('data-state', 'disconnected');
    expect(el).toHaveTextContent(/Disconnected/);
  });

  it('renders behind state with apply button when commits are pending', () => {
    const onApply = vi.fn();
    render(
      <KGSyncIndicator
        connectionState="connected"
        unseenCommits={3}
        onApply={onApply}
      />,
    );
    const el = screen.getByTestId('kg-sync-indicator');
    expect(el).toHaveAttribute('data-state', 'behind');
    expect(el).toHaveTextContent(/3 new commits/);
    fireEvent.click(el);
    expect(onApply).toHaveBeenCalledTimes(1);
  });

  it('renders live state with relative age when no commits pending', () => {
    const tenSecAgo = new Date(Date.now() - 10_000).toISOString();
    render(
      <KGSyncIndicator
        connectionState="connected"
        unseenCommits={0}
        lastEventAt={tenSecAgo}
        onApply={() => {}}
      />,
    );
    const el = screen.getByTestId('kg-sync-indicator');
    expect(el).toHaveAttribute('data-state', 'live');
    expect(el).toHaveTextContent(/Live/);
    expect(el).toHaveTextContent(/ago/);
  });

  it('shows polling label when in polling fallback', () => {
    render(
      <KGSyncIndicator
        connectionState="polling"
        unseenCommits={0}
        onApply={() => {}}
      />,
    );
    const el = screen.getByTestId('kg-sync-indicator');
    expect(el).toHaveAttribute('data-state', 'polling');
    expect(el).toHaveTextContent(/Polling/);
  });
});
