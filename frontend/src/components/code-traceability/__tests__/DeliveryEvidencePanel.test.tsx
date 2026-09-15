import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DeliveryEvidencePanel } from '../DeliveryEvidencePanel';
import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';

const api = vi.hoisted(() => ({ getDeliveryEvidence: vi.fn(), recordDeliveryEvidence: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
function projection(): DeliveryEvidenceProjection {
  return { board_id: 'b', spec_id: 's', edition: 2, version: 7, status: 'in_progress', allowed: false, complete: true,
    blockers: ['delivery_test_result_missing'], rejected_record_ids: [],
    rows: [{ obligation: { title: 'Return the expected version', binding: { obligation_ref: 'ac:1', semantic_sha256: 'a'.repeat(64) } }, implementation_ids: ['impl'], test_ids: [], implementation_waiver_ids: [], test_waiver_ids: [], implementation_satisfied: true, test_satisfied: false }],
    implementations: [{ id: 'impl', card_id: 'task', relative_path: 'src/api.py', result_revision: 'a'.repeat(40), current_accepted_execution: true }],
    candidates: [{ kind: 'implementation', id: 'execution', card_id: 'task', label: 'API commit' }, { kind: 'test', id: 'scenario', card_id: 'test-card', label: 'Version assertion' }], records: [] };
}
beforeEach(() => { vi.clearAllMocks(); api.getDeliveryEvidence.mockResolvedValue(projection()); api.recordDeliveryEvidence.mockResolvedValue({ id: 'new', replayed: false }); });
afterEach(cleanup);

it('shows missing verification without claiming the planning matrix proves delivery', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  expect(await screen.findByText('Return the expected version')).toBeTruthy();
  expect(screen.getByText('Missing / stale')).toBeTruthy();
  expect(screen.getByText(/separate from the planning/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Record association' })).toBeNull();
  fireEvent.click(screen.getByText('Recorded (1)'));
  expect(screen.getByText('src/api.py')).toBeTruthy();
  expect(screen.getByText('Task/bug: task')).toBeTruthy();
});

it('binds authenticated test-card candidate and implementation IDs using server version', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" canTest />);
  await screen.findByText('Return the expected version');
  fireEvent.click(screen.getByLabelText('Select ac:1'));
  fireEvent.change(screen.getByLabelText('Record type'), { target: { value: 'test' } });
  expect(screen.queryByRole('option', { name: 'Code delivered by task / bug' })).toBeNull();
  fireEvent.change(screen.getByRole('combobox', { name: /Accepted current proof/ }), { target: { value: 'test-card:scenario' } });
  fireEvent.click(screen.getByRole('checkbox', { name: /src\/api.py/ }));
  fireEvent.change(screen.getByRole('textbox', { name: /Explanation/ }), { target: { value: 'Verified the committed version output.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Record association' }));
  await waitFor(() => expect(api.recordDeliveryEvidence).toHaveBeenCalledTimes(1));
  expect(api.recordDeliveryEvidence.mock.calls[0]).toEqual(['b', 's', expect.objectContaining({ expected_edition: 2, expected_version: 7, kind: 'test', card_id: 'test-card', scenario_id: 'scenario', implementation_ids: ['impl'], obligation_refs: ['ac:1'] })]);
  expect(api.recordDeliveryEvidence.mock.calls[0][2]).not.toHaveProperty('verified');
});

it('keeps done Specs done and displays waiver distinctly from test success', async () => {
  const p = projection(); p.status = 'done'; p.rows[0].test_waiver_ids = ['waiver']; p.rows[0].test_satisfied = true; p.allowed = true;
  api.getDeliveryEvidence.mockResolvedValue(p);
  render(<DeliveryEvidencePanel boardId="b" specId="s" canWaive />);
  expect(await screen.findByText(/has not been reopened/)).toBeTruthy();
  expect(screen.getByText('Explicitly waived — not tested')).toBeTruthy();
  expect(screen.getByLabelText('Record type')).toHaveProperty('value', 'waiver');
});

it('does not retain a success message after refresh fails', async () => {
  const p = projection(); p.allowed = true;
  api.getDeliveryEvidence.mockResolvedValueOnce(p).mockRejectedValueOnce(new Error('Unavailable'));
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  await screen.findByText(/Delivery proof complete/);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByRole('alert');
  expect(screen.queryByText(/Delivery proof complete/)).toBeNull();
});

it('retries an uncertain save with the same idempotency key', async () => {
  api.recordDeliveryEvidence.mockRejectedValueOnce(new Error('Network failed'));
  render(<DeliveryEvidencePanel boardId="b" specId="s" canRecord />);
  await screen.findByText('Return the expected version');
  fireEvent.click(screen.getByLabelText('Select ac:1'));
  fireEvent.change(screen.getByRole('combobox', { name: /Accepted current proof/ }), { target: { value: 'task:execution' } });
  fireEvent.change(screen.getByRole('textbox', { name: /Explanation/ }), { target: { value: 'Implements output' } });
  fireEvent.click(screen.getByRole('button', { name: 'Record association' }));
  await screen.findByText('Network failed');
  fireEvent.click(screen.getByRole('button', { name: 'Record association' }));
  await waitFor(() => expect(api.recordDeliveryEvidence).toHaveBeenCalledTimes(2));
  expect(api.recordDeliveryEvidence.mock.calls[0][2]).toEqual(api.recordDeliveryEvidence.mock.calls[1][2]);
});
