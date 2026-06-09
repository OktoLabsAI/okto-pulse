/**
 * CreateBoardModal tests.
 *
 * AC6 — error surfacing (IMPL-2):
 *   When api.createBoard rejects with a backend Error, the component calls
 *   toast.error with the error's message (the detail surfaced by authFetch)
 *   instead of the old hardcoded 'Failed to create board' string.
 *   Also verifies that the loading state resets after the rejection.
 *
 * AC4 — sidebar freshness (IMPL-3, FR3):
 *   When api.createBoard resolves successfully, addBoard is called with the
 *   returned BoardResponse so the sidebar reflects the new board immediately.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import toast from 'react-hot-toast';
import { CreateBoardModal } from '../CreateBoardModal';

const apiMock = vi.hoisted(() => ({
  createBoard: vi.fn(),
}));

const storeMock = vi.hoisted(() => ({
  addBoard: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: () => storeMock,
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

describe('CreateBoardModal error surfacing (AC6)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows backend detail (not fallback) when createBoard rejects', async () => {
    const backendDetail = 'detail do backend';
    apiMock.createBoard.mockRejectedValue(new Error(backendDetail));

    render(<CreateBoardModal isOpen={true} onClose={vi.fn()} />);

    // Fill in the required Name field
    fireEvent.change(screen.getByPlaceholderText('E.g.: Project X'), {
      target: { value: 'My Board' },
    });

    // Submit the form
    fireEvent.click(screen.getByRole('button', { name: /create board/i }));

    await waitFor(() => {
      expect((toast as any).error).toHaveBeenCalledWith(backendDetail);
    });

    // Must NOT be called with the old hardcoded fallback
    expect((toast as any).error).not.toHaveBeenCalledWith('Failed to create board');

    // Loading resets: the submit button should be re-enabled (not showing 'Creating...')
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /create board/i })).not.toBeDisabled();
    });
  });
});

describe('CreateBoardModal sidebar freshness (AC4)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls addBoard with the returned board on successful submit', async () => {
    const newBoard = {
      id: 'board-new',
      name: 'My Board',
      description: null,
      owner_id: 'owner-1',
      settings: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };
    apiMock.createBoard.mockResolvedValue(newBoard);

    const onClose = vi.fn();
    render(<CreateBoardModal isOpen={true} onClose={onClose} />);

    fireEvent.change(screen.getByPlaceholderText('E.g.: Project X'), {
      target: { value: 'My Board' },
    });

    fireEvent.click(screen.getByRole('button', { name: /create board/i }));

    await waitFor(() => {
      expect(storeMock.addBoard).toHaveBeenCalledWith(newBoard);
    });

    expect((toast as any).success).toHaveBeenCalledWith('Board created');
    expect(onClose).toHaveBeenCalled();
  });
});
