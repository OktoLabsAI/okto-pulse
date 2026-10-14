import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DeliveryReadinessView } from '../DeliveryReadinessView';
import { KanbanCard } from '@/components/kanban/KanbanCard';
import { CardDeliveryDoDPanel } from '../CardDeliveryDoDPanel';
import { DeliveryEvidencePanel } from '../DeliveryEvidencePanel';
import { emptyCounts, metric, readiness, task } from './readinessFixture';

const api = vi.hoisted(() => ({ getDeliveryReadiness: vi.fn(), getDeliveryEvidence: vi.fn(), getBoard: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('@dnd-kit/sortable', () => ({ useSortable: () => ({ setNodeRef: vi.fn(), attributes: {}, listeners: {} }) }));
beforeEach(() => {
  vi.clearAllMocks();
  api.getDeliveryReadiness.mockImplementation((_board, _spec, cardId) => Promise.resolve(readiness(cardId ?? null)));
  api.getDeliveryEvidence.mockResolvedValue({ board_id: 'b', spec_id: 's', edition: 1, version: 1, complete: true,
    candidates: [], implementations: [], rows: [], tests: [], rejected_record_ids: [], per_card: [] });
  api.getBoard.mockResolvedValue({ settings: {} });
});
afterEach(cleanup);

it.each(['normal', 'bug', 'test'] as const)('U01: %s cover and Delivery share backend metrics in independent bars', async card_type => {
  render(<><KanbanCard card={{ ...task, card_type }} nameMap={{}} onClick={vi.fn()} deliveryReadiness={{ value: readiness() }} />
    <CardDeliveryDoDPanel boardId="b" card={{ id: 'c', card_type, spec_id: 's' }} /></>);
  await waitFor(() => expect(screen.getAllByRole('progressbar')).toHaveLength(4));
  const implementation = screen.getAllByRole('progressbar', { name: 'Implementation' });
  const verification = screen.getAllByRole('progressbar', { name: 'Verification' });
  implementation.forEach((bar, i) => {
    expect(bar).toHaveAttribute('aria-valuenow', '100');
    expect(bar).toHaveAttribute('aria-valuetext', '100%, 8 of 8 applicable obligations');
    expect(verification[i]).toHaveAttribute('aria-valuenow', '75');
    expect(bar.parentElement).not.toBe(verification[i].parentElement);
    expect(bar).toHaveStyle({ width: '100%' }); expect(verification[i]).toHaveStyle({ width: '75%' });
    expect(bar).toHaveAttribute('aria-valuemin', '0'); expect(bar).toHaveAttribute('aria-valuemax', '100');
  });
});

it('Spec Delivery consumes its own context, without averaging card percentages', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  expect(await screen.findByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  expect(api.getDeliveryReadiness).toHaveBeenCalledWith('b', 's', undefined, expect.any(AbortSignal));
});

it('U02: 100/100 remains closure pending while native lifecycle is open', () => {
  const value = readiness(); value.verification = metric(100, 8); value.delivery_state = 'ready';
  render(<DeliveryReadinessView value={value} />);
  expect(screen.getAllByRole('progressbar')).toHaveLength(2);
  expect(screen.getByText('Proof complete · closure pending')).toBeVisible();
  expect(screen.queryByText('Delivered', { exact: true })).not.toBeInTheDocument();
});

it('U04: Done remains historical when current qualification diverges', () => {
  const value = readiness(); value.lifecycle.stored_status = 'done';
  value.summary.message_code = 'historical_completion_diverged';
  render(<DeliveryReadinessView value={value} />);
  expect(screen.getByText('Status: done')).toBeVisible();
  expect(screen.getByText('Historical completion · current qualification pending')).toBeVisible();
  expect(screen.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
});

it('Test Card shows verification alone and scenario counts with failure text even when Done', () => {
  const value = readiness(); value.scope.card_type = 'test'; value.lifecycle.stored_status = 'done';
  value.implementation = { ...metric(100, 8), status: 'not_applicable', percent: null, total: 0, applicable: 0, satisfied: 0, unresolved: 0, counts_by_state: emptyCounts };
  value.verification.unit = 'scenario'; value.verification.counts_by_state.failed = 2;
  value.verification.counts_by_state.missing = 0; value.delivery_state = 'verification_failed';
  render(<DeliveryReadinessView value={value} />);
  expect(screen.queryByRole('progressbar', { name: 'Implementation' })).not.toBeInTheDocument();
  expect(screen.queryByText('Implementation', { exact: true })).not.toBeInTheDocument();
  expect(screen.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuetext', '75%, 6 of 8 applicable scenarios');
  expect(screen.getByText('Execution complete · failures found')).toBeVisible();
  expect(screen.getByText('Failed or conflicting evidence')).toBeVisible();
});

it.each([true, false])('Test Card without implementation gives verification the full grid (compact=%s)', compact => {
  const value = readiness(); value.scope.card_type = 'test';
  value.implementation = { ...metric(0, 0), status: 'not_applicable', percent: null };
  render(<DeliveryReadinessView value={value} compact={compact} />);
  expect(screen.queryByText('Implementation', { exact: true })).not.toBeInTheDocument();
  expect(screen.queryByText('N/A', { exact: true })).not.toBeInTheDocument();
  const bar = screen.getByRole('progressbar', { name: 'Verification' });
  expect(screen.getAllByRole('progressbar')).toHaveLength(1);
  expect(bar.closest('.grid')).toHaveClass('grid-cols-1');
  expect(bar).toHaveAttribute('aria-valuenow', '75');
});

it('Unknown implementation on a Test Card is not silently treated as non-applicable', () => {
  const value = readiness(); value.scope.card_type = 'test';
  value.implementation = { ...value.implementation, status: 'unknown', percent: null };
  render(<DeliveryReadinessView value={value} compact />);
  expect(screen.getByText('Implementation', { exact: true })).toBeVisible();
  expect(screen.getByRole('progressbar', { name: 'Verification' }).closest('.grid')).toHaveClass('grid-cols-2');
});

it.each(['loading', 'error', 'unknown', 'incompatible'] as const)('U03/U10: %s never displays cached or invented percentages', state => {
  const value = readiness();
  if (state === 'unknown') for (const key of ['implementation', 'verification'] as const)
    value[key] = { ...value[key], status: 'unknown', percent: null, applicable: null, total: null, unresolved: null };
  if (state === 'incompatible') Object.assign(value, { metrics_version: 1, delivery_completeness: { percent: 100 } });
  render(<DeliveryReadinessView value={value} loading={state === 'loading'} error={state === 'error' ? 'Read denied' : undefined} />);
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  expect(screen.queryByText(/100%|75%/)).not.toBeInTheDocument();
  if (state === 'incompatible') expect(screen.getByText(/contract unavailable or incompatible/)).toBeVisible();
});

it('N/A and waiver counts remain visible without adding proof credit', () => {
  const value = readiness(); value.verification.not_applicable = 1; value.verification.waived = 1;
  render(<DeliveryReadinessView value={value} />);
  expect(screen.getByText('1 N/A')).toBeVisible();
  expect(screen.getByText('1 waived · no proof credit')).toBeVisible();
  expect(screen.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
});

it('U09: a genuinely non-applicable scope invites inspection rather than additional work', () => {
  const value = readiness(); value.delivery_state = 'not_applicable';
  for (const key of ['implementation', 'verification'] as const) value[key] = { ...metric(0, 0), status: 'not_applicable', percent: null };
  render(<DeliveryReadinessView value={value} compact onInspect={vi.fn()} />);
  expect(screen.getByRole('button', { name: 'Open delivery details' })).toBeVisible();
  expect(screen.getAllByText('N/A', { exact: true })).toHaveLength(2);
  expect(screen.queryByRole('button', { name: 'What is missing?' })).not.toBeInTheDocument();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});

it('compact cover shows only metrics and opens the full diagnosis without triggering the Card', () => {
  const inspect = vi.fn(), openCard = vi.fn();
  render(<div onClick={openCard}><DeliveryReadinessView value={readiness()} compact onInspect={inspect} /></div>);
  expect(screen.getByText('100%')).toBeVisible();
  expect(screen.getByText('75%')).toBeVisible();
  expect(screen.queryByText('8/8')).not.toBeInTheDocument();
  expect(screen.queryByText(/pending units/)).not.toBeInTheDocument();
  expect(screen.queryByText('What is missing?')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Open delivery details' }));
  expect(inspect).toHaveBeenCalledOnce();
  expect(openCard).not.toHaveBeenCalled();
});
