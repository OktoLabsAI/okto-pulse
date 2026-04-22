import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { RetryFromHereDialog } from '../RetryFromHereDialog';
import type { PendingTreeNode } from '@/services/kg-api';

vi.mock('@/services/kg-api', () => ({
  retryPending: vi.fn(),
}));

import * as kgApi from '@/services/kg-api';

const NODE: PendingTreeNode = {
  id: 'spec_x',
  type: 'spec',
  title: 'Spec X',
  status: 'failed',
  queue_entry_id: 'q_xyz',
  children: [],
};

describe('RetryFromHereDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls retryPending(boardId, queueEntryId, false) when confirmed without recursive', async () => {
    const onSuccess = vi.fn();
    vi.mocked(kgApi.retryPending).mockResolvedValue({
      board_id: 'b1', queue_entry_id: 'q_xyz', recursive: false,
      reopened_count: 1, reopened_ids: ['q_xyz'],
    });
    render(<RetryFromHereDialog boardId="b1" node={NODE} onClose={() => {}} onSuccess={onSuccess} />);
    fireEvent.click(screen.getByTestId('retry-confirm'));
    await waitFor(() => expect(kgApi.retryPending).toHaveBeenCalledWith('b1', 'q_xyz', false));
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  });

  it('passes recursive=true when checkbox is ticked', async () => {
    vi.mocked(kgApi.retryPending).mockResolvedValue({
      board_id: 'b1', queue_entry_id: 'q_xyz', recursive: true,
      reopened_count: 4, reopened_ids: ['q_xyz', 'q_a', 'q_b', 'q_c'],
    });
    render(<RetryFromHereDialog boardId="b1" node={NODE} onClose={() => {}} />);
    fireEvent.click(screen.getByTestId('retry-recursive-checkbox'));
    fireEvent.click(screen.getByTestId('retry-confirm'));
    await waitFor(() => expect(kgApi.retryPending).toHaveBeenCalledWith('b1', 'q_xyz', true));
  });

  it('shows the API error inline when the request fails', async () => {
    vi.mocked(kgApi.retryPending).mockRejectedValue(new Error('queue entry not found'));
    render(<RetryFromHereDialog boardId="b1" node={NODE} onClose={() => {}} />);
    fireEvent.click(screen.getByTestId('retry-confirm'));
    const err = await screen.findByTestId('retry-error');
    expect(err).toHaveTextContent('queue entry not found');
  });

  it('cancel button invokes onClose without firing the API', () => {
    const onClose = vi.fn();
    render(<RetryFromHereDialog boardId="b1" node={NODE} onClose={onClose} />);
    fireEvent.click(screen.getByTestId('retry-cancel'));
    expect(onClose).toHaveBeenCalled();
    expect(kgApi.retryPending).not.toHaveBeenCalled();
  });

  it('disables confirm when the node lacks a queue_entry_id', () => {
    const orphan: PendingTreeNode = { ...NODE, queue_entry_id: null };
    render(<RetryFromHereDialog boardId="b1" node={orphan} onClose={() => {}} />);
    const btn = screen.getByTestId('retry-confirm') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});
