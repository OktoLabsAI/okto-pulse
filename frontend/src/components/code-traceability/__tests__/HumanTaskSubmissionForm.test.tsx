import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HumanTaskSubmissionForm } from '../HumanTaskSubmissionForm';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { TaskDeliverySummary } from '@/types/task-delivery';
const api = vi.hoisted(() => ({ submitHumanTask: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const summary = { card_version: 3, spec_edition: 2 } as TaskDeliverySummary;
const accepted = vi.fn();
beforeEach(() => { vi.resetAllMocks(); api.submitHumanTask.mockResolvedValue({ submission_id: 'saved', replayed: false }); });
afterEach(cleanup);
function mount(action: 'progress' | 'delivery_report' = 'progress') {
  render(<HumanTaskSubmissionForm boardId="b" cardId="c" summary={summary} action={action} requestedStatus="done"
    onAccepted={accepted} onCancel={vi.fn()} onRefresh={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('What was delivered or changed?'), { target: { value: 'Entrega humana' } });
}
it('submits human text once with no receipt, proof, metrics or impact inputs', async () => {
  mount(); fireEvent.click(screen.getByRole('button', { name: 'Save update' }));
  await waitFor(() => expect(accepted).toHaveBeenCalledTimes(1));
  expect(api.submitHumanTask).toHaveBeenCalledTimes(1);
  expect(api.submitHumanTask.mock.calls[0][2]).toEqual({ contract_version: 'human-task-submission/v1', action: 'progress',
    content: 'Entrega humana', expected_card_version: 3, expected_spec_edition: 2, idempotency_key: expect.any(String) });
  expect(screen.getAllByRole('textbox')).toHaveLength(2);
});
it('retries the exact uncertain intent and prevents changing its contents', async () => {
  api.submitHumanTask.mockRejectedValueOnce(new TypeError('Lost response'));
  mount(); fireEvent.click(screen.getByRole('button', { name: 'Save update' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Retry same submission' }));
  await waitFor(() => expect(accepted).toHaveBeenCalledTimes(1));
  expect(api.submitHumanTask.mock.calls[1][2]).toEqual(api.submitHumanTask.mock.calls[0][2]);
});
it('preserves text after rejection and requires explicit review before a new intent', async () => {
  api.submitHumanTask.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'Technical verification is pending' }));
  mount('delivery_report'); fireEvent.click(screen.getByRole('button', { name: 'Submit and complete' }));
  await screen.findByText('Technical verification is pending');
  expect(screen.getByLabelText('What was delivered or changed?')).toHaveValue('Entrega humana');
  expect(screen.getByRole('button', { name: 'Submit and complete' })).toBeDisabled();
  expect(accepted).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'I reviewed the current task' }));
  fireEvent.click(screen.getByRole('button', { name: 'Submit and complete' }));
  await waitFor(() => expect(accepted).toHaveBeenCalledTimes(1));
  expect(api.submitHumanTask.mock.calls[1][2].idempotency_key).not.toBe(api.submitHumanTask.mock.calls[0][2].idempotency_key);
  expect(api.submitHumanTask.mock.calls[1][2].requested_status).toBe('done');
});
it('guards double submission before React rerenders', async () => {
  api.submitHumanTask.mockReturnValue(new Promise(() => {})); mount();
  const form = screen.getByRole('form', { name: 'Task update' });
  fireEvent.submit(form); fireEvent.submit(form);
  expect(api.submitHumanTask).toHaveBeenCalledTimes(1);
});
