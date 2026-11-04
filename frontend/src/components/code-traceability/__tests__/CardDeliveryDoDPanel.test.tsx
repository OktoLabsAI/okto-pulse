import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CardDeliveryDoDPanel } from '../CardDeliveryDoDPanel';
import { persistedProjection } from './readinessFixture';
import type { TaskDeliveryState, TaskDeliverySummary } from '@/types/task-delivery';

const api = vi.hoisted(() => ({ getPersistedReadinessProjection: vi.fn(), getDeliveryEvidence: vi.fn(), getBoard: vi.fn(),
  getDeliveryReadinessPage: vi.fn(), getCardDeliveryLedger: vi.fn(), getTaskRequirementOptions: vi.fn(), submitHumanTask: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const card = { id: 'c', card_type: 'normal', spec_id: 's' };
export function summary(state: TaskDeliveryState = 'implementation_pending'): TaskDeliverySummary {
  return { contract_version: 'task-delivery-summary/v1', board_id: 'b', spec_id: 's', card_id: 'c',
    spec_edition: 1, card_version: 1, snapshot_id: 'a'.repeat(64), population_known: true, evaluation_complete: true,
    state, reason_code: state, reason: `Canonical reason: ${state}`, historical_status: 'in_progress',
    next_action: { kind: 'record_progress', label: 'Record progress', owner: 'user', mutation_reauthorization_required: true },
    pending_count: 1, last_update: null };
}
function projection(state?: TaskDeliveryState) {
  const result = persistedProjection(); result.scope.card_id = 'c'; result.task_delivery = summary(state); return result;
}
beforeEach(() => { vi.resetAllMocks(); api.getPersistedReadinessProjection.mockResolvedValue(projection()); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('reads one scoped projection and keeps all technical sections lazy', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={card} canProgress />);
  await screen.findByText('Work remaining');
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  expect(api.getPersistedReadinessProjection.mock.calls[0][2]).toEqual({ cardId: 'c', unitLimit: 20 });
  expect(screen.getByRole('button', { name: 'Details' })).toHaveAttribute('aria-expanded', 'false');
  for (const fn of [api.getBoard, api.getDeliveryEvidence, api.getDeliveryReadinessPage, api.getCardDeliveryLedger, api.getTaskRequirementOptions]) expect(fn).not.toHaveBeenCalled();
  expect(screen.queryByRole('slider')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Details' }));
  expect(await screen.findByText('Delivery details')).toBeInTheDocument();
  for (const fn of [api.getBoard, api.getDeliveryEvidence, api.getDeliveryReadinessPage, api.getCardDeliveryLedger, api.getTaskRequirementOptions]) expect(fn).not.toHaveBeenCalled();
});

it.each([
  ['updating_or_unknown', 'Checking delivery'], ['optional_or_waived', 'Not required'],
  ['verification_failed', 'Verification failed'], ['historical_done', 'Previously completed'],
  ['implementation_pending', 'Work remaining'], ['verification_pending', 'Awaiting verification'],
  ['completion_pending', 'Awaiting completion'], ['delivered', 'Delivered'], ['ready', 'Ready to complete'],
] as const)('renders the Core state %s without a competing verdict', async (state, title) => {
  api.getPersistedReadinessProjection.mockResolvedValue(projection(state));
  render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await screen.findByText(title);
  await screen.findByText(`Canonical reason: ${state}`);
  if (state === 'delivered' || state === 'verification_failed') expect(document.querySelector(state === 'delivered' ? '.lucide-circle-check' : '.lucide-circle-x')).toBeInTheDocument();
});

it('fails closed on transport loss and retries only when requested', async () => {
  api.getPersistedReadinessProjection.mockRejectedValue(new Error('Network unavailable'));
  render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await screen.findByText('Delivery unavailable');
  await new Promise(resolve => setTimeout(resolve, 1100));
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  api.getPersistedReadinessProjection.mockResolvedValue(projection('delivered'));
  fireEvent.click(screen.getByRole('button', { name: 'Retry delivery assessment' }));
  await screen.findByText('Delivered');
});

it.each(['normal', 'bug', 'test'])('shows a single verdict and escapes human content for %s cards', async cardType => {
  const payload = '<script>window.deliveryInjected=true</script><img src=x onerror=alert(1)>';
  const value = projection('verification_failed');
  value.task_delivery!.last_update = { record_id: 'human-record', content: payload, remaining: 'Review required',
    submitted_at: '2026-11-04T00:00:00Z', origin: 'human_submission', text_truncated: false };
  api.getPersistedReadinessProjection.mockResolvedValue(value);
  const { container } = render(<CardDeliveryDoDPanel boardId="b" card={{ ...card, card_type: cardType }} canProgress />);
  expect(await screen.findByText(payload)).toBeVisible();
  expect(screen.getAllByRole('status')).toHaveLength(1);
  expect(screen.getByRole('status')).toHaveTextContent('Verification failed');
  expect(container.querySelector('script, img')).toBeNull();
  expect(container.querySelector('.lucide-circle-x')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Details' })).toHaveAttribute('aria-expanded', 'false');
  expect(screen.queryByRole('slider')).not.toBeInTheDocument();
});

it('coalesces update bursts and stops polling when the tab is inactive', async () => {
  vi.useFakeTimers();
  const pending = projection(); pending.projection.freshness = 'updating';
  api.getPersistedReadinessProjection.mockResolvedValue(pending);
  const view = render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await act(async () => { await Promise.resolve(); });
  act(() => { for (let n = 0; n < 30; n++) window.dispatchEvent(new Event('pulse:delivery-evidence-changed')); });
  await act(async () => { await vi.advanceTimersByTimeAsync(999); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} active={false} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
});

it('does not replace a human draft when a refreshed projection changes its version', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={card} canProgress />);
  fireEvent.click(await screen.findByRole('button', { name: 'Record progress' }));
  const input = screen.getByLabelText('What was delivered or changed?');
  fireEvent.change(input, { target: { value: 'My draft' } });
  const next = projection(); next.task_delivery!.card_version = 2;
  api.getPersistedReadinessProjection.mockResolvedValue(next);
  fireEvent.click(screen.getByRole('button', { name: /^Refresh$/ }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save update' })).toBeDisabled());
  expect(input).toHaveValue('My draft');
  expect(api.submitHumanTask).not.toHaveBeenCalled();
});

it('keeps the same draft, focus and text selection when Details is toggled', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={card} canProgress />);
  fireEvent.click(await screen.findByRole('button', { name: 'Record progress' }));
  const input = screen.getByLabelText('What was delivered or changed?') as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: 'Draft preserved across Details' } });
  input.focus(); input.setSelectionRange(2, 8);
  fireEvent.click(screen.getByRole('button', { name: 'Details' }));
  fireEvent.click(screen.getByRole('button', { name: 'Details' }));
  expect(screen.getByLabelText('What was delivered or changed?')).toBe(input);
  expect(input).toHaveValue('Draft preserved across Details');
  expect(input).toHaveFocus();
  expect([input.selectionStart, input.selectionEnd]).toEqual([2, 8]);
  expect(api.submitHumanTask).not.toHaveBeenCalled();
});
