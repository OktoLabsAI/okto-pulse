import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { DeliverySelectionEditor } from '../DeliverySelectionEditor';

const api = vi.hoisted(() => ({ getDeliveryEvidence: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); api.getDeliveryEvidence.mockResolvedValue({ edition: 3, per_card: [{ card_id: 'card', card_version: 4, delivery_revision: 2, selection: {
  total: 2, truncated: false, records: [{ id: 'proof', kind: 'implementation', summary: 'Parser delivered' }, { id: 'note', kind: 'progress', summary: 'Review notes' }],
} }] }); });
function props() { return { boardId: 'board', specId: 'spec', cardId: 'card', onChange: vi.fn(), onPending: vi.fn() }; }

it('loads exact IDs and revisions and permits a narrower explicit selection', async () => {
  const p = props(); render(<DeliverySelectionEditor {...p} />);
  expect(api.getDeliveryEvidence).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText('Seal recorded evidence with this report'));
  await screen.findByText('Delivery revision 2 · 2 selected');
  fireEvent.click(screen.getByLabelText('progress: Review notes'));
  expect(p.onChange).toHaveBeenLastCalledWith({ expected_card_version: 4, expected_spec_edition: 3, expected_delivery_revision: 2, record_ids: ['proof'] });
  expect(p.onPending).toHaveBeenLastCalledWith(false);
  fireEvent.click(screen.getByLabelText('Use accumulated impact from the selected records'));
  expect(p.onChange).toHaveBeenLastCalledWith(expect.objectContaining({ record_ids: ['proof'], reuse_impact: true }));
  fireEvent.click(screen.getByLabelText('progress: Review notes'));
  expect(p.onChange).toHaveBeenLastCalledWith(expect.objectContaining({ record_ids: ['proof', 'note'], reuse_impact: true }));
});

it('keeps submission pending on read failure and refreshes before retry', async () => {
  api.getDeliveryEvidence.mockRejectedValueOnce(new Error('Denied'));
  const p = props(); render(<DeliverySelectionEditor {...p} />);
  fireEvent.click(screen.getByLabelText('Seal recorded evidence with this report'));
  await screen.findByText('Denied');
  expect(p.onPending).toHaveBeenLastCalledWith(true);
  expect(p.onChange).toHaveBeenLastCalledWith(undefined);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh evidence selection' }));
  await screen.findByText('Delivery revision 2 · 2 selected');
  expect(p.onPending).toHaveBeenLastCalledWith(false);
});

it('does not fabricate a revision or accept another card’s records', async () => {
  api.getDeliveryEvidence.mockResolvedValue({ edition: 3, per_card: [{ card_id: 'foreign', card_version: 4, delivery_revision: 2, selection: { total: 1, truncated: false, records: [{ id: 'secret', summary: 'Private' }] } }] });
  const p = props(); render(<DeliverySelectionEditor {...p} />);
  fireEvent.click(screen.getByLabelText('Seal recorded evidence with this report'));
  await screen.findByText(/current delivery selection is unavailable/);
  expect(screen.queryByText('Private')).not.toBeInTheDocument();
  expect(p.onPending).toHaveBeenLastCalledWith(true);
});

it('does not silently select a truncated population', async () => {
  api.getDeliveryEvidence.mockResolvedValue({ edition: 1, per_card: [{ card_id: 'card', card_version: 1, delivery_revision: 300, selection: { total: 300, truncated: true, records: [{ id: 'one', kind: 'progress', summary: 'One' }] } }] });
  const p = props(); render(<DeliverySelectionEditor {...p} />);
  fireEvent.click(screen.getByLabelText('Seal recorded evidence with this report'));
  await screen.findByText(/No records were selected automatically/);
  expect(p.onChange).toHaveBeenLastCalledWith(expect.objectContaining({ record_ids: [], expected_delivery_revision: 300 }));
});

it('turning off selection cancels a pending response', async () => {
  let finish!: (value: unknown) => void;
  api.getDeliveryEvidence.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const p = props(); render(<DeliverySelectionEditor {...p} />);
  const toggle = screen.getByLabelText('Seal recorded evidence with this report');
  fireEvent.click(toggle); fireEvent.click(toggle);
  finish({ edition: 1, per_card: [] });
  await waitFor(() => expect(p.onPending).toHaveBeenLastCalledWith(false));
  expect(p.onChange).toHaveBeenLastCalledWith(undefined);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
