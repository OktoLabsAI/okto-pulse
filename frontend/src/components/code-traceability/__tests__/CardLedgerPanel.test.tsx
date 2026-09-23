import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { CardLedgerPanel } from '../CardLedgerPanel';

const api = vi.hoisted(() => ({ getCardDeliveryLedger: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const props = { boardId: 'b', cardId: 'c', specId: 's', edition: 2 };
const row = { id: 'proof', kind: 'implementation', actor_id: 'author-A', created_at: 'today', summary: 'Original implementation', revoked: true };
const page = { board_id: 'b', card_id: 'c', spec_id: 's', edition: 2, current_edition: 2, total: 21, next_cursor: 'cursor', items: [row] };
beforeEach(() => { vi.resetAllMocks(); api.getCardDeliveryLedger.mockResolvedValue(page); });
afterEach(cleanup);

it('pages mixed records and reads original detail without presenting it as current proof', async () => {
  render(<CardLedgerPanel {...props} />);
  fireEvent.click(screen.getByText('Browse delivery records'));
  await screen.findByText('Original implementation');
  api.getCardDeliveryLedger.mockResolvedValueOnce({ ...page, next_cursor: null, items: [{ ...row, id: 'old', summary: 'Earlier proof' }] });
  fireEvent.click(screen.getByText('Older delivery records'));
  await screen.findByText('Earlier proof');
  expect(api.getCardDeliveryLedger.mock.calls[1][4]).toEqual({ cursor: 'cursor' });
  api.getCardDeliveryLedger.mockResolvedValueOnce({ ...page, items: [{ ...row, summary: 'Full original explanation', payload: {
    execution_id: 'execution-A', contributions: [{ obligation_ref: 'fr', contribution: 'partial' }],
  } }] });
  fireEvent.click(screen.getByText('Read delivery record old'));
  await screen.findByText('Full original explanation');
  expect(screen.getByText('fr: declared partial.')).toBeInTheDocument();
  expect(screen.getByText(/Currentness is not evaluated/)).toBeInTheDocument();
});

it('changes edition by cancelling pending reads and clearing the old scope', async () => {
  let finish!: (value: unknown) => void;
  api.getCardDeliveryLedger.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  render(<CardLedgerPanel {...props} />);
  fireEvent.click(screen.getByText('Browse delivery records'));
  const signal = api.getCardDeliveryLedger.mock.calls[0][5];
  fireEvent.change(screen.getByLabelText('History edition'), { target: { value: '1' } });
  expect(signal.aborted).toBe(true);
  await act(async () => finish(page));
  expect(screen.queryByText('Original implementation')).not.toBeInTheDocument();
  api.getCardDeliveryLedger.mockResolvedValueOnce({ ...page, edition: 1, historical: true });
  fireEvent.click(screen.getByText('Browse delivery records'));
  await screen.findByText(/Previous edition/);
  expect(api.getCardDeliveryLedger.mock.calls[1][3]).toBe(1);
});

it('clears prior results after denial and refuses a foreign scope response', async () => {
  render(<CardLedgerPanel {...props} />);
  fireEvent.click(screen.getByText('Browse delivery records'));
  await screen.findByText('Original implementation');
  api.getCardDeliveryLedger.mockRejectedValueOnce(new Error('Permission denied'));
  fireEvent.click(screen.getByText('Older delivery records'));
  await screen.findByRole('alert');
  expect(screen.queryByText('Original implementation')).not.toBeInTheDocument();
  api.getCardDeliveryLedger.mockResolvedValueOnce({ ...page, card_id: 'foreign' });
  fireEvent.click(screen.getByText('Browse delivery records'));
  await screen.findByText('History scope changed. Restart the read.');
  expect(screen.queryByText('Original implementation')).not.toBeInTheDocument();
});
