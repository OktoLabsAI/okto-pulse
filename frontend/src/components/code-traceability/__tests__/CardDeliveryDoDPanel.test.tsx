import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { CardDeliveryDoDPanel } from '../CardDeliveryDoDPanel';
import { persistedProjection, readiness } from './readinessFixture';
import type { TaskDeliveryState, TaskDeliverySummary } from '@/types/task-delivery';
import type { Spec } from '@/types';
import type { PersistedReadinessProjection } from '@/types/delivery-readiness';

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
beforeEach(() => {
  vi.resetAllMocks(); api.getPersistedReadinessProjection.mockResolvedValue(projection());
  api.getDeliveryReadinessPage.mockResolvedValue({ ...readiness(), projection: {
    projection_version: 1, generation_id: 'generation-1', snapshot_id: 'a'.repeat(64), freshness: 'current' } });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function openProgress() {
  await screen.findByText('Work remaining');
  fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Record progress' }));
}

function unit(ref: string, implementation = 'satisfied', verification = 'missing'): PersistedReadinessProjection['units'][number] {
  return { card_id: 'c', kind: 'obligation', ref, semantic_sha256: 'a'.repeat(64), spec_edition: 1,
    contribution_card_ids: ['c'], implementation_state: implementation, verification_state: verification,
    implementation_reason_codes: [], verification_reason_codes: [] };
}

it('shows the verdict, canonical percentages and assigned matrix before the two overview actions', async () => {
  const value = projection();
  value.units = [unit('fr:login'), unit('ac:secure', 'partial', 'failed'), unit('tr:audit', 'waived', 'not_applicable')];
  value.page = { offset: 0, limit: 20, total: 3, has_more: false, next_cursor: null };
  value.task_delivery!.last_update = { record_id: 'note', content: 'Narrative belongs in Details', remaining: null,
    submitted_at: '2026-11-04', origin: 'human_submission', text_truncated: false };
  api.getPersistedReadinessProjection.mockResolvedValue(value);
  const spec = { id: 's', edition: 1, version: 1, functional_requirements: [{ id: 'login', text: 'Sign in with a valid account' },
    { id: 'unassigned', text: 'Unassigned work must not appear' }], acceptance_criteria: [{ id: 'secure', text: 'Reject expired credentials' }],
    technical_requirements: [{ id: 'audit', text: 'Record access attempts' }] } as Spec;
  render(<CardDeliveryDoDPanel boardId="b" card={card} spec={spec} canProgress canWaiver />);
  await screen.findByText('Work remaining');
  expect(screen.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '100');
  expect(screen.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  const table = screen.getByRole('table', { name: 'Implementation and verification by requirement' });
  expect(within(table).getAllByRole('row')).toHaveLength(4);
  expect(within(table).getByRole('row', { name: /Sign in with a valid account/ })).toHaveTextContent('ImplementedPending');
  expect(within(table).getByRole('row', { name: /Reject expired credentials/ })).toHaveTextContent('PartialFailed');
  expect(within(table).getByRole('row', { name: /Record access attempts/ })).toHaveTextContent('WaivedNot applicable');
  expect(screen.queryByText('Unassigned work must not appear')).not.toBeInTheDocument();
  expect(screen.queryByText('Narrative belongs in Details')).not.toBeInTheDocument();
  const overview = screen.getByRole('region', { name: 'Task delivery' });
  expect(within(overview).getAllByRole('button').map(button => button.textContent)).toEqual(['How this works', 'Show details', 'Change task requirements']);
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  expect(api.getTaskRequirementOptions).not.toHaveBeenCalled();
  expect(api.getDeliveryReadinessPage).not.toHaveBeenCalled();
});

it('shows test scenarios and keeps unknown and non-applicable percentages distinct from zero', async () => {
  const value = projection('verification_pending');
  const metrics = value.summaries.find(row => row.card_id === 'c')!;
  metrics.card_type = 'test';
  metrics.implementation = { ...metrics.implementation, status: 'not_applicable', percent: null };
  metrics.verification = { ...metrics.verification, status: 'unknown', percent: null };
  value.units = [{ ...unit('scenario-1', 'not_applicable', 'unknown'), kind: 'scenario' }];
  value.page.total = 1;
  api.getPersistedReadinessProjection.mockResolvedValue(value);
  render(<CardDeliveryDoDPanel boardId="b" card={{ ...card, card_type: 'test' }} spec={{ id: 's', edition: 1, version: 1,
    test_scenarios: [{ id: 'scenario-1', title: 'Reject expired credentials' }] } as Spec} />);
  await screen.findByText('Awaiting verification');
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  expect(screen.queryByText('0%')).not.toBeInTheDocument();
  expect(screen.getByRole('row', { name: /Reject expired credentials/ })).toHaveTextContent('Not applicableUnknown');
});

it('pages the assigned matrix on demand and returns to the first page on modal refresh', async () => {
  const first = projection();
  first.units = Array.from({ length: 20 }, (_, n) => unit(`fr:requirement-${n + 1}`));
  first.page = { offset: 0, limit: 20, total: 21, has_more: true, next_cursor: 'page-two' };
  const second = { ...first, units: [unit('fr:requirement-21')], page: { offset: 20, limit: 20, total: 21, has_more: false, next_cursor: null } };
  api.getPersistedReadinessProjection.mockResolvedValueOnce(first).mockResolvedValueOnce(second).mockResolvedValue(first);
  const view = render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await screen.findByText('1–20 of 21 items');
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: 'Next items' }));
  await screen.findByText('21–21 of 21 items', {}, { timeout: 2000 });
  expect(api.getPersistedReadinessProjection.mock.calls[1][2]).toEqual({ cardId: 'c', unitLimit: 20, cursor: 'page-two' });
  expect(screen.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  expect(screen.queryByRole('button', { name: 'Next items' })).not.toBeInTheDocument();
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} refreshRequest={1} />);
  await screen.findByText('1–20 of 21 items', {}, { timeout: 2000 });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(3);
  expect(api.getPersistedReadinessProjection.mock.calls[2][2]).toEqual({ cardId: 'c', unitLimit: 20 });
});

it('rejects a matrix from another card instead of showing a false delivery result', async () => {
  const value = projection('delivered'); value.units = [{ ...unit('fr:foreign'), card_id: 'other' }]; value.page.total = 1;
  api.getPersistedReadinessProjection.mockResolvedValue(value);
  render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await screen.findByText('Delivery unavailable');
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});

it('does not mix pages from different assessments or keep green percentages after that conflict', async () => {
  const first = projection(); first.units = [unit('fr:first')];
  first.page = { offset: 0, limit: 20, total: 2, has_more: true, next_cursor: 'next' };
  const next = { ...first, projection: { ...first.projection, generation_id: 'changed' }, units: [unit('fr:second')] };
  api.getPersistedReadinessProjection.mockResolvedValueOnce(first).mockResolvedValueOnce(next);
  render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Next items' }));
  await screen.findByText('Delivery unavailable', {}, { timeout: 2000 });
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  expect(screen.queryByText('Implemented')).not.toBeInTheDocument();
  expect(screen.queryByText('Functional requirement (second)')).not.toBeInTheDocument();
});

it('reads one scoped projection and loads pending work only inside Details', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={card} canProgress />);
  await screen.findByText('Work remaining');
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  expect(api.getPersistedReadinessProjection.mock.calls[0][2]).toEqual({ cardId: 'c', unitLimit: 20 });
  expect(screen.getByRole('button', { name: 'Show details' })).toHaveAttribute('aria-expanded', 'false');
  expect(screen.queryByRole('button', { name: /^Refresh$/ })).not.toBeInTheDocument();
  for (const fn of [api.getBoard, api.getDeliveryEvidence, api.getDeliveryReadinessPage, api.getCardDeliveryLedger, api.getTaskRequirementOptions]) expect(fn).not.toHaveBeenCalled();
  expect(screen.queryByRole('slider')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
  expect(await screen.findByText('Delivery details')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Hide details' })).toHaveAttribute('aria-expanded', 'true');
  await screen.findByText(/No pending checks were identified/);
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: 'Verification and pending work' })).not.toBeInTheDocument();
  for (const fn of [api.getBoard, api.getDeliveryEvidence, api.getCardDeliveryLedger, api.getTaskRequirementOptions]) expect(fn).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Hide details' }));
  fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
  await screen.findByText(/No pending checks were identified/);
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(1);
});

it('waits for the refreshed overview before reading checks and makes only one diagnostic request per refresh', async () => {
  const view = render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await screen.findByText('Work remaining');
  fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
  await screen.findByText(/No pending checks were identified/);
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(1);
  let finish!: (value: PersistedReadinessProjection) => void;
  api.getPersistedReadinessProjection.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} refreshRequest={1} />);
  expect(screen.getByRole('status')).toHaveTextContent('Checking delivery');
  await waitFor(() => expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2), { timeout: 2000 });
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(1);
  const next = projection(); next.projection.generation_id = 'generation-2';
  api.getDeliveryReadinessPage.mockResolvedValue({ ...readiness(), projection: {
    projection_version: 1, generation_id: 'generation-2', snapshot_id: 'a'.repeat(64), freshness: 'current' } });
  await act(async () => finish(next));
  await screen.findByText(/No pending checks were identified/);
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(2);
});

it.each([
  ['updating_or_unknown', 'Delivery not confirmed'], ['needs_attention', 'Needs attention'], ['optional_or_waived', 'Not required'],
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
  fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
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
  await screen.findByText('Verification failed');
  expect(screen.queryByText(payload)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Show details' })).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
  expect(await screen.findByText(payload)).toBeVisible();
  expect(screen.getAllByRole('status')).toHaveLength(1);
  expect(screen.getByRole('status')).toHaveTextContent('Verification failed');
  expect(container.querySelector('script, img')).toBeNull();
  expect(container.querySelector('.lucide-circle-x')).toBeInTheDocument();
  expect(screen.queryByRole('slider')).not.toBeInTheDocument();
});

it('labels old remaining work as a dated declaration even after delivery is complete', async () => {
  const value = projection('delivered');
  value.task_delivery!.last_update = { record_id: 'old-update', content: 'Work was still being collected',
    remaining: 'Finish the old classifier', submitted_at: '2026-11-01T10:00:00Z',
    origin: 'technical_checkpoint', text_truncated: false };
  api.getPersistedReadinessProjection.mockResolvedValue(value);
  render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await screen.findByText('Delivered');
  fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
  expect(await screen.findByText('Last recorded update')).toBeVisible();
  expect(screen.getByText('Work remaining when this update was recorded')).toBeVisible();
  expect(screen.getByText(/This is a historical declaration/)).toBeVisible();
  expect(document.querySelector('time')).toHaveAttribute('dateTime', '2026-11-01T10:00:00Z');
  expect(screen.getByRole('status')).toHaveTextContent('Delivered');
});

it('does not label an unavailable persisted generation as checking or show cached percentages', async () => {
  const value = projection('delivered');
  value.projection.freshness = 'unavailable';
  api.getPersistedReadinessProjection.mockResolvedValue(value);
  render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await screen.findByText('Delivery unavailable');
  expect(screen.queryByText('Checking delivery')).not.toBeInTheDocument();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});

it('coalesces update bursts and stops polling when the tab is inactive', async () => {
  vi.useFakeTimers();
  const pending = projection(); pending.projection.freshness = 'updating';
  api.getPersistedReadinessProjection.mockResolvedValue(pending);
  const view = render(<CardDeliveryDoDPanel boardId="b" card={card} />);
  await act(async () => { await Promise.resolve(); });
  act(() => { for (let n = 0; n < 30; n++) window.dispatchEvent(new Event('pulse:delivery-evidence-changed')); });
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} refreshRequest={1} />);
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} refreshRequest={2} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(999); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} active={false} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
});

it('does not replace a human draft when a refreshed projection changes its version', async () => {
  const view = render(<CardDeliveryDoDPanel boardId="b" card={card} canProgress />);
  await openProgress();
  const input = screen.getByLabelText('What was delivered or changed?');
  fireEvent.change(input, { target: { value: 'My draft' } });
  const next = projection(); next.task_delivery!.card_version = 2;
  api.getPersistedReadinessProjection.mockResolvedValue(next);
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} canProgress refreshRequest={1} />);
  await waitFor(() => expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2), { timeout: 2000 });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save update' })).toBeDisabled());
  expect(input).toHaveValue('My draft');
  expect(api.submitHumanTask).not.toHaveBeenCalled();
});

it('keeps the same draft, focus and text selection when Details is toggled', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={card} canProgress />);
  await openProgress();
  const input = screen.getByLabelText('What was delivered or changed?') as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: 'Draft preserved across Details' } });
  input.focus(); input.setSelectionRange(2, 8);
  fireEvent.click(screen.getByRole('button', { name: 'Hide details' }));
  expect(screen.queryByRole('form', { name: 'Task update' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
  expect(screen.getByLabelText('What was delivered or changed?')).toBe(input);
  expect(input).toHaveValue('Draft preserved across Details');
  expect(input).toHaveFocus();
  expect([input.selectionStart, input.selectionEnd]).toEqual([2, 8]);
  expect(api.submitHumanTask).not.toHaveBeenCalled();
});

it('preserves the draft and Details after a failed refresh, then recovers only on explicit modal refresh', async () => {
  const view = render(<CardDeliveryDoDPanel boardId="b" card={card} canProgress />);
  await openProgress();
  const input = screen.getByLabelText('What was delivered or changed?');
  fireEvent.change(input, { target: { value: 'Keep my draft even offline' } });
  api.getPersistedReadinessProjection.mockRejectedValue(new Error('Network unavailable'));
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} canProgress refreshRequest={1} />);
  await screen.findByText('Delivery unavailable', {}, { timeout: 2000 });
  expect(input).toHaveValue('Keep my draft even offline');
  expect(screen.getByRole('button', { name: 'Save update' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Hide details' })).toHaveAttribute('aria-expanded', 'true');
  act(() => { for (let n = 0; n < 30; n++) window.dispatchEvent(new Event('pulse:delivery-evidence-changed')); });
  await new Promise(resolve => setTimeout(resolve, 1100));
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  api.getPersistedReadinessProjection.mockResolvedValue(projection());
  view.rerender(<CardDeliveryDoDPanel boardId="b" card={card} canProgress refreshRequest={2} />);
  await screen.findByText('Work remaining');
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(3);
  expect(screen.getByLabelText('What was delivered or changed?')).toBe(input);
  expect(input).toHaveValue('Keep my draft even offline');
  expect(screen.getByRole('button', { name: 'Save update' })).toBeEnabled();
  expect(api.submitHumanTask).not.toHaveBeenCalled();
});
