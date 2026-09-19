import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CardProgressPanel } from '../CardProgressPanel';
import { CardDeliveryDoDPanel } from '../CardDeliveryDoDPanel';
import type { DeliveryPerCard } from '@/types/delivery-evidence';

const api = vi.hoisted(() => ({ recordCardDeliveryEvidence: vi.fn(), getDeliveryEvidence: vi.fn(), getBoard: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const card: DeliveryPerCard = { card_id: 'card', card_version: 7, delivery_revision: 4, title: 'Parser', card_type: 'normal', status: 'in_progress', obligations: [], satisfied: false };
const props = () => ({ boardId: 'board', specId: 'spec', edition: 2, card, canWrite: true, onSaved: vi.fn() });
beforeEach(() => { vi.clearAllMocks(); api.recordCardDeliveryEvidence.mockResolvedValue({ id: 'saved' }); });
afterEach(cleanup);
function fill() {
  fireEvent.change(screen.getByLabelText('Work recorded'), { target: { value: 'Parser changed' } });
  fireEvent.change(screen.getByLabelText('Remaining work'), { target: { value: 'Normalization missing' } });
}

it('saves dirty progress without an execution receipt or final completion', async () => {
  const p = props(); render(<CardProgressPanel {...p} />); fill();
  fireEvent.click(screen.getByLabelText('Work is in an external dirty workspace'));
  fireEvent.click(screen.getByRole('button', { name: 'Save progress' }));
  await waitFor(() => expect(p.onSaved).toHaveBeenCalledOnce());
  expect(api.recordCardDeliveryEvidence.mock.calls[0]).toEqual(['board', 'card', 'spec', expect.objectContaining({
    contract_version: 'card-delivery-batch/v1', expected_card_version: 7, expected_spec_edition: 2, expected_delivery_revision: 4,
    entries: [{ client_ref: 'progress', kind: 'progress', obligation_refs: [], justification: 'Parser changed',
      progress: { contract_version: 'delivery-progress/v1', remaining: 'Normalization missing', source_state: { workspace_state: 'dirty', recoverability: 'external_workspace' } } }],
  })]);
  expect(api.recordCardDeliveryEvidence.mock.calls[0][3]).not.toHaveProperty('execution_id');
});

it('reuses the idempotency key after an uncertain failure', async () => {
  api.recordCardDeliveryEvidence.mockRejectedValueOnce(new Error('Timeout'));
  render(<CardProgressPanel {...props()} />); fill();
  fireEvent.click(screen.getByRole('button', { name: 'Save progress' }));
  await screen.findByText('Timeout');
  fireEvent.click(screen.getByRole('button', { name: 'Save progress' }));
  await waitFor(() => expect(api.recordCardDeliveryEvidence).toHaveBeenCalledTimes(2));
  expect(api.recordCardDeliveryEvidence.mock.calls[0][3]).toEqual(api.recordCardDeliveryEvidence.mock.calls[1][3]);
});

it('prevents double submit and ignores a response after changing Card', async () => {
  let finish!: () => void;
  api.recordCardDeliveryEvidence.mockImplementation(() => new Promise<void>(resolve => { finish = resolve; }));
  const p = props(); const view = render(<CardProgressPanel {...p} />); fill();
  const save = screen.getByRole('button', { name: 'Save progress' });
  fireEvent.click(save); fireEvent.click(save);
  expect(api.recordCardDeliveryEvidence).toHaveBeenCalledOnce();
  view.unmount();
  await act(async () => { finish(); });
  expect(p.onSaved).not.toHaveBeenCalled();
});

it.each(['validation', 'rejected', 'done', 'not_started', 'on_hold'])('cannot append while %s', status => {
  render(<CardProgressPanel {...props()} card={{ ...card, status }} />);
  expect(screen.queryByRole('button', { name: 'Save progress' })).not.toBeInTheDocument();
});

it('removes the write action when permission is revoked', () => {
  const p = props(); const view = render(<CardProgressPanel {...p} />); fill();
  view.rerender(<CardProgressPanel {...p} canWrite={false} />);
  expect(screen.queryByRole('button', { name: 'Save progress' })).not.toBeInTheDocument();
});

it('does not invent a delivery revision when the projection is unavailable', () => {
  render(<CardProgressPanel {...props()} card={{ ...card, delivery_revision: undefined }} />);
  expect(screen.queryByRole('button', { name: 'Save progress' })).not.toBeInTheDocument();
});

it('uses zero as a real empty-ledger revision', async () => {
  render(<CardProgressPanel {...props()} card={{ ...card, delivery_revision: 0 }} />); fill();
  fireEvent.click(screen.getByRole('button', { name: 'Save progress' }));
  await waitFor(() => expect(api.recordCardDeliveryEvidence).toHaveBeenCalledOnce());
  expect(api.recordCardDeliveryEvidence.mock.calls[0][3].expected_delivery_revision).toBe(0);
});

it('shows several saved facts and an explicit history limit after reload', async () => {
  const entries = ['First change', 'Second change'].map((summary, i) => ({ id: String(i), actor_id: 'original-author', created_at: 'today', summary, remaining: 'Next work', text_truncated: false, source_state: { workspace_state: 'dirty', recoverability: 'external_workspace' }, target_ids: [] }));
  api.getBoard.mockResolvedValue({ settings: {} });
  api.getDeliveryEvidence.mockResolvedValue({ edition: 2, candidates: [], implementations: [], rejected_record_ids: [], rows: [], per_card: [{ ...card, progress: { total: 25, truncated: true, recovery_verified: false, items: entries } }] });
  render(<CardDeliveryDoDPanel boardId="board" card={{ id: 'card', card_type: 'normal', spec_id: 'spec' }} canProgress />);
  await screen.findByText('First change');
  expect(screen.getByText('Second change')).toBeInTheDocument();
  expect(screen.getByText(/incomplete history/)).toBeInTheDocument();
  expect(screen.getByText(/does not complete this card/)).toBeInTheDocument();
});
