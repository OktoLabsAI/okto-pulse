import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { GuidedHelpProvider, GUIDED_HELP_STORAGE_KEY, guidedHelpRegistry } from '@/components/guided-help';
import { HelpPanel } from '../HelpPanel';

function completedBoardTourState() {
  return {
    schemaVersion: 1,
    updatedAt: '2026-05-16T19:15:00Z',
    skippedAll: false,
    tours: {
      'board.overview': {
        version: '1',
        status: 'completed',
        steps: {
          'board.navigation.tabs': {
            version: '1',
            status: 'completed',
            completedAt: '2026-05-16T19:15:00Z',
          },
          'board.refresh': {
            version: '1',
            status: 'completed',
            completedAt: '2026-05-16T19:15:00Z',
          },
        },
        completedAt: '2026-05-16T19:15:00Z',
      },
    },
  };
}

function renderHelpPanel() {
  return render(
    <GuidedHelpProvider registry={guidedHelpRegistry} surface="help">
      <HelpPanel onClose={vi.fn()} />
    </GuidedHelpProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
});

describe('HelpPanel guided tours', () => {
  it('opens directly on tours with statuses, restart, and global Skip all recovery controls', async () => {
    renderHelpPanel();

    expect(screen.getByTestId('guided-tours-panel')).toBeInTheDocument();
    expect(screen.getByTestId('guided-tour-row-board.overview')).toHaveTextContent('Board overview');
    expect(screen.getByTestId('guided-tours-reset-all')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('guided-tours-skip-all'));

    expect(screen.getByRole('status')).toHaveTextContent('Skip all is active');
    expect(screen.getByTestId('guided-tours-undo-skip-all')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('guided-tours-undo-skip-all'));

    await waitFor(() => expect(screen.getByTestId('guided-tours-skip-all')).toBeInTheDocument());
  });

  it('replays and resets a completed tour without reloading the panel', async () => {
    localStorage.setItem(GUIDED_HELP_STORAGE_KEY, JSON.stringify(completedBoardTourState()));

    renderHelpPanel();

    const row = screen.getByTestId('guided-tour-row-board.overview');
    expect(row).toHaveTextContent('Completed');
    expect(screen.getByTestId('guided-tour-action-board.overview')).toHaveTextContent('Replay');

    fireEvent.click(screen.getByTestId('guided-tour-action-board.overview'));

    await waitFor(() => expect(row).toHaveTextContent('In progress'));
    expect(screen.getByTestId('guided-tour-action-board.overview')).toHaveTextContent('Replay');

    fireEvent.click(screen.getByTestId('guided-tour-reset-board.overview'));

    await waitFor(() => expect(row).toBeInTheDocument());
  });

  it('restarts all tours and clears Skip all from the visible controls', async () => {
    const state = completedBoardTourState();
    localStorage.setItem(
      GUIDED_HELP_STORAGE_KEY,
      JSON.stringify({ ...state, skippedAll: true, skippedAllAt: '2026-05-16T19:16:00Z' }),
    );

    renderHelpPanel();

    expect(screen.getByTestId('guided-tours-undo-skip-all')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('guided-tours-reset-all'));

    await waitFor(() => expect(screen.getByTestId('guided-tours-skip-all')).toBeInTheDocument());
    expect(screen.getByTestId('guided-tour-row-board.overview')).toHaveTextContent('Not started');
  });
});
