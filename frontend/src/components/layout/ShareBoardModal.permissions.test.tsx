import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ShareBoardModal } from './ShareBoardModal';

const apiMock = vi.hoisted(() => ({
  listBoardShares: vi.fn(),
  shareBoard: vi.fn(),
  updateBoardShare: vi.fn(),
  revokeBoardShare: vi.fn(),
}));

const permissionState = vi.hoisted(() => ({
  allowed: new Set<string>(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'custom',
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: (flag: string) => permissionState.allowed.has(flag),
  }),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const share = {
  id: 'share-1',
  board_id: 'board-1',
  user_id: 'user-2',
  realm_id: 'realm-1',
  permission: 'viewer' as const,
  shared_by: 'owner-1',
  created_at: '2026-08-07T00:00:00Z',
};

describe('ShareBoardModal canonical permissions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.allowed = new Set();
    apiMock.listBoardShares.mockResolvedValue([share]);
    apiMock.shareBoard.mockResolvedValue(undefined);
    apiMock.updateBoardShare.mockResolvedValue(undefined);
    apiMock.revokeBoardShare.mockResolvedValue(undefined);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  it('does not list shares without board.share.read', async () => {
    render(
      <ShareBoardModal
        isOpen
        onClose={vi.fn()}
        boardId="board-1"
        boardName="Board One"
      />,
    );

    expect(await screen.findByText('You do not have permission to view board shares')).toBeInTheDocument();
    expect(apiMock.listBoardShares).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText('User ID')).toBeDisabled();
  });

  it('maps create, edit, and revoke controls to their exact leaves', async () => {
    permissionState.allowed = new Set(['board.share.read']);
    const view = render(
      <ShareBoardModal
        isOpen
        onClose={vi.fn()}
        boardId="board-1"
        boardName="Board One"
      />,
    );

    await screen.findByText('user-2');
    const userInput = screen.getByPlaceholderText('User ID');
    const rowPermission = screen.getAllByRole('combobox')[1];
    const revoke = screen.getByTitle('Revoke access');
    expect(userInput).toBeDisabled();
    expect(rowPermission).toBeDisabled();
    expect(revoke).toBeDisabled();

    permissionState.allowed = new Set(['board.share.read', 'board.share.create']);
    view.rerender(
      <ShareBoardModal isOpen onClose={vi.fn()} boardId="board-1" boardName="Board One" />,
    );
    fireEvent.change(screen.getByPlaceholderText('User ID'), { target: { value: 'user-3' } });
    fireEvent.submit(screen.getByPlaceholderText('User ID').closest('form')!);
    await waitFor(() => {
      expect(apiMock.shareBoard).toHaveBeenCalledWith('board-1', {
        user_id: 'user-3',
        permission: 'viewer',
      });
    });

    permissionState.allowed = new Set(['board.share.read', 'board.share.edit']);
    view.rerender(
      <ShareBoardModal isOpen onClose={vi.fn()} boardId="board-1" boardName="Board One" />,
    );
    fireEvent.change(screen.getAllByRole('combobox')[1], { target: { value: 'editor' } });
    await waitFor(() => {
      expect(apiMock.updateBoardShare).toHaveBeenCalledWith(
        'board-1',
        'share-1',
        { permission: 'editor' },
      );
    });

    permissionState.allowed = new Set(['board.share.read', 'board.share.revoke']);
    view.rerender(
      <ShareBoardModal isOpen onClose={vi.fn()} boardId="board-1" boardName="Board One" />,
    );
    fireEvent.click(screen.getByTitle('Revoke access'));
    await waitFor(() => {
      expect(apiMock.revokeBoardShare).toHaveBeenCalledWith('board-1', 'share-1');
    });
  });
});
