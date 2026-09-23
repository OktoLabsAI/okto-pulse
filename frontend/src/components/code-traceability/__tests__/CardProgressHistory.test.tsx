import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CardProgressHistory } from '../CardProgressHistory';

const api = vi.hoisted(() => ({ getCardProgressHistory: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const props = { boardId: 'b', cardId: 'c', specId: 's', edition: 1 };
const item = { id: 'r', actor_id: 'original-author', created_at: 'today', summary: 'Old partial work', remaining: 'Still pending',
  source_state: { workspace_state: 'dirty', recoverability: 'external_workspace' }, target_ids: ['target'], text_truncated: true };
const page = { board_id: 'b', card_id: 'c', spec_id: 's', edition: 1, total: 25, next_cursor: 'cursor', items: [item] };
beforeEach(() => { vi.resetAllMocks(); api.getCardProgressHistory.mockResolvedValue(page); });
afterEach(cleanup);

it('loads older pages and full text without accumulating the whole history', async () => {
  render(<CardProgressHistory {...props} />);
  expect(api.getCardProgressHistory).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Browse progress history'));
  await screen.findByText('Old partial work');
  api.getCardProgressHistory.mockResolvedValueOnce({ ...page, next_cursor: null, items: [{ ...item, id: 'older', summary: 'Earlier blocker' }] });
  fireEvent.click(screen.getByText('Older checkpoints'));
  await screen.findByText('Earlier blocker');
  expect(screen.queryByText('Old partial work')).not.toBeInTheDocument();
  expect(api.getCardProgressHistory.mock.calls[1]).toEqual(['b', 'c', 's', { cursor: 'cursor' }, expect.any(AbortSignal)]);
  api.getCardProgressHistory.mockResolvedValueOnce({ ...page, detail: true, items: [{ ...item, summary: 'Complete original text', revoked: true }] });
  fireEvent.click(screen.getByText('Read full checkpoint older'));
  await screen.findByText('Complete original text');
  expect(screen.getByText(/does not verify your workspace/)).toBeInTheDocument();
  expect(screen.getByText('Revoked; retained as history.')).toBeInTheDocument();
});

it('clears stale history after denial and offers restart', async () => {
  render(<CardProgressHistory {...props} />);
  fireEvent.click(screen.getByText('Browse progress history'));
  await screen.findByText('Old partial work');
  api.getCardProgressHistory.mockRejectedValueOnce(new Error('Permission denied'));
  fireEvent.click(screen.getByText('Older checkpoints'));
  await screen.findByRole('alert');
  expect(screen.queryByText('Old partial work')).not.toBeInTheDocument();
  expect(screen.getByText('Browse progress history')).toBeInTheDocument();
});

it('rejects a changed edition without showing its records', async () => {
  api.getCardProgressHistory.mockResolvedValue({ ...page, edition: 2 });
  render(<CardProgressHistory {...props} />);
  fireEvent.click(screen.getByText('Browse progress history'));
  await screen.findByRole('alert');
  expect(screen.queryByText('Old partial work')).not.toBeInTheDocument();
});

it('cancels pending read on unmount and prevents duplicate requests', async () => {
  let finish!: (value: unknown) => void;
  api.getCardProgressHistory.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<CardProgressHistory {...props} />);
  fireEvent.click(screen.getByText('Browse progress history'));
  fireEvent.click(screen.getByText('Browse progress history'));
  await waitFor(() => expect(api.getCardProgressHistory).toHaveBeenCalledOnce());
  const signal = api.getCardProgressHistory.mock.calls[0][4];
  view.unmount(); expect(signal.aborted).toBe(true);
  await act(async () => finish(page));
});
