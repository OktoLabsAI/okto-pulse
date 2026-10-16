import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import toast from 'react-hot-toast';
import * as kgApi from '@/services/kg-api';
import { PrivacyView } from './PrivacyView';
import { GraphControlsPanel } from './GraphControlsPanel';

const policy = vi.hoisted(() => ({
  isLoading: false, error: null as string | null, ownerReviewRequired: false,
  has: vi.fn((flag: string) => flag === 'kg.operations.board.erase'),
}));
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => policy }));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));

beforeEach(() => {
  vi.restoreAllMocks();
  policy.isLoading = false;
  policy.error = null;
  policy.ownerReviewRequired = false;
  policy.has.mockImplementation((flag) => flag === 'kg.operations.board.erase');
});
afterEach(() => cleanup());

describe('KG privacy after technical settings retirement', () => {
  it('requires confirmation and scopes deletion to the selected board', async () => {
    const erase = vi.spyOn(kgApi, 'deleteKG').mockResolvedValue(undefined);
    const confirmation = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { rerender } = render(<PrivacyView boardId="board-a" />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete KG Data' }));
    expect(erase).not.toHaveBeenCalled();
    rerender(<PrivacyView boardId="board-b" />);
    confirmation.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: 'Delete KG Data' }));
    await waitFor(() => expect(erase).toHaveBeenCalledExactlyOnceWith('board-b'));
    expect(toast.success).toHaveBeenCalledWith('Knowledge graph data deleted');
    expect('getKGSettings' in kgApi).toBe(false);
    expect('getHistoricalProgress' in kgApi).toBe(false);
  });

  it.each(['denied', 'loading', 'error', 'review'] as const)('fails closed when authority is %s', (state) => {
    if (state === 'denied') policy.has.mockReturnValue(false);
    if (state === 'loading') policy.isLoading = true;
    if (state === 'error') policy.error = 'unavailable';
    if (state === 'review') policy.ownerReviewRequired = true;
    const erase = vi.spyOn(kgApi, 'deleteKG');
    render(<PrivacyView boardId="board-a" />);
    expect(screen.queryByRole('button', { name: 'Delete KG Data' })).not.toBeInTheDocument();
    expect(erase).not.toHaveBeenCalled();
  });

  it('preserves backend denials and allows a retry after failure', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(kgApi, 'deleteKG').mockRejectedValue(new Error('Forbidden'));
    render(<PrivacyView boardId="board-a" />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete KG Data' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Forbidden'));
    expect(screen.getByRole('button', { name: 'Delete KG Data' })).toBeEnabled();
  });

  it('exposes privacy navigation with the erasure permission alone and removes it on denial', () => {
    const props = {
      boardId: 'board-a', filters: { types: [], edgeTypes: [], graphLayer: 'canonical' as const, minRelevance: 0, searchQuery: '' },
      subView: 'graph' as const, onFiltersChange: vi.fn(), onSubViewChange: vi.fn(),
      nodeCount: 0, nodeLimit: 100, onNodeLimitChange: vi.fn(),
    };
    const { rerender } = render(<GraphControlsPanel {...props} />);
    fireEvent.click(screen.getByRole('button', { name: 'Privacy' }));
    expect(props.onSubViewChange).toHaveBeenCalledWith('privacy');
    expect(screen.queryByRole('button', { name: 'Settings' })).not.toBeInTheDocument();
    policy.has.mockReturnValue(false);
    rerender(<GraphControlsPanel {...props} />);
    expect(screen.queryByRole('button', { name: 'Privacy' })).not.toBeInTheDocument();
  });
});
